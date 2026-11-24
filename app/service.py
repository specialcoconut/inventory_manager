"""Business logic that orchestrates Grocy calls for the kiosk."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from dataclasses import dataclass
from typing import Optional

from .categories import build_category_tree, default_group_names, split_group_name
from .config import Settings
from .grocy import NEVER_EXPIRES, GrocyClient, GrocyError
from .scanning import parse_scan

logger = logging.getLogger(__name__)

STOCK_ID_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,64}")

# "Use by days after opened" for long-life items: allowed range and the default
# used when a product hasn't been configured yet.
USE_BY_DAYS_MIN = 3
USE_BY_DAYS_MAX = 14
USE_BY_DAYS_DEFAULT = 7


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


def _parse_location_id(value) -> Optional[int]:
    """Grocy reports "no location" as null, '' or 0 depending on version/endpoint."""
    if value in (None, ""):
        return None
    loc = int(value)
    return loc if loc > 0 else None


def _is_long_life(major: Optional[str]) -> bool:
    """Products in the "Long Life" major category get circle-a-day use-by labels."""
    return bool(major) and re.sub(r"[^a-z0-9]", "", major.lower()) == "longlife"


def _use_by_days_of(product: dict) -> Optional[int]:
    try:
        days = int(float(product.get("default_best_before_days_after_open") or 0))
    except (TypeError, ValueError):
        days = 0
    return days if days > 0 else None


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
        location_id = _parse_location_id(product.get("location_id"))
        # "Move to <location> when opened": Grocy's move_on_open + default consume location.
        open_location_id = None
        if str(product.get("move_on_open") or "0") == "1":
            open_location_id = _parse_location_id(product.get("default_consume_location_id"))
        return {
            "id": int(product["id"]),
            "name": product.get("name", ""),
            "group_id": int(product["product_group_id"]) if product.get("product_group_id") not in (None, "") else None,
            "major": major,
            "sub": sub,
            "location_id": location_id,
            "location_name": locations.get(location_id) if location_id is not None else None,
            "open_location_id": open_location_id,
            "open_location_name": locations.get(open_location_id) if open_location_id is not None else None,
            "default_best_before_days": int(product.get("default_best_before_days") or 0),
            "long_life": _is_long_life(major),
            "use_by_days_after_open": _use_by_days_of(product),
            "min_stock_amount": float(product.get("min_stock_amount") or 0),
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
        stock, groups, locations, entries = await asyncio.gather(
            self.grocy.current_stock(),
            self.grocy.product_groups(),
            self.grocy.locations(),
            self.grocy.get_objects("stock"),
        )
        group_names = {int(g["id"]): g.get("name", "") for g in groups}
        location_names = _location_names(locations)
        # Where the stock actually is (entries may have been moved away from the product's default location)
        stored_at: dict[int, set[str]] = {}
        for entry in entries:
            loc_id = entry.get("location_id")
            if loc_id in (None, "") or float(entry.get("amount") or 0) <= 0:
                continue
            name = location_names.get(int(loc_id))
            if name:
                stored_at.setdefault(int(entry["product_id"]), set()).add(name)
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
                    "stock_locations": sorted(stored_at.get(summary["id"], set()), key=str.lower),
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
            loc_id = _parse_location_id(entry.get("location_id"))
            result.append(
                {
                    "stock_id": entry.get("stock_id"),
                    "amount": float(entry.get("amount") or 0),
                    "best_before_date": entry.get("best_before_date"),
                    "purchased_date": entry.get("purchased_date"),
                    "open": str(entry.get("open", "0")) == "1",
                    "location_id": loc_id,
                    "location_name": locations.get(loc_id) if loc_id is not None else None,
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
            "open": bool(entry.get("open")),
            "long_life": resolved.product["long_life"],
            "use_by_days_after_open": resolved.product["use_by_days_after_open"],
        }
        return resolved.product["id"], resolved.stock_id, item

    async def consume(self, product_id: int, stock_id: Optional[str]) -> None:
        await self.grocy.consume(product_id, amount=1, stock_entry_id=stock_id)
        await self._restock_check(product_id)

    async def _restock_check(self, product_id: int) -> None:
        """Have Grocy put the product on the shopping list when it fell below its
        minimum stock amount. Best effort – never fails the consume itself."""
        try:
            details = await self.grocy.product_details(product_id)
            product = details.get("product") or {}
            min_amount = float(product.get("min_stock_amount") or 0)
            if min_amount <= 0:
                return
            if float(details.get("stock_amount") or 0) < min_amount:
                await self.grocy.add_missing_products_to_shoppinglist()
        except GrocyError as exc:
            logger.warning("Shopping list check failed for product %s: %s", product_id, exc)

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
        amount: int = 1,
    ) -> dict:
        if amount < 1:
            raise ValueError("Amount must be at least 1")
        product = await self._product_by_id(product_id)
        bbd = self.best_before_for(product, best_before_date, no_expiry)
        if location_id is None:
            location_id = self.settings.grocy_default_location_id
        if location_id is None and product.get("location_id") not in (None, ""):
            location_id = int(product["location_id"])
        if location_id is None:
            # Last resort: without this Grocy creates the entry with no location at
            # all, which can never be transferred ("source location doesn't exist").
            locations = await self.grocy.locations()
            if locations:
                location_id = int(sorted(locations, key=lambda loc: int(loc["id"]))[0]["id"])
        rows = await self.grocy.add_stock(product_id, amount=amount, best_before_date=bbd, location_id=location_id)
        stock_id = next((r.get("stock_id") for r in rows if r.get("stock_id")), None)
        if not stock_id:
            raise GrocyError("Grocy did not return a stock_id for the new stock entry")
        groups = await self.grocy.product_groups()
        major, _ = self._category_of(product, {int(g["id"]): g.get("name", "") for g in groups})
        return {
            "stock_id": stock_id,
            "product_id": int(product["id"]),
            "product_name": product["name"],
            "best_before_date": bbd,
            "amount": amount,
            "long_life": _is_long_life(major),
        }

    # --------------------------------------------------------------------- move
    async def _set_entry_location(self, stock_id: str, location_id: int, amount: float) -> None:
        """Edit the entry in place – used when Grocy cannot transfer it."""
        raw = await self.grocy.stock_entry_by_stock_id(stock_id)
        if raw is None:
            raise NotFoundError(f"No stock entry {stock_id} in Grocy (already consumed?)")
        await self.grocy.edit_stock_entry(
            int(raw["id"]),
            {
                "amount": amount,
                "best_before_date": raw.get("best_before_date"),
                "purchased_date": raw.get("purchased_date"),
                "open": str(raw.get("open", "0")) == "1",
                "location_id": location_id,
            },
        )

    async def move(self, stock_id: str, location_id: int) -> dict:
        info, entry = await self.stock_entry(stock_id)
        if entry["location_id"] == location_id:
            return entry
        # Move the whole entry (not just one unit) so it isn't split across locations.
        amount = float(entry.get("amount") or 1)
        if entry["location_id"] is None:
            # Grocy cannot transfer from "nowhere" (stock added while the product had
            # no location): edit the entry in place instead.
            await self._set_entry_location(stock_id, location_id, amount)
        else:
            try:
                await self.grocy.transfer(
                    info["id"], entry["location_id"], location_id, stock_entry_id=stock_id, amount=amount
                )
            except GrocyError as exc:
                # e.g. the entry's location was deleted in Grocy ("source location
                # does not exist"): fall back to editing the entry in place.
                if exc.status_code != 400:
                    raise
                await self._set_entry_location(stock_id, location_id, amount)
        try:
            _, moved = await self.stock_entry(stock_id)
            return moved
        except NotFoundError:
            return {**entry, "location_id": location_id}

    # --------------------------------------------------------------------- open
    async def open_entry(self, stock_id: str) -> dict:
        """Mark one unit of a stock entry as opened.

        If the product is configured to move opened items (``move_on_open`` +
        ``default_consume_location_id`` in Grocy), the opened unit – and only
        that unit – ends up at that location; the rest stays where it is.

        For long-life products the opened unit's due date is set to today plus
        the product's "use by days after opened" (default 7 days).
        """
        if not STOCK_ID_PATTERN.fullmatch(stock_id):
            raise NotFoundError(f"No stock entry {stock_id} in Grocy (already consumed?)")
        rows = [
            r
            for r in await self.grocy.get_objects("stock", [f"stock_id={stock_id}"])
            if str(r.get("stock_id")) == stock_id and float(r.get("amount") or 0) > 0
        ]
        if not rows:
            raise NotFoundError(f"No stock entry {stock_id} in Grocy (already consumed?)")
        product_id = int(rows[0]["product_id"])
        if all(str(r.get("open", "0")) == "1" for r in rows):
            raise ConflictError("All items of this entry are already open")
        product = await self._product_by_id(product_id)
        groups = await self.grocy.product_groups()
        major, _ = self._category_of(product, {int(g["id"]): g.get("name", "") for g in groups})
        long_life = _is_long_life(major)
        opened_before = {int(r["id"]) for r in rows if str(r.get("open", "0")) == "1"}

        await self.grocy.open_stock(product_id, 1, stock_id)

        target = None
        if str(product.get("move_on_open") or "0") == "1":
            target = _parse_location_id(product.get("default_consume_location_id"))
        use_by_date = None
        if long_life:
            days = _use_by_days_of(product) or USE_BY_DAYS_DEFAULT
            days = max(USE_BY_DAYS_MIN, min(USE_BY_DAYS_MAX, days))
            use_by_date = (dt.date.today() + dt.timedelta(days=days)).isoformat()
        rows_after = await self.grocy.get_objects("stock", [f"stock_id={stock_id}"])
        opened_row = next(
            (
                r
                for r in rows_after
                if str(r.get("open", "0")) == "1" and int(r["id"]) not in opened_before and float(r.get("amount") or 0) > 0
            ),
            None,
        )
        if opened_row is not None:
            current_loc = _parse_location_id(opened_row.get("location_id"))
            desired_loc = target if target is not None else current_loc
            desired_bbd = use_by_date or opened_row.get("best_before_date")
            if desired_loc != current_loc or (desired_bbd or "")[:10] != (opened_row.get("best_before_date") or "")[:10]:
                # Grocy versions without move-on-open / due-days-after-open support
                # leave the opened unit untouched: edit just that row (it has its
                # own id after the split).
                await self.grocy.edit_stock_entry(
                    int(opened_row["id"]),
                    {
                        "amount": float(opened_row.get("amount") or 1),
                        "best_before_date": desired_bbd,
                        "purchased_date": opened_row.get("purchased_date"),
                        "open": True,
                        "location_id": desired_loc,
                    },
                )
                opened_row = {**opened_row, "location_id": desired_loc, "best_before_date": desired_bbd}
        locations = _location_names(await self.grocy.locations())
        loc_id = _parse_location_id(opened_row.get("location_id")) if opened_row else None
        return {
            "stock_id": stock_id,
            "product_id": product_id,
            "product_name": product.get("name", ""),
            "moved": target is not None and loc_id == target,
            "location_id": loc_id,
            "location_name": locations.get(loc_id) if loc_id is not None else None,
            "long_life": long_life,
            "use_by_date": use_by_date if opened_row is not None else None,
            "best_before_date": opened_row.get("best_before_date") if opened_row else None,
        }

    # ------------------------------------------------------------------ product
    async def create_product(
        self,
        name: str,
        product_group_id: Optional[int],
        barcode: Optional[str],
        default_best_before_days: Optional[int],
        location_id: Optional[int],
        min_stock_amount: Optional[int] = None,
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
            min_stock_amount=min_stock_amount if min_stock_amount else None,
        )
        if barcode:
            await self.grocy.add_product_barcode(product_id, barcode)
        product = await self._product_by_id(product_id)
        return await self._product_info(product)

    async def create_location(self, name: str) -> dict:
        name = name.strip()
        if not name:
            raise ValueError("Location name is required")
        existing = await self.grocy.locations()
        if any((loc.get("name") or "").strip().lower() == name.lower() for loc in existing):
            raise ConflictError(f"Location {name} already exists")
        location_id = await self.grocy.create_object("locations", {"name": name})
        return {"id": location_id, "name": name}

    async def create_category(self, name: str) -> dict:
        name = name.strip()
        if not name:
            raise ValueError("Category name is required")
        existing = await self.grocy.product_groups()
        if any((g.get("name") or "").strip().lower() == name.lower() for g in existing):
            raise ConflictError(f"Category {name} already exists")
        group_id = await self.grocy.create_object("product_groups", {"name": name})
        return {"id": group_id, "name": name}

    async def seed_default_categories(self) -> dict:
        """Create any missing groups from the suggested category tree (idempotent)."""
        existing = await self.grocy.product_groups()
        existing_names = {(g.get("name") or "").strip().lower() for g in existing}
        created = []
        names = default_group_names(self.settings.category_delimiter)
        for name in names:
            if name.lower() in existing_names:
                continue
            await self.grocy.create_object("product_groups", {"name": name})
            created.append(name)
        return {"created": created, "skipped": len(names) - len(created)}

    async def set_product_category(self, product_id: int, product_group_id: Optional[int]) -> dict:
        product = await self._product_by_id(product_id)
        if product_group_id is not None:
            groups = await self.grocy.product_groups()
            if not any(int(g["id"]) == product_group_id for g in groups):
                raise NotFoundError(f"Product group #{product_group_id} not found in Grocy")
        await self.grocy.update_object("products", int(product["id"]), {"product_group_id": product_group_id})
        return await self._product_info(await self._product_by_id(product_id))

    async def set_open_behavior(self, product_id: int, location_id: Optional[int]) -> dict:
        """Configure whether opening one item of this product moves it to ``location_id``."""
        product = await self._product_by_id(product_id)
        if location_id is None:
            await self.grocy.update_object("products", int(product["id"]), {"move_on_open": 0})
        else:
            locations = await self.grocy.locations()
            if not any(int(loc["id"]) == location_id for loc in locations):
                raise NotFoundError(f"Location #{location_id} not found in Grocy")
            await self.grocy.update_object(
                "products", int(product["id"]), {"move_on_open": 1, "default_consume_location_id": location_id}
            )
        return await self._product_info(await self._product_by_id(product_id))

    async def set_min_stock(self, product_id: int, amount: int) -> dict:
        """Set the minimum stock amount; Grocy puts the product on the shopping list below it."""
        if amount < 0:
            raise ValueError("Minimum stock amount cannot be negative")
        product = await self._product_by_id(product_id)
        await self.grocy.update_object("products", int(product["id"]), {"min_stock_amount": amount})
        return await self._product_info(await self._product_by_id(product_id))

    async def set_use_by_days(self, product_id: int, days: Optional[int]) -> dict:
        """Set "use by days after opened" for long-life items (None resets to the default)."""
        if days is not None and not (USE_BY_DAYS_MIN <= days <= USE_BY_DAYS_MAX):
            raise ValueError(
                f"Use by days after opened must be between {USE_BY_DAYS_MIN} and {USE_BY_DAYS_MAX}"
            )
        product = await self._product_by_id(product_id)
        await self.grocy.update_object(
            "products", int(product["id"]), {"default_best_before_days_after_open": days or 0}
        )
        return await self._product_info(await self._product_by_id(product_id))
