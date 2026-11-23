"""In-memory stand-in for the Grocy REST API, used via httpx.MockTransport."""

from __future__ import annotations

import json
import uuid
from urllib.parse import unquote

import httpx


class FakeGrocy:
    def __init__(self):
        self.requests: list[tuple[str, str, dict | None]] = []
        self.product_groups = [
            {"id": 1, "name": "Dairy/Butter"},
            {"id": 2, "name": "Dairy/Milk"},
            {"id": 3, "name": "Meat/Chicken"},
            {"id": 4, "name": "Long Life/Sauces"},
            {"id": 5, "name": "Dry"},
        ]
        self.locations = [{"id": 1, "name": "Fridge"}, {"id": 2, "name": "Freezer"}, {"id": 3, "name": "Pantry"}]
        self.quantity_units = [{"id": 2, "name": "Pack"}, {"id": 1, "name": "Piece"}]
        self.products = [
            {"id": 10, "name": "Butter", "product_group_id": 1, "location_id": 1, "default_best_before_days": 30},
            {"id": 11, "name": "Whole Milk", "product_group_id": 2, "location_id": 1, "default_best_before_days": 0},
            {"id": 12, "name": "Chicken Thighs", "product_group_id": 3, "location_id": 2, "default_best_before_days": 3},
            {"id": 13, "name": "Rice", "product_group_id": None, "location_id": 3, "default_best_before_days": -1},
        ]
        self.barcodes = [{"product_id": 10, "barcode": "4006040052555"}]
        self.stock = [
            self._entry(10, "2030-01-10", 1, "butter1"),
            self._entry(10, "2030-01-05", 1, "butter2"),
            self._entry(12, "2029-12-01", 2, "chicken1"),
        ]
        self.next_id = 100
        self.fail_consume: str | None = None
        self.v3 = False  # simulate an old Grocy without qu_id_consume / qu_id_price
        self.down = False  # simulate Grocy being unreachable

    @staticmethod
    def _entry(product_id, bbd, location_id, stock_id, amount=1):
        return {
            "id": abs(hash(stock_id)) % 100000,
            "product_id": product_id,
            "amount": amount,
            "best_before_date": bbd,
            "purchased_date": "2026-11-01",
            "stock_id": stock_id,
            "location_id": location_id,
            "open": 0,
        }

    # ------------------------------------------------------------------ helpers
    def product(self, product_id):
        return next((p for p in self.products if int(p["id"]) == int(product_id)), None)

    def entries(self, product_id):
        return [e for e in self.stock if int(e["product_id"]) == int(product_id)]

    def _details(self, product):
        entries = sorted(self.entries(product["id"]), key=lambda e: e["best_before_date"])
        return {
            "product": product,
            "stock_amount": sum(e["amount"] for e in entries),
            "next_due_date": entries[0]["best_before_date"] if entries else None,
            "location": next((loc for loc in self.locations if loc["id"] == product["location_id"]), None),
        }

    @staticmethod
    def _json(data, status=200):
        return httpx.Response(status, json=data)

    @staticmethod
    def _error(message, status=400):
        return httpx.Response(status, json={"error_message": message})

    # ------------------------------------------------------------------ routing
    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("Connection refused", request=request)
        assert request.headers.get("GROCY-API-KEY") == "test-key"
        path = unquote(request.url.path)
        assert path.startswith("/api/")
        path = path[len("/api/"):]
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, path, body))
        parts = path.split("/")

        if request.method == "GET":
            if path == "system/info":
                return self._json({"grocy_version": {"Version": "4.2.0"}})
            if path == "stock":
                rows = []
                for product in self.products:
                    entries = self.entries(product["id"])
                    if entries:
                        rows.append(
                            {
                                "product_id": product["id"],
                                "amount": sum(e["amount"] for e in entries),
                                "best_before_date": min(e["best_before_date"] for e in entries),
                                "product": product,
                            }
                        )
                return self._json(rows)
            if parts[0] == "objects":
                entity = parts[1]
                data = {
                    "product_groups": self.product_groups,
                    "products": self.products,
                    "locations": self.locations,
                    "quantity_units": self.quantity_units,
                    "product_barcodes": self.barcodes,
                    "stock": self.stock,
                }[entity]
                for q in request.url.params.get_list("query[]"):
                    field, _, value = q.partition("=")
                    data = [d for d in data if str(d.get(field)) == value]
                return self._json(data)
            if parts[:3] == ["stock", "products", "by-barcode"]:
                code = parts[3]
                match = next((b for b in self.barcodes if b["barcode"] == code), None)
                if match is None:
                    return self._error(f"No product with barcode {code} found")
                return self._json(self._details(self.product(match["product_id"])))
            if parts[:2] == ["stock", "products"] and len(parts) == 3:
                product = self.product(parts[2])
                return self._json(self._details(product)) if product else self._error("Product does not exist")
            if parts[:2] == ["stock", "products"] and parts[3] == "entries":
                return self._json(self.entries(parts[2]))

        if request.method == "POST":
            if parts[:2] == ["stock", "products"] and parts[3] == "add":
                stock_id = uuid.uuid4().hex[:13]
                product = self.product(parts[2])
                if product is None:
                    return self._error("Product does not exist")
                entry = self._entry(
                    product["id"], body.get("best_before_date", "2026-11-23"),
                    body.get("location_id", product["location_id"]), stock_id, body["amount"],
                )
                self.stock.append(entry)
                return self._json([{"id": 1, "product_id": product["id"], "stock_id": stock_id, "transaction_type": "purchase"}])
            if parts[:2] == ["stock", "products"] and parts[3] == "consume":
                if self.fail_consume:
                    return self._error(self.fail_consume)
                entries = sorted(self.entries(parts[2]), key=lambda e: e["best_before_date"])
                if body.get("stock_entry_id"):
                    entries = [e for e in entries if e["stock_id"] == body["stock_entry_id"]]
                if not entries:
                    return self._error("Amount to be consumed cannot be > current stock amount")
                entry = entries[0]
                entry["amount"] -= body["amount"]
                if entry["amount"] <= 0:
                    self.stock.remove(entry)
                return self._json([{"stock_id": entry["stock_id"], "transaction_type": "consume"}])
            if parts[:2] == ["stock", "products"] and parts[3] == "transfer":
                from_id = body["location_id_from"]
                if not any(int(loc["id"]) == int(from_id or 0) for loc in self.locations):
                    return self._error("Source location does not exist")
                entry = next(e for e in self.stock if e["stock_id"] == body["stock_entry_id"])
                entry["location_id"] = body["location_id_to"]
                return self._json([{"stock_id": entry["stock_id"], "transaction_type": "transfer_to"}])
            if parts[0] == "objects":
                entity = parts[1]
                if entity == "products":
                    if self.v3 and "qu_id_consume" in body:
                        return self._error("SQLSTATE[HY000]: General error: 1 table products has no column named qu_id_consume")
                    if any(p["name"] == body["name"] for p in self.products):
                        return self._error("UNIQUE constraint failed: products.name")
                    self.next_id += 1
                    self.products.append({"id": self.next_id, **body})
                    return self._json({"created_object_id": self.next_id})
                if entity == "product_barcodes":
                    self.barcodes.append(body)
                    return self._json({"created_object_id": len(self.barcodes)})
                if entity in ("locations", "product_groups"):
                    store = self.locations if entity == "locations" else self.product_groups
                    self.next_id += 1
                    store.append({"id": self.next_id, **body})
                    return self._json({"created_object_id": self.next_id})

        if request.method == "PUT":
            if parts[:2] == ["stock", "entry"] and len(parts) == 3:
                entry = next((e for e in self.stock if str(e["id"]) == parts[2]), None)
                if entry is None:
                    return self._error("Stock entry does not exist")
                entry.update({k: v for k, v in body.items()
                              if k in ("amount", "best_before_date", "purchased_date", "location_id")})
                if "open" in body:
                    entry["open"] = 1 if body["open"] else 0
                return self._json([{"id": entry["id"], "stock_id": entry["stock_id"]}])
            if parts[0] == "objects" and parts[1] == "products" and len(parts) == 3:
                product = self.product(parts[2])
                if product is None:
                    return self._error("Product does not exist", 404)
                product.update(body)
                return httpx.Response(204)

        return self._error(f"Unhandled {request.method} {path}", 404)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
