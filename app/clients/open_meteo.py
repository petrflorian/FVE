"""
Open-Meteo client for weather context data.

Fetches hourly cloud cover and temperature for the panel location.
Used in Phase 3 calibration for weather-aware correction.
Also stores actuals for post-hoc analysis (why was the forecast off?).

API reference: https://open-meteo.com/en/docs
"""

import logging
from datetime import date, timedelta

import httpx

from app.config import AppConfig

logger = logging.getLogger(__name__)

BASE_URL = "https://api.open-meteo.com/v1/forecast"
REQUEST_TIMEOUT = 20


class OpenMeteoClient:

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    async def fetch_hourly(
        self,
        start_date: date,
        end_date: date,
    ) -> list[dict]:
        """
        Fetch hourly weather data for the panel location.
        Returns list of {datetime, cloud_cover_pct, temperature_c, shortwave_radiation_wm2}.
        """
        params = {
            "latitude": self.config.latitude,
            "longitude": self.config.longitude,
            "hourly": ",".join(
                [
                    "cloud_cover",
                    "temperature_2m",
                    "shortwave_radiation",
                    "direct_normal_irradiance",
                ]
            ),
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "timezone": "UTC",
        }

        logger.debug("Fetching Open-Meteo weather %s → %s", start_date, end_date)

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            try:
                response = await client.get(BASE_URL, params=params)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.error("Open-Meteo HTTP %s: %s", exc.response.status_code, exc.response.text[:200])
                raise
            except httpx.RequestError as exc:
                logger.error("Open-Meteo request failed: %s", exc)
                raise

        data = response.json()
        return self._parse(data)

    async def fetch_today_and_tomorrow(self) -> list[dict]:
        today = date.today()
        return await self.fetch_hourly(today, today + timedelta(days=1))

    def _parse(self, data: dict) -> list[dict]:
        hourly = data.get("hourly", {})
        times = hourly.get("time", [])
        cloud = hourly.get("cloud_cover", [])
        temp = hourly.get("temperature_2m", [])
        ghi = hourly.get("shortwave_radiation", [])
        dni = hourly.get("direct_normal_irradiance", [])

        result = []
        for i, ts in enumerate(times):
            result.append(
                {
                    "datetime": ts.replace("T", " ") + ":00",  # → "YYYY-MM-DD HH:MM:SS"
                    "cloud_cover_pct": cloud[i] if i < len(cloud) else None,
                    "temperature_c": temp[i] if i < len(temp) else None,
                    "ghi_wm2": ghi[i] if i < len(ghi) else None,
                    "dni_wm2": dni[i] if i < len(dni) else None,
                }
            )

        logger.info("Open-Meteo: parsed %d hourly records", len(result))
        return result
