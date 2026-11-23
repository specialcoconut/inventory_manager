from app.scanning import parse_scan


def test_prefixed_stock_label():
    scan = parse_scan("grcy:s:65a1b2c3d4e5f", "grcy:s:")
    assert (scan.kind, scan.value) == ("stock", "65a1b2c3d4e5f")


def test_prefix_is_case_insensitive_and_trimmed():
    scan = parse_scan("  GRCY:S:abc123 \n", "grcy:s:")
    assert (scan.kind, scan.value) == ("stock", "abc123")


def test_custom_prefix_and_native_grocy_stock_label():
    assert parse_scan("KIT-abc", "KIT-").value == "abc"
    # Labels printed by Grocy itself still work with a custom prefix.
    assert parse_scan("grcy:s:abc", "KIT-").kind == "stock"


def test_native_grocy_product_labels():
    assert (parse_scan("grcy:p:12").kind, parse_scan("grcy:p:12").value) == ("product", "12")
    scan = parse_scan("grcy:p:12:65a1b2c3")
    assert (scan.kind, scan.value) == ("stock", "65a1b2c3")


def test_oem_barcode_is_unknown():
    scan = parse_scan("4006040052555")
    assert (scan.kind, scan.value) == ("unknown", "4006040052555")


def test_bare_prefix_is_not_a_stock_label():
    assert parse_scan("grcy:s:").kind == "unknown"
