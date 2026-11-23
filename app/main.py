"""FastAPI application: JSON API for the kiosk + static PWA assets."""

from __future__ import annotations

import datetime as dt
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings, get_settings
from .consume import ConsumeManager, NotPendingError
from .external import ExternalProductLookup
from .grocy import GrocyClient, GrocyError
from .labels import LabelError, render_label
from .printer import LabelPrinter, PrinterError
from .service import ConflictError, InventoryService, NotFoundError, Resolved

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


# --------------------------------------------------------------------- schemas
class ScanRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=256)


class AddStockRequest(BaseModel):
    product_id: int
    amount: int = Field(1, ge=1, le=999)
    best_before_date: Optional[str] = None
    no_expiry: bool = False
    location_id: Optional[int] = None
    print_label: bool = True
    # individual = one label per item, box = a single label for the whole quantity
    label_mode: Literal["individual", "box"] = "individual"


class MoveRequest(BaseModel):
    location_id: int


class CreateProductRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    product_group_id: Optional[int] = None
    barcode: Optional[str] = Field(None, max_length=256)
    default_best_before_days: Optional[int] = None
    location_id: Optional[int] = None


class CreateNamedRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)


class SetCategoryRequest(BaseModel):
    product_group_id: Optional[int] = None


class OpenBehaviorRequest(BaseModel):
    # None = opened items stay where they are
    location_id: Optional[int] = None


def _resolved_to_dict(resolved: Resolved) -> dict:
    return {
        "kind": resolved.kind,
        "product": resolved.product,
        "entries": resolved.entries,
        "stock_id": resolved.stock_id,
    }


