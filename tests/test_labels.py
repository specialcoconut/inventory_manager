import pytest

from app.config import Settings
from app.labels import LabelError, code128_modules, format_expiry, render_barcode, render_label
from app.printer import LabelPrinter, PrinterError


def test_code128_modules_include_start_check_and_stop():
    modules = code128_modules("grcy:s:65a1b2c3d4e5f")
    assert set(modules) == {"0", "1"}
    assert modules.startswith("11010010000")  # START B
    assert modules.endswith("1100011101011")  # STOP + termination bar


def test_barcode_is_pixel_exact_with_quiet_zones():
    image = render_barcode("grcy:s:65a1b2c3d4e5f", 696, height=40)
    assert image.size == (696, 40)
    histogram = image.histogram()
    assert [value for value, count in enumerate(histogram) if count] == [0, 255]  # no grey anti-aliasing
    row = [image.getpixel((x, 0)) for x in range(696)]
    first_bar = row.index(0)
    last_bar = len(row) - 1 - row[::-1].index(0)
    module_px = 696 // (len(code128_modules("grcy:s:65a1b2c3d4e5f")) + 20)
    assert first_bar >= 10 * module_px
    assert 695 - last_bar >= 10 * module_px


def test_barcode_too_long_raises():
    with pytest.raises(LabelError):
        render_barcode("x" * 200, 696)


def test_format_expiry():
    assert format_expiry("2026-12-01") == "EXP: 2026-12-01"
    assert format_expiry("2026-12-01 00:00:00") == "EXP: 2026-12-01"
    assert format_expiry("2999-12-31") == "EXP: NO EXPIRY"
    assert format_expiry(None) == "EXP: NO EXPIRY"


def test_render_label_dimensions_and_long_names():
    short = render_label("Butter", "2026-12-01", "grcy:s:abc")
    long = render_label("Extra mature farmhouse cheddar cheese with caramelised onion chutney " * 3, "2026-12-01", "grcy:s:abc")
    assert short.width == long.width == 696
    assert short.mode == "L"
    assert 250 < short.height < 600 and 250 < long.height < 600


def test_label_converts_to_ql800_raster(settings):
    printer = LabelPrinter(settings)
    assert printer.width == 696
    data = printer.build_instructions(render_label("Butter", "2026-12-01", "grcy:s:abc"))
    assert b"\x00" * 200 + b"\x1b@" in data  # invalidate + initialize
    assert b"\x1biz" in data  # media/quality information
    assert data.endswith(b"\x1a")  # print with feed


def test_print_image_sends_raster_to_configured_printer(settings, monkeypatch):
    sent = {}

    def fake_send(instructions, printer_identifier, backend_identifier, blocking):
        sent.update(identifier=printer_identifier, backend=backend_identifier, size=len(instructions))
        return {"outcome": "printed", "did_print": True, "printer_state": None}

    monkeypatch.setattr("app.printer.send", fake_send)
    result = LabelPrinter(settings).print_image(render_label("Butter", None, "grcy:s:abc"))
    assert result == {"outcome": "printed", "did_print": True}
    assert sent["identifier"] == "file:///dev/usb/lp0"
    assert sent["backend"] == "linux_kernel"
    assert sent["size"] > 1000


def test_print_image_reports_printer_errors(settings, monkeypatch):
    monkeypatch.setattr(
        "app.printer.send",
        lambda **_: {"outcome": "error", "did_print": False, "printer_state": {"errors": ["No media when printing"]}},
    )
    with pytest.raises(PrinterError, match="No media"):
        LabelPrinter(settings).print_image(render_label("Butter", None, "grcy:s:abc"))


def test_die_cut_label_rejected():
    with pytest.raises(PrinterError):
        LabelPrinter(Settings(_env_file=None, printer_label_size="62x29"))


def test_printer_status_missing_device(tmp_path):
    printer = LabelPrinter(Settings(_env_file=None, printer_identifier=f"file://{tmp_path}/lp0"))
    status = printer.status()
    assert status["ok"] is False
    assert "not found" in status["detail"]
    (tmp_path / "lp0").write_bytes(b"")
    assert printer.status()["ok"] is True
