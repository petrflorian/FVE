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

        metrics = []
        for s in summaries:
            actual = s.get("actual_wh")
            forecast_cal = s.get("forecast_wh_calibrated")
            error_wh = None
            mape = None
            if actual is not None and forecast_cal is not None and actual > 0:
                error_wh = actual - forecast_cal
                mape = abs(error_wh) / actual * 100

            metrics.append(
                {
                    "date": s["summary_date"],
                    "ratio": round(s["ratio"], 3) if s.get("ratio") else None,
                    "correction_factor": round(s["correction_factor"], 3)
                    if s.get("correction_factor")
                    else None,
                    "error_wh": round(error_wh, 0) if error_wh is not None else None,
                    "mape_pct": round(mape, 1) if mape is not None else None,
                    "tod_morning": s.get("tod_morning_factor"),
                    "tod_midday": s.get("tod_midday_factor"),
                    "tod_afternoon": s.get("tod_afternoon_factor"),
                }
            )

        return {
            "calibration_state": cal_state_row,
            "metrics": metrics,
        }

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
