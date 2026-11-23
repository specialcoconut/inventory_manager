"""Business logic that orchestrates Grocy calls for the kiosk."""

from __future__ import annotations

import asyncio
import datetime as dt
import re
from dataclasses import dataclass
from typing import Optional

from .categories import build_category_tree, split_group_name
from .config import Settings
from .grocy import NEVER_EXPIRES, GrocyClient, GrocyError
from .scanning import parse_scan


STOCK_ID_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,64}")


class NotFoundError(Exception):
    pass


class ConflictError(Exception):
    pass


@dataclass
class Resolved:
    kind: str  # "stock" | "product"
    product: dict
    entries: list[dict]
    stock_id: Optional[str] = None


def _location_names(locations: list[dict]) -> dict[int, str]:
    return {int(loc["id"]): loc.get("name", f"#{loc['id']}") for loc in locations}


def _entry_sort_key(entry: dict):
    # Grocy's default consume order: opened first, then soonest due, then oldest purchase.
    return (
        0 if str(entry.get("open", "0")) == "1" else 1,
        entry.get("best_before_date") or NEVER_EXPIRES,
        entry.get("purchased_date") or "",
    )


class InventoryService:
    def __init__(self, grocy: GrocyClient, settings: Settings):
        self.grocy = grocy
        self.settings = settings

    # ------------------------------------------------------------------ catalog
    def _category_of(self, product: dict, groups: dict[int, str]) -> tuple[str, Optional[str]]:
        group_id = product.get("product_group_id")
        name = groups.get(int(group_id)) if group_id not in (None, "") else None
        return split_group_name(name, self.settings.category_delimiter)

    def _product_summary(self, product: dict, groups: dict[int, str], locations: dict[int, str]) -> dict:
        major, sub = self._category_of(product, groups)
        location_id = product.get("location_id")
        return {
            "id": int(product["id"]),
            "name": product.get("name", ""),
            "group_id": int(product["product_group_id"]) if product.get("product_group_id") not in (None, "") else None,
            "major": major,
            "sub": sub,
            "location_id": int(location_id) if location_id not in (None, "") else None,
            "location_name": locations.get(int(location_id)) if location_id not in (None, "") else None,
            "default_best_before_days": int(product.get("default_best_before_days") or 0),
        }

    async def catalog(self) -> dict:
        groups, products, locations = await asyncio.gather(
            self.grocy.product_groups(), self.grocy.products(), self.grocy.locations()
        )
        group_names = {int(g["id"]): g.get("name", "") for g in groups}
        location_names = _location_names(locations)
        active = [p for p in products if str(p.get("active", "1")) != "0"]
        summaries = sorted(
            (self._product_summary(p, group_names, location_names) for p in active), key=lambda p: p["name"].lower()
        )
        tree = build_category_tree(groups, self.settings.category_delimiter)
        if any(p["group_id"] is None for p in summaries):
            tree.append({"name": "Uncategorized", "group_ids": [], "subcategories": []})
        return {
            "categories": tree,
            "groups": [{"id": int(g["id"]), "name": g.get("name", "")} for g in groups],
            "products": summaries,
            "locations": [{"id": k, "name": v} for k, v in sorted(location_names.items(), key=lambda kv: kv[1].lower())],
        }

    async def inventory(self) -> dict:
        stock, groups, locations = await asyncio.gather(
            self.grocy.current_stock(), self.grocy.product_groups(), self.grocy.locations()
        )
        group_names = {int(g["id"]): g.get("name", "") for g in groups}
        location_names = _location_names(locations)
        items = []
        for row in stock:
            product = row.get("product") or {"id": row["product_id"], "name": f"Product #{row['product_id']}"}
            summary = self._product_summary(product, group_names, location_names)
            amount = float(row.get("amount") or 0)
            if amount <= 0:
                continue
            items.append(
                {
                    **summary,
                    "product_id": summary["id"],
                    "amount": amount,
                    "best_before_date": row.get("best_before_date"),
                }
            )
        items.sort(key=lambda i: i["name"].lower())
        return {"items": items, "total_products": len(items), "total_amount": sum(i["amount"] for i in items)}

    # ---------------------------------------------------------------- lookups
    async def _enriched_entries(self, product_id: int, locations: Optional[dict[int, str]] = None) -> list[dict]:
        if locations is None:
            entries, raw_locations = await asyncio.gather(
                self.grocy.product_entries(product_id), self.grocy.locations()
            )
            locations = _location_names(raw_locations)
        else:
            entries = await self.grocy.product_entries(product_id)
        result = []
        for entry in sorted(entries, key=_entry_sort_key):
            loc_id = entry.get("location_id")
            result.append(
                {
                    "stock_id": entry.get("stock_id"),
                    "amount": float(entry.get("amount") or 0),
                    "best_before_date": entry.get("best_before_date"),
                    "purchased_date": entry.get("purchased_date"),
                    "open": str(entry.get("open", "0")) == "1",
                    "location_id": int(loc_id) if loc_id not in (None, "") else None,
                    "location_name": locations.get(int(loc_id)) if loc_id not in (None, "") else None,
                }
            )
        return result

    async def _product_by_id(self, product_id: int) -> dict:
        try:
            details = await self.grocy.product_details(product_id)
        except GrocyError as exc:
            if exc.status_code in (400, 404):
                raise NotFoundError(f"Product #{product_id} not found in Grocy") from exc
            raise
        return details["product"]

    async def _product_info(self, product: dict) -> dict:
        groups, locations = await asyncio.gather(self.grocy.product_groups(), self.grocy.locations())
        return self._product_summary(product, {int(g["id"]): g.get("name", "") for g in groups}, _location_names(locations))

    async def resolve(self, code: str) -> Resolved:
        scan = parse_scan(code, self.settings.barcode_prefix)
        if not scan.value:
            raise NotFoundError("Empty barcode")

        stock_id: Optional[str] = None
        product: Optional[dict] = None
        if scan.kind == "stock":
            if not STOCK_ID_PATTERN.fullmatch(scan.value):
                raise NotFoundError(f"Invalid stock label {scan.raw}")
            entry = await self.grocy.stock_entry_by_stock_id(scan.value)
            if entry is None:
                raise NotFoundError(f"No stock entry {scan.value} in Grocy (already consumed?)")
            stock_id = scan.value
            product = await self._product_by_id(int(entry["product_id"]))
        elif scan.kind == "product":
            product = await self._product_by_id(int(scan.value))
        else:
            details = await self.grocy.product_by_barcode(scan.value)
            if details is not None:
                product = details["product"]
            else:
                entry = None
                if STOCK_ID_PATTERN.fullmatch(scan.value):
                    entry = await self.grocy.stock_entry_by_stock_id(scan.value)
                if entry is None:
                    raise NotFoundError(f"Unknown barcode {scan.value}")
                stock_id = scan.value
                product = await self._product_by_id(int(entry["product_id"]))

        info = await self._product_info(product)
        entries = await self._enriched_entries(info["id"])
        return Resolved(kind="stock" if stock_id else "product", product=info, entries=entries, stock_id=stock_id)

    async def product_stock(self, product_id: int) -> Resolved:
        product = await self._product_by_id(product_id)
        info = await self._product_info(product)
        return Resolved(kind="product", product=info, entries=await self._enriched_entries(product_id))

    async def stock_entry(self, stock_id: str) -> tuple[dict, dict]:
        """Return (product summary, enriched entry) for a stock_id."""
        entry = None
        if STOCK_ID_PATTERN.fullmatch(stock_id):
            entry = await self.grocy.stock_entry_by_stock_id(stock_id)
        if entry is None:
            raise NotFoundError(f"No stock entry {stock_id} in Grocy (already consumed?)")
        product = await self._product_by_id(int(entry["product_id"]))
        info = await self._product_info(product)
        entries = await self._enriched_entries(info["id"])
        match = next((e for e in entries if e["stock_id"] == stock_id), None)
        if match is None:
            raise NotFoundError(f"No stock entry {stock_id} in Grocy (already consumed?)")
        return info, match

    # ------------------------------------------------------------------ consume
    async def prepare_consume(self, code: str) -> tuple[int, Optional[str], dict]:
        """Resolve a scan into (product_id, stock_id, display item) without touching stock."""
        resolved = await self.resolve(code)
        if resolved.stock_id:
            entry = next((e for e in resolved.entries if e["stock_id"] == resolved.stock_id), None)
            if entry is None:
                raise NotFoundError(f"No stock entry {resolved.stock_id} in Grocy (already consumed?)")
        else:
            if not resolved.entries:
                raise ConflictError(f"{resolved.product['name']} is not in stock")
            entry = resolved.entries[0]
        item = {
            "product_id": resolved.product["id"],
            "product_name": resolved.product["name"],
            "best_before_date": entry.get("best_before_date"),
            "location_name": entry.get("location_name"),
            "stock_id": entry.get("stock_id"),
            "specific_entry": bool(resolved.stock_id),
        }
        return resolved.product["id"], resolved.stock_id, item

    async def consume(self, product_id: int, stock_id: Optional[str]) -> None:
        await self.grocy.consume(product_id, amount=1, stock_entry_id=stock_id)

    # ---------------------------------------------------------------------- add
    def best_before_for(self, product: dict, requested: Optional[str], no_expiry: bool) -> str:
        if no_expiry:
            return NEVER_EXPIRES
        if requested:
            try:
                return dt.date.fromisoformat(requested[:10]).isoformat()
            except ValueError as exc:
                raise ValueError(f"Invalid best before date {requested!r}") from exc
        days = int(product.get("default_best_before_days") or 0)
        if days == -1:
            return NEVER_EXPIRES
        if days <= 0:
            days = self.settings.grocy_default_best_before_days
        return (dt.date.today() + dt.timedelta(days=days)).isoformat()

    async def add_stock(
        self,
        product_id: int,
        best_before_date: Optional[str],
        no_expiry: bool,
        location_id: Optional[int],
    ) -> dict:
        product = await self._product_by_id(product_id)
        bbd = self.best_before_for(product, best_before_date, no_expiry)
        if location_id is None:
            location_id = self.settings.grocy_default_location_id
        if location_id is None and product.get("location_id") not in (None, ""):
            location_id = int(product["location_id"])
        rows = await self.grocy.add_stock(product_id, amount=1, best_before_date=bbd, location_id=location_id)
        stock_id = next((r.get("stock_id") for r in rows if r.get("stock_id")), None)
        if not stock_id:
            raise GrocyError("Grocy did not return a stock_id for the new stock entry")
        return {"stock_id": stock_id, "product_id": int(product["id"]), "product_name": product["name"], "best_before_date": bbd}

    # --------------------------------------------------------------------- move
    async def move(self, stock_id: str, location_id: int) -> dict:
        info, entry = await self.stock_entry(stock_id)
        if entry["location_id"] == location_id:
            return entry
        await self.grocy.transfer(info["id"], entry["location_id"], location_id, stock_entry_id=stock_id)
        try:
            _, moved = await self.stock_entry(stock_id)
            return moved
        except NotFoundError:
            return {**entry, "location_id": location_id}

    # ------------------------------------------------------------------ product
    async def create_product(
        self,
        name: str,
        product_group_id: Optional[int],
        barcode: Optional[str],
        default_best_before_days: Optional[int],
        location_id: Optional[int],
    ) -> dict:
        name = name.strip()
        if not name:
            raise ValueError("Product name is required")
        if barcode:
            existing = await self.grocy.product_by_barcode(barcode)
            if existing is not None:
                raise ConflictError(f"Barcode {barcode} already belongs to {existing['product']['name']}")
        location_id = location_id or self.settings.grocy_default_location_id
        if location_id is None:
            locations = await self.grocy.locations()
            if not locations:
                raise ValueError("Grocy has no locations – create one first")
            location_id = int(locations[0]["id"])
        qu_id = self.settings.grocy_default_quantity_unit_id
        if qu_id is None:
            units = await self.grocy.quantity_units()
            if not units:
                raise ValueError("Grocy has no quantity units – create one first")
            qu_id = int(sorted(units, key=lambda u: int(u["id"]))[0]["id"])
        product_id = await self.grocy.create_product(
            name=name,
            location_id=location_id,
            qu_id=qu_id,
            product_group_id=product_group_id,
            default_best_before_days=default_best_before_days or 0,
        )
        if barcode:
            await self.grocy.add_product_barcode(product_id, barcode)
        product = await self._product_by_id(product_id)
        return await self._product_info(product)
