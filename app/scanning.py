"""Normalization of codes read by the barcode scanner."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScanCode:
    """A normalized scanned barcode.

    ``kind`` is one of:
      * ``stock``   – a stock-entry label (``<prefix><stock_id>``), ``value`` is the stock_id
      * ``product`` – a Grocy product barcode (``grcy:p:<product_id>``), ``value`` is the product id
      * ``unknown`` – anything else: an OEM barcode or a raw stock_id, ``value`` is the raw code
    """

    kind: str
    value: str
    raw: str


def parse_scan(code: str, prefix: str = "grcy:s:") -> ScanCode:
    raw = (code or "").strip()
    lowered = raw.lower()
    if prefix and lowered.startswith(prefix.lower()) and len(raw) > len(prefix):
        return ScanCode("stock", raw[len(prefix):].strip(), raw)
    if lowered.startswith("grcy:p:"):
        # Native Grocy product label, optionally followed by ":<stock_id>".
        parts = raw.split(":")
        if len(parts) >= 4 and parts[3]:
            return ScanCode("stock", parts[3], raw)
        if parts[2].isdigit():
            return ScanCode("product", parts[2], raw)
    if lowered.startswith("grcy:s:") and len(raw) > len("grcy:s:"):
        return ScanCode("stock", raw[len("grcy:s:"):].strip(), raw)
    return ScanCode("unknown", raw, raw)
