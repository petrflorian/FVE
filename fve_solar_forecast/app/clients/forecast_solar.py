"""Client for the forecast.solar free API."""

import logging
from datetime import datetime

import httpx

from app.config import AppConfig

logger = logging.getLogger(__name__)

BASE_URL = "https://api.forecast.solar/estimate"
# Free tier allows 12 requests/hour per IP – running every hour is safe.
REQUEST_TIMEOUT = 30


class ForecastSolarClient:

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def _url(self) -> str:
        c = self.config
        az = c.forecast_solar_azimuth  # converted from standard to forecast.solar convention
        return f"{BASE_URL}/{c.latitude}/{c.longitude}/{c.tilt}/{az}/{c.kwp}"

    async def fetch(self) -> list[dict]:
        """
        Fetch forecast from forecast.solar and return a flat list of slot dicts
        ready for database insertion.

        Returns list of:
          {for_date, slot_time, watts, wh_day}
        """
        url = self._url()
        logger.debug("Fetching forecast from %s", url)

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            try:
                response = await client.get(url)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "forecast.solar returned HTTP %s: %s",
                    exc.response.status_code,
                    exc.response.text[:200],
                )
                raise
            except httpx.RequestError as exc:
                logger.error("forecast.solar request failed: %s", exc)
                raise

        data = response.json()
        result = data.get("result", {})
        return self._parse(result)

    def _parse(self, result: dict) -> list[dict]:
        """Transform raw API result into flat slot list."""
        watts_by_slot: dict = result.get("watts", {})
        wh_day: dict = result.get("watt_hours_day", {})
        slots = []

        for slot_str, w in watts_by_slot.items():
            try:
                slot_dt = datetime.strptime(slot_str, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                logger.warning("Skipping unparseable slot time: %s", slot_str)
                continue

            day_str = slot_dt.date().isoformat()
            slots.append(
                {
                    "for_date": day_str,
                    "slot_time": slot_str,
                    "watts": float(w),
                    "wh_day": wh_day.get(day_str),
                }
            )

        logger.info("Parsed %d forecast slots across %d days", len(slots), len(wh_day))
        return slots
