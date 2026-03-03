"""Configuration loader – reads /data/options.json written by HA Supervisor."""

import json
import os
from pathlib import Path
from pydantic import BaseModel, Field
from typing import Optional

OPTIONS_PATH = Path("/data/options.json")


class AppConfig(BaseModel):
    latitude: float
    longitude: float
    tilt: int = Field(default=30, ge=0, le=90)
    azimuth: int = Field(default=180, ge=0, le=360)
    kwp: float = Field(default=5.0, gt=0)
    ha_token: Optional[str] = None
    ha_url: str = "http://supervisor/core"
    ha_sensor_power: str = "sensor.solar_assistant_pv_power"
    ha_sensor_energy: str = "sensor.solar_assistant_pv_energy_today"
    calibration_window_days: int = Field(default=14, ge=7, le=90)
    log_level: str = "info"

    @property
    def effective_ha_token(self) -> str:
        """
        Use explicit ha_token if provided, otherwise fall back to SUPERVISOR_TOKEN
        injected by HA Supervisor (when homeassistant_api: true in config.yaml).
        """
        return self.ha_token or os.environ.get("SUPERVISOR_TOKEN", "")

    @property
    def forecast_solar_azimuth(self) -> int:
        """
        Convert standard azimuth (0=N, 90=E, 180=S, 270=W) to
        forecast.solar convention (-180..180, 0=S, -90=E, 90=W).
        """
        az = self.azimuth - 180
        return az


def load_config() -> AppConfig:
    """Load configuration from HA options.json or return defaults for development."""
    if OPTIONS_PATH.exists():
        raw = json.loads(OPTIONS_PATH.read_text())
        return AppConfig(**raw)

    # Development fallback – use environment variables or defaults
    return AppConfig(
        latitude=float(os.environ.get("FVE_LATITUDE", "50.0")),
        longitude=float(os.environ.get("FVE_LONGITUDE", "14.0")),
        tilt=int(os.environ.get("FVE_TILT", "30")),
        azimuth=int(os.environ.get("FVE_AZIMUTH", "180")),
        kwp=float(os.environ.get("FVE_KWP", "5.0")),
        ha_url=os.environ.get("HA_URL", "http://homeassistant.local:8123"),
        ha_sensor_power=os.environ.get(
            "HA_SENSOR_POWER", "sensor.solar_assistant_pv_power"
        ),
        ha_sensor_energy=os.environ.get(
            "HA_SENSOR_ENERGY", "sensor.solar_assistant_pv_energy_today"
        ),
        log_level=os.environ.get("LOG_LEVEL", "info"),
    )
