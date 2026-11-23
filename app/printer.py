"""Silent server-side printing to a Brother QL printer via brother_ql."""

from __future__ import annotations

import glob
import logging
import os
import socket
import threading
from fnmatch import fnmatch
from urllib.parse import urlparse

from PIL import Image

# brother_ql 0.9.x still references Image.ANTIALIAS, which Pillow >= 10 removed.
if not hasattr(Image, "ANTIALIAS"):
    Image.ANTIALIAS = Image.LANCZOS  # type: ignore[attr-defined]

from brother_ql.backends.helpers import send  # noqa: E402
from brother_ql.conversion import convert  # noqa: E402
from brother_ql.labels import FormFactor, LabelsManager  # noqa: E402
from brother_ql.raster import BrotherQLRaster  # noqa: E402

from .config import Settings  # noqa: E402

logger = logging.getLogger(__name__)

# Where the kernel exposes USB printer device nodes. After a printer power
# cycle the kernel may assign a different number (lp0 -> lp1), so when the
# configured node vanishes we look here for the replacement.
DEVICE_NODE_GLOB = "/dev/usb/lp*"


class PrinterError(RuntimeError):
    pass


class LabelPrinter:
    def __init__(self, settings: Settings):
        self.model = settings.printer_model
        self.backend = settings.printer_backend
        self.identifier = settings.printer_identifier
        self.label_size = settings.printer_label_size
        self.auto_cut = settings.printer_auto_cut
        self.red = settings.printer_red
        self._lock = threading.Lock()

        label = next((lbl for lbl in LabelsManager().iter_elements() if lbl.identifier == self.label_size), None)
        if label is None:
            raise PrinterError(f"Unknown PRINTER_LABEL_SIZE {self.label_size!r}")
        if label.form_factor != FormFactor.ENDLESS:
            raise PrinterError(
                f"PRINTER_LABEL_SIZE {self.label_size!r} is a die-cut label; use a continuous roll such as '62'"
            )
        self.width = label.dots_printable[0]

    # ---------------------------------------------------------------- status
    def status(self) -> dict:
        info = {"model": self.model, "backend": self.backend, "identifier": self.identifier, "label": self.label_size}
        try:
            ok, detail = self._probe()
        except Exception as exc:  # never let a health check crash
            ok, detail = False, f"{exc.__class__.__name__}: {exc}"
        return {**info, "ok": ok, "detail": detail}

    def _probe(self) -> tuple[bool, str]:
        if self.backend == "linux_kernel":
            path = self.identifier.removeprefix("file://")
            if not os.path.exists(path) and self._recover_device_node():
                path = self.identifier.removeprefix("file://")
            if not os.path.exists(path):
                return False, f"{path} not found – is the QL-800 switched on and passed into the container?"
            if not os.access(path, os.W_OK):
                return False, f"{path} exists but is not writable"
            return True, f"{path} present"
        if self.backend == "pyusb":
            import usb.core

            vendor, product = _parse_usb_identifier(self.identifier)
            try:
                device = usb.core.find(idVendor=vendor, idProduct=product)
            except usb.core.NoBackendError:
                return False, "libusb not available"
            if device is None:
                return False, f"USB device {vendor:#06x}:{product:#06x} not found"
            return True, f"USB device {vendor:#06x}:{product:#06x} present"
        if self.backend == "network":
            parsed = urlparse(self.identifier)
            host, port = parsed.hostname, parsed.port or 9100
            with socket.create_connection((host, port), timeout=1.5):
                return True, f"{host}:{port} reachable"
        return False, f"Unsupported backend {self.backend!r}"

    # ----------------------------------------------------------------- print
    def _recover_device_node(self) -> bool:
        """Switch to another ``/dev/usb/lp*`` node when the configured one vanished.

        After the printer powers down and back up the kernel may assign a new
        device number (e.g. ``lp0`` becomes ``lp1``). Returns ``True`` when the
        identifier was updated to a newly discovered node.
        """
        if self.backend != "linux_kernel":
            return False
        path = self.identifier.removeprefix("file://")
        if not fnmatch(path, DEVICE_NODE_GLOB) or os.path.exists(path):
            return False
        candidates = sorted(p for p in glob.glob(DEVICE_NODE_GLOB) if os.access(p, os.W_OK))
        if not candidates:
            return False
        logger.warning("Printer device %s is gone; switching to %s", path, candidates[0])
        self.identifier = f"file://{candidates[0]}"
        return True

    def build_instructions(self, image: Image.Image) -> bytes:
        qlr = BrotherQLRaster(self.model)
        qlr.exception_on_warning = True
        return convert(
            qlr=qlr,
            images=[image],
            label=self.label_size,
            rotate="0",
            threshold=70.0,
            dither=False,
            compress=False,
            red=self.red,
            dpi_600=False,
            hq=True,
            cut=self.auto_cut,
        )

    def _send(self, instructions: bytes) -> dict:
        try:
            result = send(
                instructions=instructions,
                printer_identifier=self.identifier,
                backend_identifier=self.backend,
                blocking=True,
            )
        except Exception as exc:
            raise PrinterError(f"Printing failed: {exc.__class__.__name__}: {exc}") from exc
        if result.get("outcome") == "error":
            errors = (result.get("printer_state") or {}).get("errors") or ["unknown error"]
            raise PrinterError("Printer reported: " + ", ".join(errors))
        return result

    def print_image(self, image: Image.Image) -> dict:
        """Send ``image`` to the printer. Blocking – call from a worker thread."""
        if image.width != self.width:
            raise PrinterError(f"Label image must be {self.width}px wide, got {image.width}px")
        instructions = self.build_instructions(image)
        with self._lock:
            self._recover_device_node()
            try:
                result = self._send(instructions)
            except PrinterError:
                # The device node may have disappeared mid-print (printer power
                # cycle): look for its replacement and retry once.
                if not self._recover_device_node():
                    raise
                result = self._send(instructions)
        if not result.get("did_print"):
            logger.warning("Printer did not confirm completion (outcome=%s)", result.get("outcome"))
        return {"outcome": result.get("outcome"), "did_print": bool(result.get("did_print"))}


def _parse_usb_identifier(identifier: str) -> tuple[int, int]:
    # usb://0x04f9:0x209b or usb://0x04f9:0x209b/SERIAL
    rest = identifier.removeprefix("usb://").split("/")[0]
    vendor, _, product = rest.partition(":")
    try:
        return int(vendor, 16), int(product, 16)
    except ValueError as exc:
        raise PrinterError(f"Invalid USB printer identifier {identifier!r}") from exc
