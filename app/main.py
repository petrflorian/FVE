"""
FVE Solar Forecast – application entrypoint.

Starts:
  1. DatabaseManager – initialises SQLite schema
  2. Service clients  – ForecastSolarClient, HAClient
  3. CalibrationEngine
  4. JobScheduler     – APScheduler async jobs
  5. FastAPI + uvicorn on port 8099 (HA Ingress)
"""

import asyncio
import logging
import os
import sys

import uvicorn

from app.clients.forecast_solar import ForecastSolarClient
from app.clients.ha_client import HAClient
from app.config import load_config
from app.database import DatabaseManager
from app.engine.calibration import CalibrationEngine
from app.scheduler import JobScheduler
from app.web.app import create_app


def setup_logging(level: str) -> None:
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        stream=sys.stdout,
        level=numeric,
        format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Quiet down noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


async def main() -> None:
    config = load_config()
    setup_logging(config.log_level)
    logger = logging.getLogger(__name__)

    logger.info("FVE Solar Forecast starting…")
    logger.info(
        "Location: %.4f, %.4f | %.1f kWp | tilt=%d° | azimuth=%d°",
        config.latitude, config.longitude,
        config.kwp, config.tilt, config.azimuth,
    )

    # 1. Database
    db = DatabaseManager()
    await db.initialize()

    # 2. Service clients
    forecast_client = ForecastSolarClient(config)
    ha_client = HAClient(config)

    # 3. Calibration engine
    calibration = CalibrationEngine(db, window_days=config.calibration_window_days)

    # 4. Scheduler
    scheduler = JobScheduler(config, db, forecast_client, ha_client, calibration)
    scheduler.start()

    # Optional: verify HA connection at startup
    if await ha_client.check_connection():
        logger.info("Home Assistant API connection OK")
    else:
        logger.warning(
            "Could not reach Home Assistant API at %s – "
            "check ha_url and ha_token configuration",
            config.ha_url,
        )

    # 5. FastAPI + uvicorn
    app = create_app(db, calibration)

    uv_config = uvicorn.Config(
        app=app,
        host="0.0.0.0",
        port=8099,
        log_level=config.log_level.lower(),
        proxy_headers=True,      # trust X-Forwarded-* from HA Ingress proxy
        forwarded_allow_ips="*",
    )
    server = uvicorn.Server(uv_config)

    try:
        await server.serve()
    finally:
        scheduler.stop()
        logger.info("FVE Solar Forecast stopped.")


if __name__ == "__main__":
    asyncio.run(main())
