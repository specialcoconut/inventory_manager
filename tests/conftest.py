import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.grocy import GrocyClient
from app.main import create_app
from app.printer import PrinterError

from .fake_grocy import FakeGrocy


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
    )


@pytest.fixture
def fake_grocy():
    return FakeGrocy()


@pytest.fixture
def printer():
    return FakePrinter()


@pytest.fixture
def client(settings, fake_grocy, printer):
    grocy = GrocyClient(settings.grocy_api_base, settings.grocy_api_key, transport=fake_grocy.transport())
    app = create_app(settings, grocy=grocy, printer=printer)
    with TestClient(app) as test_client:
        yield test_client
