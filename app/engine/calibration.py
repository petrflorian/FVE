"""
Self-learning calibration engine.

Phase 'warmup' (0 valid days): use factor 1.0 (raw forecast unchanged).
Phase 'phase1' (1+ valid days): global rolling correction factor.
Phase 'phase2' (14+ valid days): + time-of-day correction factors.
"""

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from app.database import DatabaseManager

logger = logging.getLogger(__name__)

# Minimum forecasted Wh to include a day in calibration.
# Excludes heavily overcast/snowy days that would distort the model.
MIN_FORECAST_WH = 200

# Factor safety clamps – prevent runaway correction from outlier days.
FACTOR_MIN = 0.3
FACTOR_MAX = 3.0

# Activate phase2 (time-of-day corrections) after this many valid days.
PHASE2_MIN_DAYS = 14

# Minimum data points per ToD band to trust the band factor.
TOD_MIN_SAMPLES = 5


@dataclass
class CalibrationState:
    global_correction: float = 1.0
    tod_morning_factor: float = 1.0    # 06:00–10:59
    tod_midday_factor: float = 1.0     # 11:00–14:59
    tod_afternoon_factor: float = 1.0  # 15:00–19:59
    days_of_data: int = 0
    phase: str = "warmup"


class CalibrationEngine:

    def __init__(self, db: DatabaseManager, window_days: int = 14) -> None:
        self.db = db
        self.window_days = window_days

    async def run_daily_calibration(self, target_date: date) -> CalibrationState:
        """
        Full calibration cycle for target_date:
        1. Compute actual daily Wh via trapezoidal integration of power readings.
        2. Get raw forecast daily total.
        3. Store ratio in daily_summary.
        4. Recompute rolling factors from window.
        5. Update calibration_state singleton in DB.
        6. Return the new CalibrationState.
        """
        date_str = target_date.isoformat()
        logger.info("Running daily calibration for %s", date_str)

        actual_wh = await self._compute_actual_wh(date_str)
        forecast_wh = await self.db.get_latest_forecast_wh_day(date_str)

        ratio: Optional[float] = None
        if (
            actual_wh is not None
            and forecast_wh is not None
            and forecast_wh >= MIN_FORECAST_WH
        ):
            ratio = actual_wh / forecast_wh
            logger.info(
                "%s: actual=%.0f Wh, forecast=%.0f Wh, ratio=%.3f",
                date_str, actual_wh, forecast_wh, ratio,
            )
        else:
            logger.info(
                "%s: skipped ratio (actual=%s Wh, forecast=%s Wh)",
                date_str, actual_wh, forecast_wh,
            )

        state = await self._recompute_state()

        # Update daily_summary with new calibration info
        calibrated_wh = (
            forecast_wh * state.global_correction
            if forecast_wh is not None
            else None
        )
        await self.db.upsert_daily_summary(
            {
                "summary_date": date_str,
                "forecast_wh_raw": forecast_wh,
                "forecast_wh_calibrated": calibrated_wh,
                "actual_wh": actual_wh,
                "ratio": ratio,
                "correction_factor": state.global_correction,
                "tod_morning_factor": state.tod_morning_factor,
                "tod_midday_factor": state.tod_midday_factor,
                "tod_afternoon_factor": state.tod_afternoon_factor,
            }
        )

        return state

    async def _recompute_state(self) -> CalibrationState:
        """Recompute global + ToD factors from the last window of valid days."""
        summaries = await self.db.get_recent_summaries(self.window_days * 2)
        valid = [s for s in summaries if s.get("ratio") is not None]
        valid = valid[: self.window_days]  # newest first, trim to window

        state = CalibrationState(days_of_data=len(valid))

        if not valid:
            # Not enough data yet
            await self.db.update_calibration_state(
                {
                    "global_correction": 1.0,
                    "tod_morning_factor": 1.0,
                    "tod_midday_factor": 1.0,
                    "tod_afternoon_factor": 1.0,
                    "days_of_data": 0,
                    "phase": "warmup",
                }
            )
            return state

        state.phase = "phase1"
        ratios = [s["ratio"] for s in valid]
        state.global_correction = self._clamp(
            sum(ratios) / len(ratios), FACTOR_MIN, FACTOR_MAX
        )

        if len(valid) >= PHASE2_MIN_DAYS:
            state.phase = "phase2"
            tod = await self._compute_tod_factors(valid)
            state.tod_morning_factor = tod["morning"]
            state.tod_midday_factor = tod["midday"]
            state.tod_afternoon_factor = tod["afternoon"]

        await self.db.update_calibration_state(
            {
                "global_correction": state.global_correction,
                "tod_morning_factor": state.tod_morning_factor,
                "tod_midday_factor": state.tod_midday_factor,
                "tod_afternoon_factor": state.tod_afternoon_factor,
                "days_of_data": state.days_of_data,
                "phase": state.phase,
            }
        )
        logger.info(
            "Calibration updated: phase=%s, global=%.3f, days=%d",
            state.phase, state.global_correction, state.days_of_data,
        )
        return state

    async def _compute_tod_factors(self, valid_days: list[dict]) -> dict:
        """
        For each valid day, compute per-band (morning/midday/afternoon) ratio
        of actual power vs. forecasted power, then average across all days.
        """
        bands: dict[str, list[float]] = {
            "morning": [],
            "midday": [],
            "afternoon": [],
        }

        for summary in valid_days:
            day_str = summary["summary_date"]
            actuals = await self.db.get_actuals_for_date(day_str)
            forecast_slots = await self.db.get_forecast_for_date(day_str)

            # Build hour → forecast watts lookup
            f_by_hour: dict[int, float] = {}
            for slot in forecast_slots:
                try:
                    hour = int(slot["slot_time"][11:13])
                    f_by_hour[hour] = slot["watts"]
                except (ValueError, KeyError, TypeError):
                    continue

            for actual in actuals:
                try:
                    hour = int(actual["sampled_at"][11:13])
                    power_w = actual["power_w"]
                    f_watts = f_by_hour.get(hour, 0)
                except (ValueError, KeyError, TypeError):
                    continue

                band = self._hour_to_band(hour)
                if band is None or f_watts < 10 or power_w is None:
                    continue
                bands[band].append(power_w / f_watts)

        return {
            "morning": self._clamp(
                self._safe_mean(bands["morning"]), FACTOR_MIN, FACTOR_MAX
            ),
            "midday": self._clamp(
                self._safe_mean(bands["midday"]), FACTOR_MIN, FACTOR_MAX
            ),
            "afternoon": self._clamp(
                self._safe_mean(bands["afternoon"]), FACTOR_MIN, FACTOR_MAX
            ),
        }

    async def _compute_actual_wh(self, date_str: str) -> Optional[float]:
        """
        Compute actual Wh for a date using trapezoidal integration of power_w samples.
        Returns None if fewer than 2 readings are available.
        """
        rows = await self.db.get_actuals_for_date(date_str)
        if len(rows) < 2:
            return None

        total_wh = 0.0
        for i in range(1, len(rows)):
            try:
                t0 = datetime.fromisoformat(rows[i - 1]["sampled_at"])
                t1 = datetime.fromisoformat(rows[i]["sampled_at"])
                dt_h = (t1 - t0).total_seconds() / 3600
                avg_w = (rows[i - 1]["power_w"] + rows[i]["power_w"]) / 2
                total_wh += avg_w * dt_h
            except (ValueError, TypeError, KeyError):
                continue

        return max(0.0, total_wh)

    def apply_correction(
        self,
        hour: int,
        raw_watts: float,
        state: CalibrationState,
    ) -> float:
        """
        Apply calibration factors to a single forecast slot.
        Phase1: multiply by global_correction.
        Phase2: additionally multiply by time-of-day factor.
        """
        result = raw_watts * state.global_correction
        if state.phase == "phase2":
            band = self._hour_to_band(hour)
            if band == "morning":
                result *= state.tod_morning_factor
            elif band == "midday":
                result *= state.tod_midday_factor
            elif band == "afternoon":
                result *= state.tod_afternoon_factor
        return max(0.0, result)

    @staticmethod
    def _hour_to_band(hour: int) -> Optional[str]:
        if 6 <= hour < 11:
            return "morning"
        if 11 <= hour < 15:
            return "midday"
        if 15 <= hour < 20:
            return "afternoon"
        return None

    @staticmethod
    def _safe_mean(values: list[float]) -> float:
        return sum(values) / len(values) if len(values) >= TOD_MIN_SAMPLES else 1.0

    @staticmethod
    def _clamp(value: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, value))

    async def get_current_state(self) -> CalibrationState:
        """Load current calibration state from DB."""
        row = await self.db.get_calibration_state()
        if not row:
            return CalibrationState()
        return CalibrationState(
            global_correction=row.get("global_correction", 1.0),
            tod_morning_factor=row.get("tod_morning_factor", 1.0),
            tod_midday_factor=row.get("tod_midday_factor", 1.0),
            tod_afternoon_factor=row.get("tod_afternoon_factor", 1.0),
            days_of_data=row.get("days_of_data", 0),
            phase=row.get("phase", "warmup"),
        )
