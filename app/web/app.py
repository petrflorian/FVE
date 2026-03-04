"""
FastAPI application factory.

Serves three HTML pages (/, /week, /accuracy) and a JSON API (/api/*).
Handles HA Ingress correctly via X-Ingress-Path middleware.
"""

import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.database import DatabaseManager
from app.engine.calibration import CalibrationEngine, CalibrationState
from app.engine import metrics as M

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


def create_app(db: DatabaseManager, calibration: CalibrationEngine) -> FastAPI:
    app = FastAPI(title="FVE Solar Forecast", docs_url=None, redoc_url=None)

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # ── Middleware: extract X-Ingress-Path for correct URL prefix ────────
    @app.middleware("http")
    async def ingress_middleware(request: Request, call_next):
        ingress_path = request.headers.get("X-Ingress-Path", "").rstrip("/")
        request.state.base = ingress_path
        return await call_next(request)

    # ── HTML Pages ────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def today_page(request: Request):
        cal_state = await calibration.get_current_state()
        return templates.TemplateResponse(
            "today.html",
            {
                "request": request,
                "base": request.state.base,
                "phase": cal_state.phase,
                "global_correction": cal_state.global_correction,
                "days_of_data": cal_state.days_of_data,
            },
        )

    @app.get("/week", response_class=HTMLResponse)
    async def week_page(request: Request):
        return templates.TemplateResponse(
            "week.html", {"request": request, "base": request.state.base}
        )

    @app.get("/accuracy", response_class=HTMLResponse)
    async def accuracy_page(request: Request):
        cal_state = await calibration.get_current_state()
        return templates.TemplateResponse(
            "accuracy.html",
            {
                "request": request,
                "base": request.state.base,
                "cal": {
                    "phase": cal_state.phase,
                    "global_correction": cal_state.global_correction,
                    "tod_morning_factor": cal_state.tod_morning_factor,
                    "tod_midday_factor": cal_state.tod_midday_factor,
                    "tod_afternoon_factor": cal_state.tod_afternoon_factor,
                    "days_of_data": cal_state.days_of_data,
                },
            },
        )

    # ── JSON API ──────────────────────────────────────────────────────────

    @app.get("/api/forecast/today")
    async def api_today(request: Request):
        today = date.today().isoformat()
        cal_state = await calibration.get_current_state()
        slots = await db.get_forecast_for_date(today)
        actuals = await db.get_actuals_for_date(today)

        # Build hour → avg power lookup from actuals
        actuals_by_hour: dict[int, list[float]] = {}
        for a in actuals:
            try:
                h = int(a["sampled_at"][11:13])
                actuals_by_hour.setdefault(h, []).append(a["power_w"])
            except (ValueError, TypeError, KeyError):
                continue
        avg_actual = {
            h: sum(vs) / len(vs) for h, vs in actuals_by_hour.items()
        }

        result = []
        for slot in slots:
            try:
                hour = int(slot["slot_time"][11:13])
            except (ValueError, TypeError):
                continue
            calibrated_w = calibration.apply_correction(
                hour, slot["watts"], cal_state
            )
            result.append(
                {
                    "time": slot["slot_time"][11:16],  # "HH:MM"
                    "forecast_raw_w": round(slot["watts"], 1),
                    "forecast_calibrated_w": round(calibrated_w, 1),
                    "actual_w": round(avg_actual[hour], 1) if hour in avg_actual else None,
                }
            )

        return {
            "date": today,
            "phase": cal_state.phase,
            "global_correction": round(cal_state.global_correction, 3),
            "slots": result,
        }

    @app.get("/api/forecast/week")
    async def api_week(request: Request):
        today = date.today()
        cal_state = await calibration.get_current_state()
        summaries = {
            r["summary_date"]: r
            for r in await db.get_recent_summaries(30)
        }

        days = []
        for offset in range(-7, 8):
            d = today + timedelta(days=offset)
            d_str = d.isoformat()
            summary = summaries.get(d_str, {})

            forecast_raw: Optional[float] = summary.get("forecast_wh_raw")
            forecast_cal: Optional[float] = summary.get("forecast_wh_calibrated")

            # For future days not in summary, pull from forecasts table
            if forecast_raw is None and d >= today:
                forecast_raw = await db.get_latest_forecast_wh_day(d_str)
                if forecast_raw is not None:
                    forecast_cal = forecast_raw * cal_state.global_correction

            days.append(
                {
                    "date": d_str,
                    "label": d.strftime("%a %-d.%-m."),
                    "forecast_raw_wh": round(forecast_raw, 0) if forecast_raw else None,
                    "forecast_cal_wh": round(forecast_cal, 0) if forecast_cal else None,
                    "actual_wh": round(summary["actual_wh"], 0)
                    if summary.get("actual_wh") is not None
                    else None,
                    "is_future": d > today,
                }
            )

        return {"today": today.isoformat(), "days": days}

    @app.get("/api/accuracy")
    async def api_accuracy(request: Request):
        summaries = await db.get_recent_summaries(60)
        cal_state_row = await db.get_calibration_state()

        daily = []
        for s in summaries:
            daily.append(
                {
                    "date": s["summary_date"],
                    "ratio": _r(s.get("ratio"), 3),
                    "correction_factor": _r(s.get("correction_factor"), 3),
                    # Calibrated
                    "rmse_w":   _r(s.get("rmse_w"), 1),
                    "mae_w":    _r(s.get("mae_w"), 1),
                    "mbe_w":    _r(s.get("mbe_w"), 1),
                    "mape_pct": _r(s.get("mape_pct"), 1),
                    # Raw
                    "rmse_raw_w":   _r(s.get("rmse_raw_w"), 1),
                    "mae_raw_w":    _r(s.get("mae_raw_w"), 1),
                    "mbe_raw_w":    _r(s.get("mbe_raw_w"), 1),
                    "mape_raw_pct": _r(s.get("mape_raw_pct"), 1),
                    # Weather
                    "cloud_pct": _r(s.get("avg_cloud_cover_pct"), 0),
                    "temp_c":    _r(s.get("avg_temperature_c"), 1),
                    # Skill score: improvement of calibrated over raw
                    "skill_score": _skill(s.get("rmse_w"), s.get("rmse_raw_w")),
                }
            )

        # ── Aggregate statistics over valid days ──────────────────────────
        valid = [s for s in summaries if s.get("mape_pct") is not None]
        valid_raw = [s for s in summaries if s.get("mape_raw_pct") is not None]

        mape_values = [s["mape_pct"] for s in valid]
        mape_raw_values = [s["mape_raw_pct"] for s in valid_raw]
        rmse_values = [s["rmse_w"] for s in valid if s.get("rmse_w")]
        mbe_values  = [s["mbe_w"]  for s in valid if s.get("mbe_w") is not None]

        # Week-over-week improvement: last 7d vs. previous 7d MAPE
        recent7  = [s["mape_pct"] for s in valid[:7]  if s.get("mape_pct")]
        prev7    = [s["mape_pct"] for s in valid[7:14] if s.get("mape_pct")]
        wow_improvement = None
        if recent7 and prev7:
            avg_r = sum(recent7) / len(recent7)
            avg_p = sum(prev7) / len(prev7)
            if avg_p > 0:
                wow_improvement = round((avg_p - avg_r) / avg_p * 100, 1)

        aggregate = {
            "n_days": len(valid),
            "mean_mape_pct":     _r(sum(mape_values) / len(mape_values) if mape_values else None, 1),
            "mean_mape_raw_pct": _r(sum(mape_raw_values) / len(mape_raw_values) if mape_raw_values else None, 1),
            "mean_rmse_w":    _r(sum(rmse_values) / len(rmse_values) if rmse_values else None, 1),
            "mean_mbe_w":     _r(sum(mbe_values) / len(mbe_values) if mbe_values else None, 1),
            "p25_mape":  _r(M.percentile(mape_values, 25), 1),
            "p50_mape":  _r(M.percentile(mape_values, 50), 1),
            "p75_mape":  _r(M.percentile(mape_values, 75), 1),
            "wow_improvement_pct": wow_improvement,
            "overall_skill_score": _r(
                M.skill_score(
                    sum(rmse_values) / len(rmse_values) if rmse_values else None,
                    sum(s["rmse_raw_w"] for s in valid_raw if s.get("rmse_raw_w")) /
                    len([s for s in valid_raw if s.get("rmse_raw_w")]) if valid_raw else None,
                ),
                1,
            ),
        }

        return {
            "calibration_state": cal_state_row,
            "aggregate": aggregate,
            "daily": daily,
        }

    @app.get("/api/accuracy/hourly")
    async def api_accuracy_hourly(request: Request):
        """Per-hour-of-day accuracy breakdown – how accurate is each hour."""
        rows = await db.get_hourly_accuracy()
        return {"hours": rows}

    @app.get("/api/accuracy/weather")
    async def api_accuracy_weather(request: Request):
        """
        Accuracy vs. cloud cover: bucket actual/forecast errors by cloud_cover_pct.
        Returns list of {cloud_bucket, n_samples, mean_mape_pct, mean_error_wh}.
        """
        summaries = await db.get_recent_summaries(90)
        buckets: dict[str, list[float]] = {
            "0-20": [], "20-40": [], "40-60": [], "60-80": [], "80-100": [],
        }
        for s in summaries:
            if s.get("avg_cloud_cover_pct") is None or s.get("mape_pct") is None:
                continue
            cc = s["avg_cloud_cover_pct"]
            if   cc < 20:  buckets["0-20"].append(s["mape_pct"])
            elif cc < 40:  buckets["20-40"].append(s["mape_pct"])
            elif cc < 60:  buckets["40-60"].append(s["mape_pct"])
            elif cc < 80:  buckets["60-80"].append(s["mape_pct"])
            else:          buckets["80-100"].append(s["mape_pct"])

        result = []
        for label, vals in buckets.items():
            result.append(
                {
                    "cloud_bucket": label + "%",
                    "n_samples": len(vals),
                    "mean_mape_pct": round(sum(vals) / len(vals), 1) if vals else None,
                }
            )
        return {"cloud_buckets": result}

    @app.get("/api/status")
    async def api_status():
        """Health check endpoint."""
        cal_state = await db.get_calibration_state()
        return {
            "status": "ok",
            "calibration_phase": cal_state.get("phase", "unknown"),
            "days_of_data": cal_state.get("days_of_data", 0),
            "global_correction": cal_state.get("global_correction", 1.0),
        }

    return app


def _r(value, decimals: int = 1) -> Optional[float]:
    """Round a nullable float."""
    return round(value, decimals) if value is not None else None


def _skill(rmse_cal: Optional[float], rmse_raw: Optional[float]) -> Optional[float]:
    if rmse_cal is None or rmse_raw is None or rmse_raw == 0:
        return None
    return round((1 - rmse_cal / rmse_raw) * 100, 1)
