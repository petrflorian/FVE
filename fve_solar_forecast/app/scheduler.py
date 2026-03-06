"""
APScheduler job definitions.

Jobs:
  fetch_forecast    – stáhne předpověď z forecast.solar (každou hodinu + při startu)
  fetch_weather     – stáhne počasí z Open-Meteo (6:10 + 14:10 local)
  collect_actual    – zaznamená aktuální výkon z HA (každých 5 min, 5–21h local)
  daily_calibrate   – spustí kalibraci a uloží denní souhrn (23:30 local)
"""

import logging
from datetime import date, datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.clients.forecast_solar import ForecastSolarClient
from app.clients.ha_client import HAClient
from app.clients.open_meteo import OpenMeteoClient
from app.config import AppConfig
from app.database import DatabaseManager
from app.engine.calibration import CalibrationEngine

logger = logging.getLogger(__name__)


class JobScheduler:

    def __init__(
        self,
        config: AppConfig,
        db: DatabaseManager,
        forecast_client: ForecastSolarClient,
        ha_client: HAClient,
        calibration: CalibrationEngine,
        weather_client: OpenMeteoClient,
    ) -> None:
        self.config = config
        self.db = db
        self.forecast_client = forecast_client
        self.ha_client = ha_client
        self.calibration = calibration
        self.weather_client = weather_client
        self.scheduler = AsyncIOScheduler(timezone=config.timezone)

    def start(self) -> None:
        # ── Forecast fetch: every hour + immediately on startup ──────────
        self.scheduler.add_job(
            self._fetch_forecast_job,
            trigger=IntervalTrigger(hours=1),
            id="fetch_forecast_hourly",
            name="Fetch forecast.solar (hourly)",
            next_run_time=datetime.now(dt_timezone.utc),  # run immediately at startup
            max_instances=1,
            coalesce=True,
        )

        # ── Weather fetch: 06:10 + 14:10 local ───────────────────────────
        self.scheduler.add_job(
            self._fetch_weather_job,
            trigger=CronTrigger(hour="6,14", minute=10, timezone=self.config.timezone),
            id="fetch_weather",
            name="Fetch Open-Meteo weather (6+14h local)",
            next_run_time=datetime.now(dt_timezone.utc),  # also run at startup
            max_instances=1,
            coalesce=True,
        )

        # ── Actual power logging: every 5 min during daylight (local time) ─
        self.scheduler.add_job(
            self._collect_actual_job,
            trigger=CronTrigger(minute="*/5", hour="5-21", timezone=self.config.timezone),
            id="collect_actual",
            name="Collect HA PV power (5 min, 5–21h local)",
            max_instances=1,
            coalesce=True,
        )

        # ── Daily calibration: 23:30 local ───────────────────────────────
        self.scheduler.add_job(
            self._daily_calibrate_job,
            trigger=CronTrigger(hour=23, minute=30, timezone=self.config.timezone),
            id="daily_calibrate",
            name="Daily calibration (23:30 local)",
            max_instances=1,
        )

        self.scheduler.start()
        logger.info(
            "Scheduler started with %d jobs", len(self.scheduler.get_jobs())
        )

    def stop(self) -> None:
        self.scheduler.shutdown(wait=False)

    # ── Job implementations ───────────────────────────────────────────────

    async def _fetch_forecast_job(self) -> None:
        try:
            slots = await self.forecast_client.fetch()
            count = await self.db.upsert_forecast_slots(datetime.utcnow(), slots)
            logger.info("Forecast fetch: stored %d slots", count)
        except Exception as exc:
            logger.error("Forecast fetch failed: %s", exc)

    async def _fetch_weather_job(self) -> None:
        try:
            records = await self.weather_client.fetch_today_and_tomorrow()
            await self.db.upsert_weather_hourly(records)
            logger.info("Weather fetch: stored %d hourly records", len(records))
        except Exception as exc:
            logger.error("Weather fetch failed: %s", exc)

    async def _collect_actual_job(self) -> None:
        power_w = await self.ha_client.get_pv_power_w()
        if power_w is None:
            logger.debug("HA power sensor returned None – skipping")
            return
        energy_kwh = await self.ha_client.get_energy_kwh()
        sampled_at = datetime.utcnow().isoformat(timespec="seconds")
        try:
            await self.db.insert_actual(sampled_at, power_w, energy_kwh)
            logger.debug("Logged %.1f W at %s", power_w, sampled_at)
        except Exception as exc:
            logger.error("Failed to store actual reading: %s", exc)

    async def _daily_calibrate_job(self) -> None:
        tz = ZoneInfo(self.config.timezone)
        yesterday = datetime.now(tz).date() - timedelta(days=1)
        try:
            state = await self.calibration.run_daily_calibration(yesterday)
            logger.info(
                "Calibration done: phase=%s, global_factor=%.3f, days=%d",
                state.phase, state.global_correction, state.days_of_data,
            )
        except Exception as exc:
            logger.error("Daily calibration failed: %s", exc)
