"""Thin async client for the Grocy REST API."""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import quote

import httpx

# Grocy stores "never expires" as this sentinel date.
NEVER_EXPIRES = "2999-12-31"


class GrocyError(Exception):
    """Raised when Grocy returns an error or cannot be reached."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class GrocyClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 10.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers={"GROCY-API-KEY": api_key, "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ helpers
    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._client.request(method, path.lstrip("/"), **kwargs)
        except httpx.HTTPError as exc:
            raise GrocyError(f"Cannot reach Grocy: {exc.__class__.__name__}: {exc}") from exc
        if response.status_code >= 400:
            message = response.text
            try:
                body = response.json()
                if isinstance(body, dict) and body.get("error_message"):
                    message = body["error_message"]
            except ValueError:
                pass
            raise GrocyError(message or f"HTTP {response.status_code}", response.status_code)
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise GrocyError("Grocy returned a non-JSON response", response.status_code) from exc

    # ------------------------------------------------------------------- system
    async def system_info(self) -> dict:
        return await self._request("GET", "system/info")

    # ------------------------------------------------------------------ objects
    async def get_objects(self, entity: str, query: Optional[list[str]] = None) -> list[dict]:
        params = [("query[]", q) for q in (query or [])]
        return await self._request("GET", f"objects/{entity}", params=params) or []

    async def create_object(self, entity: str, payload: dict) -> int:
        result = await self._request("POST", f"objects/{entity}", json=payload)
        return int(result["created_object_id"])

    async def update_object(self, entity: str, object_id: int, payload: dict) -> None:
        await self._request("PUT", f"objects/{entity}/{int(object_id)}", json=payload)

    async def product_groups(self) -> list[dict]:
        return await self.get_objects("product_groups")

    async def products(self) -> list[dict]:
        return await self.get_objects("products")

    async def locations(self) -> list[dict]:
        return await self.get_objects("locations")

    async def quantity_units(self) -> list[dict]:
        return await self.get_objects("quantity_units")

    # -------------------------------------------------------------------- stock
    async def current_stock(self) -> list[dict]:
        return await self._request("GET", "stock") or []

    async def product_details(self, product_id: int) -> dict:
        return await self._request("GET", f"stock/products/{int(product_id)}")

    async def product_entries(self, product_id: int) -> list[dict]:
        return await self._request("GET", f"stock/products/{int(product_id)}/entries") or []

    async def product_by_barcode(self, barcode: str) -> Optional[dict]:
        """Return product details for a barcode, or ``None`` if Grocy doesn't know it."""
        try:
            return await self._request("GET", f"stock/products/by-barcode/{quote(barcode, safe='')}")
        except GrocyError as exc:
            if exc.status_code in (400, 404):
                return None
            raise

    async def stock_entry_by_stock_id(self, stock_id: str) -> Optional[dict]:
        rows = await self.get_objects("stock", [f"stock_id={stock_id}"])
        for row in rows:
            if str(row.get("stock_id")) == stock_id:
                return row
        return None

    async def add_stock(
        self,
        product_id: int,
        amount: float = 1,
        best_before_date: Optional[str] = None,
        location_id: Optional[int] = None,
    ) -> list[dict]:
        payload: dict[str, Any] = {"amount": amount, "transaction_type": "purchase"}
        if best_before_date:
            payload["best_before_date"] = best_before_date
        if location_id is not None:
            payload["location_id"] = location_id
        return await self._request("POST", f"stock/products/{int(product_id)}/add", json=payload) or []

    async def consume(self, product_id: int, amount: float = 1, stock_entry_id: Optional[str] = None) -> list[dict]:
        payload: dict[str, Any] = {"amount": amount, "transaction_type": "consume", "spoiled": False}
        if stock_entry_id:
            payload["stock_entry_id"] = stock_entry_id
        return await self._request("POST", f"stock/products/{int(product_id)}/consume", json=payload) or []

    async def open_stock(self, product_id: int, amount: float = 1, stock_entry_id: Optional[str] = None) -> list[dict]:
        payload: dict[str, Any] = {"amount": amount}
        if stock_entry_id:
            payload["stock_entry_id"] = stock_entry_id
        return await self._request("POST", f"stock/products/{int(product_id)}/open", json=payload) or []

    async def transfer(
        self,
        product_id: int,
        location_id_from: int,
        location_id_to: int,
        stock_entry_id: Optional[str] = None,
        amount: float = 1,
    ) -> list[dict]:
        payload: dict[str, Any] = {
            "amount": amount,
            "location_id_from": location_id_from,
            "location_id_to": location_id_to,
        }
        if stock_entry_id:
            payload["stock_entry_id"] = stock_entry_id
        return await self._request("POST", f"stock/products/{int(product_id)}/transfer", json=payload) or []

    async def edit_stock_entry(self, entry_id: int, payload: dict) -> list[dict]:
        return await self._request("PUT", f"stock/entry/{int(entry_id)}", json=payload) or []

    # ----------------------------------------------------------------- products
    async def create_product(
        self,
        name: str,
        location_id: int,
        qu_id: int,
        product_group_id: Optional[int] = None,
        default_best_before_days: int = 0,
    ) -> int:
        base: dict[str, Any] = {
            "name": name,
            "location_id": location_id,
            "qu_id_purchase": qu_id,
            "qu_id_stock": qu_id,
            "default_best_before_days": default_best_before_days,
        }
        if product_group_id is not None:
            base["product_group_id"] = product_group_id
        try:
            # Grocy >= 4 requires the consume/price quantity units as well.
            return await self.create_object("products", {**base, "qu_id_consume": qu_id, "qu_id_price": qu_id})
        except GrocyError as exc:
            if exc.status_code != 400 or "qu_id_" not in exc.message:
                raise
            # Older Grocy versions don't know those columns.
            return await self.create_object("products", base)

    async def add_product_barcode(self, product_id: int, barcode: str) -> int:
        return await self.create_object("product_barcodes", {"product_id": int(product_id), "barcode": barcode})