def create_app(
    settings: Optional[Settings] = None,
    grocy: Optional[GrocyClient] = None,
    printer: Optional[LabelPrinter] = None,
    external: Optional[ExternalProductLookup] = None,
) -> FastAPI:
    settings = settings or get_settings()
    owns_grocy = grocy is None
    grocy = grocy or GrocyClient(settings.grocy_api_base, settings.grocy_api_key, settings.grocy_timeout_seconds)
    printer = printer or LabelPrinter(settings)
    owns_external = external is None and bool(settings.external_lookup_url)
    if owns_external:
        external = ExternalProductLookup(settings.external_lookup_url, settings.external_lookup_timeout_seconds)
    service = InventoryService(grocy, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.consumer = ConsumeManager(service.consume, settings.consume_countdown_seconds)
        try:
            yield
        finally:
            await app.state.consumer.shutdown()
            if owns_grocy:
                await grocy.aclose()
            if owns_external and external is not None:
                await external.aclose()

    app = FastAPI(title="Grocy Kitchen Kiosk", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.service = service
    app.state.printer = printer

    # ----------------------------------------------------------- error mapping
    @app.exception_handler(GrocyError)
    async def _grocy_error(_: Request, exc: GrocyError):
        status = 400 if exc.status_code == 400 else 502
        return JSONResponse({"detail": f"Grocy: {exc.message}"}, status_code=status)

    @app.exception_handler(NotFoundError)
    async def _not_found(_: Request, exc: NotFoundError):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(ConflictError)
    async def _conflict(_: Request, exc: ConflictError):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(ValueError)
    async def _bad_value(_: Request, exc: ValueError):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    # ---------------------------------------------------------------- printing
    async def print_label(product_name: str, best_before_date: Optional[str], stock_id: str, copies: int = 1) -> dict:
        data = f"{settings.barcode_prefix}{stock_id}"
        printed = 0
        try:
            image = render_label(product_name, best_before_date, data, width=printer.width)
            result: dict = {}
            for _ in range(max(1, copies)):
                result = await run_in_threadpool(printer.print_image, image)
                printed += 1
        except (PrinterError, LabelError) as exc:
            logger.warning("Label print failed for %s after %d copies: %s", stock_id, printed, exc)
            return {"ok": False, "error": str(exc), "printed": printed}
        except Exception as exc:  # never turn a successful stock add into an HTTP 500
            logger.exception("Unexpected error printing label for %s", stock_id)
            return {"ok": False, "error": f"Printing failed: {exc.__class__.__name__}: {exc}", "printed": printed}
        return {"ok": True, "printed": printed, **result}

    # -------------------------------------------------------------------- API
    @app.get("/api/config")
    async def config():
        return {
            "consume_countdown_seconds": settings.consume_countdown_seconds,
            "barcode_prefix": settings.barcode_prefix,
            "default_best_before_days": settings.grocy_default_best_before_days,
            "default_location_id": settings.grocy_default_location_id,
            "category_delimiter": settings.category_delimiter,
            "ui_idle_return_seconds": settings.ui_idle_return_seconds,
        }

    @app.get("/api/health")
    async def health():
        grocy_status: dict
        try:
            info = await grocy.system_info()
            grocy_status = {"ok": True, "version": (info.get("grocy_version") or {}).get("Version")}
        except GrocyError as exc:
            grocy_status = {"ok": False, "error": exc.message}
        printer_status = await run_in_threadpool(printer.status)
        ok = grocy_status["ok"] and printer_status["ok"]
        return {"status": "ok" if ok else "degraded", "grocy": grocy_status, "printer": printer_status}

    @app.get("/api/catalog")
    async def catalog():
        return await service.catalog()

    @app.get("/api/inventory")
    async def inventory():
        return await service.inventory()

    @app.get("/api/lookup")
    async def lookup(code: str = Query(..., min_length=1, max_length=256)):
        return _resolved_to_dict(await service.resolve(code))

    @app.get("/api/products/{product_id}/stock")
    async def product_stock(product_id: int):
        return _resolved_to_dict(await service.product_stock(product_id))

    @app.get("/api/external-product")
    async def external_product(barcode: str = Query(..., min_length=1, max_length=256)):
        if external is None:
            return {"found": False}
        info = await external.lookup(barcode)
        return {"found": info is not None, **(info or {})}

    @app.post("/api/products")
    async def create_product(body: CreateProductRequest):
        return await service.create_product(
            body.name, body.product_group_id, body.barcode, body.default_best_before_days, body.location_id
        )

    @app.post("/api/products/{product_id}/category")
    async def set_product_category(product_id: int, body: SetCategoryRequest):
        return await service.set_product_category(product_id, body.product_group_id)

    @app.post("/api/products/{product_id}/open-behavior")
    async def set_open_behavior(product_id: int, body: OpenBehaviorRequest):
        return await service.set_open_behavior(product_id, body.location_id)

    @app.post("/api/locations")
    async def create_location(body: CreateNamedRequest):
        return await service.create_location(body.name)

    @app.post("/api/categories")
    async def create_category(body: CreateNamedRequest):
        return await service.create_category(body.name)

    # ------------------------------------------------------------- consume
    @app.post("/api/consume")
    async def start_consume(body: ScanRequest, request: Request):
        consumer: ConsumeManager = request.app.state.consumer
        pending, committed, duplicate = await consumer.start(lambda: service.prepare_consume(body.code))
        return {
            "pending": pending.to_dict(),
            "duplicate": duplicate,
            "committed_previous": [p.to_dict() for p in committed],
            "countdown_seconds": settings.consume_countdown_seconds,
        }

    def _get_pending(request: Request, pending_id: str):
        pending = request.app.state.consumer.get(pending_id)
        if pending is None:
            raise HTTPException(404, "Unknown pending consumption")
        return pending

    @app.get("/api/consume/{pending_id}")
    async def get_consume(pending_id: str, request: Request):
        return _get_pending(request, pending_id).to_dict()

    @app.post("/api/consume/{pending_id}/commit")
    async def commit_consume(pending_id: str, request: Request):
        _get_pending(request, pending_id)
        return (await request.app.state.consumer.commit(pending_id)).to_dict()

    @app.post("/api/consume/{pending_id}/cancel")
    async def cancel_consume(pending_id: str, request: Request):
        _get_pending(request, pending_id)
        try:
            return (await request.app.state.consumer.cancel(pending_id)).to_dict()
        except NotPendingError as exc:
            return JSONResponse(
                {"detail": f"Too late – item already {exc.pending.status}", "pending": exc.pending.to_dict()},
                status_code=409,
            )

    # ---------------------------------------------------------------- stock
    @app.post("/api/stock/add")
    async def add_stock(body: AddStockRequest):
        added = await service.add_stock(
            body.product_id, body.best_before_date, body.no_expiry, body.location_id, body.amount
        )
        printed = None
        if body.print_label:
            copies = body.amount if body.label_mode == "individual" else 1
            printed = await print_label(
                added["product_name"], added["best_before_date"], added["stock_id"], copies=copies
            )
        return {**added, "barcode": f"{settings.barcode_prefix}{added['stock_id']}", "print": printed}

    @app.post("/api/stock/{stock_id}/move")
    async def move_stock(stock_id: str, body: MoveRequest):
        return await service.move(stock_id, body.location_id)

    @app.post("/api/stock/{stock_id}/open")
    async def open_stock(stock_id: str):
        return await service.open_entry(stock_id)

    @app.post("/api/print/stock/{stock_id}")
    async def reprint(stock_id: str):
        product, entry = await service.stock_entry(stock_id)
        result = await print_label(product["name"], entry["best_before_date"], stock_id)
        if not result["ok"]:
            raise HTTPException(503, result["error"])
        return result

    @app.post("/api/print/test")
    async def print_test():
        result = await print_label("TEST LABEL – Kitchen Kiosk", dt.date.today().isoformat(), "TEST")
        if not result["ok"]:
            raise HTTPException(503, result["error"])
        return result

    @app.get("/api/{_path:path}", include_in_schema=False)
    async def api_not_found(_path: str):
        raise HTTPException(404, "Not found")

    # ----------------------------------------------------------------- PWA
    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/manifest.json", include_in_schema=False)
    async def manifest():
        return FileResponse(STATIC_DIR / "manifest.json", media_type="application/manifest+json")

    @app.get("/service-worker.js", include_in_schema=False)
    async def service_worker():
        return FileResponse(
            STATIC_DIR / "service-worker.js",
            media_type="application/javascript",
            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
        )

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app

