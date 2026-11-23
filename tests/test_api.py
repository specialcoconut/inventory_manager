import datetime as dt
import time

from PIL import Image

from app.printer import PrinterError


def test_config_and_static_assets(client):
    cfg = client.get("/api/config").json()
    assert cfg["consume_countdown_seconds"] == 1
    assert cfg["barcode_prefix"] == "grcy:s:"
    assert "CONSUME" in client.get("/").text
    manifest = client.get("/manifest.json")
    assert manifest.json()["display"] == "fullscreen"
    sw = client.get("/service-worker.js")
    assert sw.status_code == 200 and "javascript" in sw.headers["content-type"]
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/icons/icon-512.png").status_code == 200
    assert client.get("/api/nope").status_code == 404


def test_health_reports_grocy_and_printer(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["grocy"] == {"ok": True, "version": "4.2.0"}
    assert body["printer"]["ok"] is True


def test_health_degraded_when_grocy_down(client, fake_grocy):
    fake_grocy.down = True
    body = client.get("/api/health").json()
    assert body["status"] == "degraded"
    assert body["grocy"]["ok"] is False
    assert "Cannot reach Grocy" in body["grocy"]["error"]


def test_catalog_two_tier_categories(client):
    body = client.get("/api/catalog").json()
    names = [c["name"] for c in body["categories"]]
    assert names == ["Dairy", "Dry", "Long Life", "Meat", "Uncategorized"]
    dairy = body["categories"][0]
    assert [s["name"] for s in dairy["subcategories"]] == ["Butter", "Milk"]
    butter = next(p for p in body["products"] if p["name"] == "Butter")
    assert (butter["major"], butter["sub"], butter["location_name"]) == ("Dairy", "Butter", "Fridge")
    rice = next(p for p in body["products"] if p["name"] == "Rice")
    assert (rice["major"], rice["sub"]) == ("Uncategorized", None)
    assert [loc["name"] for loc in body["locations"]] == ["Freezer", "Fridge", "Pantry"]


def test_inventory_lists_total_current_stock(client):
    body = client.get("/api/inventory").json()
    assert body["total_products"] == 2
    assert body["total_amount"] == 3
    butter = next(i for i in body["items"] if i["name"] == "Butter")
    assert butter["amount"] == 2
    assert butter["best_before_date"] == "2030-01-05"
    assert butter["major"] == "Dairy"
    assert butter["stock_locations"] == ["Fridge"]


def test_inventory_locations_follow_moved_stock(client):
    assert client.post("/api/stock/chicken1/move", json={"location_id": 1}).status_code == 200
    chicken = next(i for i in client.get("/api/inventory").json()["items"] if i["name"] == "Chicken Thighs")
    assert chicken["stock_locations"] == ["Fridge"]


def test_lookup_stock_label_highlights_entry(client):
    body = client.get("/api/lookup", params={"code": "grcy:s:butter1"}).json()
    assert body["kind"] == "stock"
    assert body["stock_id"] == "butter1"
    assert body["product"]["name"] == "Butter"
    # sorted soonest-expiry first, with location names
    assert [e["stock_id"] for e in body["entries"]] == ["butter2", "butter1"]
    assert body["entries"][0]["location_name"] == "Fridge"


def test_lookup_oem_barcode_and_raw_stock_id(client):
    assert client.get("/api/lookup", params={"code": "4006040052555"}).json()["kind"] == "product"
    raw = client.get("/api/lookup", params={"code": "chicken1"}).json()
    assert raw["kind"] == "stock" and raw["product"]["name"] == "Chicken Thighs"
    assert client.get("/api/lookup", params={"code": "grcy:p:11"}).json()["product"]["name"] == "Whole Milk"


def test_lookup_unknown(client):
    r = client.get("/api/lookup", params={"code": "0000000000"})
    assert r.status_code == 404
    assert client.get("/api/lookup", params={"code": "grcy:s:gone"}).status_code == 404
    assert client.get("/api/lookup", params={"code": "grcy:s:bad id!"}).status_code == 404


def _consume_calls(fake_grocy):
    return [r for r in fake_grocy.requests if r[0] == "POST" and r[1].endswith("/consume")]


def test_consume_waits_for_countdown_then_removes(client, fake_grocy):
    r = client.post("/api/consume", json={"code": "grcy:s:butter1"})
    assert r.status_code == 200
    pending = r.json()["pending"]
    assert pending["status"] == "pending"
    assert pending["item"]["product_name"] == "Butter"
    assert pending["item"]["location_name"] == "Fridge"
    assert pending["item"]["stock_id"] == "butter1"
    assert _consume_calls(fake_grocy) == []  # nothing removed yet

    done = client.post(f"/api/consume/{pending['id']}/commit").json()
    assert done["status"] == "committed"
    assert _consume_calls(fake_grocy) == [
        ("POST", "stock/products/10/consume",
         {"amount": 1, "transaction_type": "consume", "spoiled": False, "stock_entry_id": "butter1"})
    ]
    assert "butter1" not in [e["stock_id"] for e in fake_grocy.stock]


def test_consume_undo_keeps_item(client, fake_grocy):
    pending = client.post("/api/consume", json={"code": "grcy:s:butter1"}).json()["pending"]
    r = client.post(f"/api/consume/{pending['id']}/cancel")
    assert r.json()["status"] == "cancelled"
    assert client.post(f"/api/consume/{pending['id']}/commit").json()["status"] == "cancelled"
    assert _consume_calls(fake_grocy) == []
    # Undo after it was already removed is rejected.
    pending = client.post("/api/consume", json={"code": "grcy:s:butter1"}).json()["pending"]
    client.post(f"/api/consume/{pending['id']}/commit")
    late = client.post(f"/api/consume/{pending['id']}/cancel")
    assert late.status_code == 409
    assert late.json()["pending"]["status"] == "committed"


def test_second_scan_commits_first_immediately(client, fake_grocy):
    first = client.post("/api/consume", json={"code": "grcy:s:butter1"}).json()["pending"]
    second = client.post("/api/consume", json={"code": "grcy:s:chicken1"}).json()
    assert [p["id"] for p in second["committed_previous"]] == [first["id"]]
    assert second["committed_previous"][0]["status"] == "committed"
    assert len(_consume_calls(fake_grocy)) == 1
    assert second["pending"]["status"] == "pending"


def test_double_scan_of_same_label_keeps_single_pending(client, fake_grocy):
    first = client.post("/api/consume", json={"code": "grcy:s:butter1"}).json()
    again = client.post("/api/consume", json={"code": "grcy:s:butter1"}).json()
    assert again["duplicate"] is True and again["committed_previous"] == []
    assert again["pending"]["id"] == first["pending"]["id"]
    assert client.post(f"/api/consume/{first['pending']['id']}/cancel").json()["status"] == "cancelled"
    assert _consume_calls(fake_grocy) == []
    assert "butter1" in [e["stock_id"] for e in fake_grocy.stock]


def test_oem_rescan_when_last_unit_was_pending_reports_not_in_stock(client, fake_grocy):
    assert client.post("/api/consume", json={"code": "grcy:s:chicken1"}).status_code == 200
    # chicken1 is the only chicken entry: scanning again commits it, then finds nothing left
    response = client.post("/api/consume", json={"code": "grcy:p:12"})
    assert response.status_code == 409
    assert len(_consume_calls(fake_grocy)) == 1


def test_consume_oem_barcode_uses_fifo_without_stock_entry(client, fake_grocy):
    pending = client.post("/api/consume", json={"code": "4006040052555"}).json()["pending"]
    assert pending["item"]["best_before_date"] == "2030-01-05"  # next to expire is shown
    client.post(f"/api/consume/{pending['id']}/commit")
    assert "stock_entry_id" not in _consume_calls(fake_grocy)[0][2]


def test_consume_safety_timer(client, fake_grocy):
    pending = client.post("/api/consume", json={"code": "grcy:s:chicken1"}).json()["pending"]
    deadline = time.time() + 8
    while time.time() < deadline:
        if client.get(f"/api/consume/{pending['id']}").json()["status"] != "pending":
            break
        time.sleep(0.2)
    assert client.get(f"/api/consume/{pending['id']}").json()["status"] == "committed"


def test_consume_errors(client, fake_grocy):
    assert client.post("/api/consume", json={"code": "nope-123"}).status_code == 404
    # Whole Milk is known but has no stock
    fake_grocy.barcodes.append({"product_id": 11, "barcode": "111"})
    r = client.post("/api/consume", json={"code": "111"})
    assert r.status_code == 409 and "not in stock" in r.json()["detail"]
    assert client.post("/api/consume/unknown/commit").status_code == 404


def test_consume_grocy_failure_reported(client, fake_grocy):
    pending = client.post("/api/consume", json={"code": "grcy:s:butter1"}).json()["pending"]
    fake_grocy.fail_consume = "boom"
    done = client.post(f"/api/consume/{pending['id']}/commit").json()
    assert done["status"] == "failed" and done["error"] == "boom"


def test_add_and_print(client, fake_grocy, printer):
    r = client.post("/api/stock/add", json={"product_id": 12, "best_before_date": "2026-12-24"})
    assert r.status_code == 200, r.text
    body = r.json()
    add_call = next(req for req in fake_grocy.requests if req[1] == "stock/products/12/add")
    assert add_call[2] == {"amount": 1, "transaction_type": "purchase", "best_before_date": "2026-12-24", "location_id": 2}
    assert body["barcode"] == f"grcy:s:{body['stock_id']}"
    assert body["print"]["ok"] is True
    assert len(printer.images) == 1
    image: Image.Image = printer.images[0]
    assert image.width == 696


def test_add_uses_defaults_and_env_location(client, fake_grocy, settings):
    settings.grocy_default_location_id = 3
    body = client.post("/api/stock/add", json={"product_id": 11}).json()
    expected = (dt.date.today() + dt.timedelta(days=7)).isoformat()  # env fallback (product has 0)
    assert body["best_before_date"] == expected
    add_call = next(req for req in fake_grocy.requests if req[1] == "stock/products/11/add")
    assert add_call[2]["location_id"] == 3
    # product default days and "never expires"
    assert client.post("/api/stock/add", json={"product_id": 10}).json()["best_before_date"] == (
        dt.date.today() + dt.timedelta(days=30)
    ).isoformat()
    assert client.post("/api/stock/add", json={"product_id": 13}).json()["best_before_date"] == "2999-12-31"
    assert client.post("/api/stock/add", json={"product_id": 10, "no_expiry": True}).json()["best_before_date"] == "2999-12-31"


def test_add_invalid_date(client):
    r = client.post("/api/stock/add", json={"product_id": 10, "best_before_date": "soon"})
    assert r.status_code == 422


def test_add_with_quantity_prints_label_per_item(client, fake_grocy, printer):
    r = client.post("/api/stock/add", json={"product_id": 12, "amount": 6, "best_before_date": "2026-12-24"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["amount"] == 6
    add_call = next(req for req in fake_grocy.requests if req[1] == "stock/products/12/add")
    assert add_call[2]["amount"] == 6
    entry = next(e for e in fake_grocy.stock if e["stock_id"] == body["stock_id"])
    assert entry["amount"] == 6
    assert body["print"] == {"ok": True, "printed": 6, "outcome": "printed", "did_print": True}
    assert len(printer.images) == 6  # one label per item, all with the same barcode


def test_add_quantity_reports_partial_print_failure(client, printer):
    original = printer.print_image

    def flaky(image):
        if len(printer.images) >= 2:
            raise PrinterError("No media")
        return original(image)

    printer.print_image = flaky
    body = client.post("/api/stock/add", json={"product_id": 12, "amount": 5}).json()
    assert body["print"]["ok"] is False
    assert body["print"]["printed"] == 2
    assert "No media" in body["print"]["error"]


def test_add_rejects_bad_quantity(client):
    assert client.post("/api/stock/add", json={"product_id": 12, "amount": 0}).status_code == 422
    assert client.post("/api/stock/add", json={"product_id": 12, "amount": 1000}).status_code == 422


def test_external_product_lookup(client, fake_off):
    body = client.get("/api/external-product", params={"barcode": "7612345678900"}).json()
    assert body == {"found": True, "name": "Crunchy Peanut Butter", "brand": "Acme", "quantity": "500 g"}
    assert client.get("/api/external-product", params={"barcode": "0000000000"}).json() == {"found": False}
    fake_off.down = True
    assert client.get("/api/external-product", params={"barcode": "7612345678900"}).json() == {"found": False}


def test_external_product_without_name_is_not_found(client, fake_off):
    fake_off.products["123"] = {"product_name": "", "brands": "", "quantity": ""}
    assert client.get("/api/external-product", params={"barcode": "123"}).json() == {"found": False}


def test_add_still_succeeds_when_printer_fails(client, printer, fake_grocy):
    printer.fail = "/dev/usb/lp0 not found"
    body = client.post("/api/stock/add", json={"product_id": 10}).json()
    assert body["print"] == {"ok": False, "error": "/dev/usb/lp0 not found", "printed": 0}
    assert body["stock_id"] in [e["stock_id"] for e in fake_grocy.stock]


def test_add_still_succeeds_on_unexpected_print_error(client, printer, fake_grocy):
    def boom(image):
        raise RuntimeError("driver exploded")

    printer.print_image = boom
    response = client.post("/api/stock/add", json={"product_id": 10})
    assert response.status_code == 200
    body = response.json()
    assert body["print"]["ok"] is False and "driver exploded" in body["print"]["error"]
    assert body["stock_id"] in [e["stock_id"] for e in fake_grocy.stock]


def test_reprint_and_test_print(client, printer):
    assert client.post("/api/print/stock/butter1").json()["ok"] is True
    assert client.post("/api/print/test").json()["ok"] is True
    assert len(printer.images) == 2
    assert client.post("/api/print/stock/missing").status_code == 404
    printer.fail = "No media"
    r = client.post("/api/print/test")
    assert r.status_code == 503 and "No media" in r.json()["detail"]


def test_move_location(client, fake_grocy):
    r = client.post("/api/stock/butter1/move", json={"location_id": 2})
    assert r.status_code == 200
    assert r.json()["location_name"] == "Freezer"
    transfer = next(req for req in fake_grocy.requests if req[1].endswith("/transfer"))
    assert transfer[2] == {"amount": 1, "location_id_from": 1, "location_id_to": 2, "stock_entry_id": "butter1"}


def test_move_location_moves_whole_entry(client, fake_grocy):
    next(e for e in fake_grocy.stock if e["stock_id"] == "chicken1")["amount"] = 3
    assert client.post("/api/stock/chicken1/move", json={"location_id": 3}).status_code == 200
    transfer = next(req for req in fake_grocy.requests if req[1].endswith("/transfer"))
    assert transfer[2]["amount"] == 3


def test_create_product_with_barcode(client, fake_grocy):
    r = client.post("/api/products", json={"name": "Oat Milk", "product_group_id": 2, "barcode": "5000000000001"})
    assert r.status_code == 200, r.text
    product = r.json()
    assert (product["name"], product["major"], product["sub"]) == ("Oat Milk", "Dairy", "Milk")
    created = next(req for req in fake_grocy.requests if req[:2] == ("POST", "objects/products"))[2]
    assert created["qu_id_stock"] == 1 and created["qu_id_purchase"] == 1  # lowest quantity unit id
    assert created["location_id"] == 1
    assert {"product_id": product["id"], "barcode": "5000000000001"} in fake_grocy.barcodes
    assert client.get("/api/lookup", params={"code": "5000000000001"}).json()["product"]["name"] == "Oat Milk"


def test_create_product_old_grocy_and_errors(client, fake_grocy):
    fake_grocy.v3 = True
    assert client.post("/api/products", json={"name": "Lentils"}).status_code == 200
    r = client.post("/api/products", json={"name": "Lentils"})
    assert r.status_code == 400 and "UNIQUE" in r.json()["detail"]
    r = client.post("/api/products", json={"name": "Dup", "barcode": "4006040052555"})
    assert r.status_code == 409 and "Butter" in r.json()["detail"]
