import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.external import ExternalProductLookup
from app.grocy import GrocyClient
from app.main import create_app
from app.printer import PrinterError

from .fake_grocy import FakeGrocy


class FakeOpenFoodFacts:
    def __init__(self):
        self.products = {
            "7612345678900": {"product_name": "Crunchy Peanut Butter", "brands": "Acme,Other", "quantity": "500 g"},
        }
        self.down = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("Connection refused", request=request)
        barcode = request.url.path.rsplit("/", 1)[-1]
        product = self.products.get(barcode)
        if product is None:
            return httpx.Response(404, json={"status": 0, "status_verbose": "product not found"})
        return httpx.Response(200, json={"status": 1, "product": product})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


class FakePrinter:
    width = 696

    def __init__(self):
        self.images = []
        self.fail = None

    def print_image(self, image):
        if self.fail:
            raise PrinterError(self.fail)
        self.images.append(image)
        return {"outcome": "printed", "did_print": True}

    def status(self):
        return {"ok": True, "detail": "fake", "model": "QL-800"}


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        grocy_url="http://grocy.test",
        grocy_api_key="test-key",
        grocy_default_location_id=None,
        grocy_default_best_before_days=7,
        consume_countdown_seconds=1,
        seed_default_categories=False,  # tests opt in explicitly
    )


@pytest.fixture
def fake_grocy():
    return FakeGrocy()


@pytest.fixture
def printer():
    return FakePrinter()


@pytest.fixture
def fake_off():
    return FakeOpenFoodFacts()


@pytest.fixture
def client(settings, fake_grocy, printer, fake_off):
    grocy = GrocyClient(settings.grocy_api_base, settings.grocy_api_key, transport=fake_grocy.transport())
    external = ExternalProductLookup("http://off.test", transport=fake_off.transport())
    app = create_app(settings, grocy=grocy, printer=printer, external=external)
    with TestClient(app) as test_client:
        yield test_client
