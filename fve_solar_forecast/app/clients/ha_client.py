"""Home Assistant REST API client."""

import logging
from typing import Optional

import httpx

from app.config import AppConfig

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 10


class HAClient:

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.config.effective_ha_token}",
            "Content-Type": "application/json",
        }

    def _api_url(self) -> str:
        base = self.config.ha_url.rstrip("/")
        return f"{base}/api"

    async def get_state(self, entity_id: str) -> dict:
        """
        GET /api/states/<entity_id>
        Returns the full state object from HA.
        Raises httpx.HTTPStatusError on non-2xx responses.
        """
        url = f"{self._api_url()}/states/{entity_id}"
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.get(url, headers=self._headers())
            response.raise_for_status()
        return response.json()

    async def get_pv_power_w(self) -> Optional[float]:
        """
        Return current PV power in Watts.
        Solar Assistant reports in W directly.
        Returns None on any error (unavailable sensor, network issue, etc.).
        """
        try:
            state = await self.get_state(self.config.ha_sensor_power)
            value = state.get("state", "unavailable")
            if value in ("unavailable", "unknown", ""):
                return None
            return float(value)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            logger.debug("get_pv_power_w failed: %s", exc)
            return None

    async def get_energy_kwh(self) -> Optional[float]:
        """
        Return cumulative PV energy counter in kWh.
        Solar Assistant reports daily energy in kWh.
        Returns None on any error.
        """
        try:
            state = await self.get_state(self.config.ha_sensor_energy)
            value = state.get("state", "unavailable")
            if value in ("unavailable", "unknown", ""):
                return None
            return float(value)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            logger.debug("get_energy_kwh failed: %s", exc)
            return None

    async def check_connection(self) -> bool:
        """
        Verify HA API connectivity.
        Returns True if /api/ responds with {"message": "API running."}.
        """
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                response = await client.get(
                    f"{self._api_url()}/", headers=self._headers()
                )
            return response.status_code == 200
        except httpx.HTTPError:
            return False
