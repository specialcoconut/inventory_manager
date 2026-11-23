"""Lookup of unknown manufacturer barcodes in OpenFoodFacts."""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

USER_AGENT = "inventory-manager-kiosk/1.0 (https://github.com/specialcoconut/inventory_manager)"


class ExternalProductLookup:
    """Best-effort product info for a barcode. Never raises – the kiosk must
    keep working when the internet is down."""

    def __init__(
        self,
        base_url: str,
        timeout: float = 5.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def lookup(self, barcode: str) -> Optional[dict]:
        """Return ``{"name", "brand", "quantity"}`` or ``None`` if unknown/unreachable."""
        path = f"api/v2/product/{quote(barcode, safe='')}"
        try:
            response = await self._client.get(path, params={"fields": "product_name,brands,quantity"})
        except httpx.HTTPError as exc:
            logger.warning("External barcode lookup failed for %s: %s", barcode, exc)
            return None
        if response.status_code != 200:
            return None
        try:
            body = response.json()
        except ValueError:
            return None
        if body.get("status") != 1 or not isinstance(body.get("product"), dict):
            return None
        product = body["product"]
        name = (product.get("product_name") or "").strip()
        if not name:
            return None
        return {
            "name": name,
            "brand": (product.get("brands") or "").split(",")[0].strip() or None,
            "quantity": (product.get("quantity") or "").strip() or None,
        }
