"""Render 62mm thermal labels: product name, expiry date and a Code 128 barcode."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Optional

import barcode
from PIL import Image, ImageDraw, ImageFont

from .grocy import NEVER_EXPIRES

# Printable width in dots for a 62mm continuous roll at 300 dpi (brother_ql label "62").
DEFAULT_WIDTH = 696

MARGIN = 16
QUIET_ZONE_MODULES = 10
BARCODE_HEIGHT = 150

BOLD_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
)
REGULAR_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
)


class LabelError(ValueError):
    pass


@lru_cache(maxsize=64)
def _font(size: int, bold: bool) -> ImageFont.FreeTypeFont:
    for path in BOLD_FONT_CANDIDATES if bold else REGULAR_FONT_CANDIDATES:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    left, _, right, _ = draw.textbbox((0, 0), text, font=font)
    return right - left


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if _text_width(draw, candidate, font) <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word
        # Break overly long single words character by character.
        while _text_width(draw, current, font) > max_width and len(current) > 1:
            cut = len(current)
            while cut > 1 and _text_width(draw, current[:cut], font) > max_width:
                cut -= 1
            lines.append(current[:cut])
            current = current[cut:]
    if current:
        lines.append(current)
    return lines


def _fit_title(draw: ImageDraw.ImageDraw, text: str, max_width: int, max_lines: int = 2):
    """Pick the largest bold font size that fits ``text`` into ``max_lines`` lines."""
    for size in range(72, 31, -4):
        font = _font(size, True)
        lines = _wrap(draw, text, font, max_width)
        if len(lines) <= max_lines:
            return font, lines
    font = _font(32, True)
    lines = _wrap(draw, text, font, max_width)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and _text_width(draw, last + "…", font) > max_width:
            last = last[:-1]
        lines[-1] = last + "…"
    return font, lines


def code128_modules(data: str) -> str:
    """Return the Code 128 bar pattern ('1' = bar, '0' = space) including start/check/stop."""
    try:
        return barcode.get("code128", data).build()[0]
    except Exception as exc:  # python-barcode raises various errors for unsupported characters
        raise LabelError(f"Cannot encode {data!r} as Code 128: {exc}") from exc


def render_barcode(data: str, width: int, height: int = BARCODE_HEIGHT) -> Image.Image:
    """Draw a pixel-exact Code 128 barcode centred in ``width`` px with quiet zones."""
    modules = code128_modules(data)
    module_px = width // (len(modules) + 2 * QUIET_ZONE_MODULES)
    if module_px < 1:
        raise LabelError(f"Barcode data {data!r} is too long to fit on the label")
    image = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(image)
    x = (width - len(modules) * module_px) // 2
    for bit in modules:
        if bit == "1":
            draw.rectangle([x, 0, x + module_px - 1, height - 1], fill=0)
        x += module_px
    return image


def format_expiry(best_before_date: Optional[str]) -> str:
    """Expiry line for the label; empty when the item never expires (line is omitted)."""
    if not best_before_date or best_before_date.startswith(NEVER_EXPIRES):
        return ""
    return f"EXP: {best_before_date[:10]}"


def render_label(
    product_name: str,
    best_before_date: Optional[str],
    barcode_data: str,
    human_readable: Optional[str] = None,
    width: int = DEFAULT_WIDTH,
) -> Image.Image:
    """Render a label for a continuous roll; height grows with the content."""
    scratch = ImageDraw.Draw(Image.new("L", (1, 1), 255))
    inner = width - 2 * MARGIN

    title_font, title_lines = _fit_title(scratch, product_name.strip() or "?", inner)
    exp_font = _font(48, True)
    hr_font = _font(30, False)
    exp_text = format_expiry(best_before_date)
    hr_text = human_readable if human_readable is not None else barcode_data

    bars = render_barcode(barcode_data, width)

    title_line_h = int(title_font.size * 1.15)
    exp_h = int(exp_font.size * 1.2) if exp_text else 0
    hr_h = int(hr_font.size * 1.3)
    exp_block = exp_h + 16 if exp_text else 8
    height = MARGIN + title_line_h * len(title_lines) + 8 + exp_block + bars.height + 8 + hr_h + MARGIN

    image = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(image)
    y = MARGIN
    for line in title_lines:
        draw.text((MARGIN, y), line, font=title_font, fill=0)
        y += title_line_h
    y += 8
    if exp_text:
        draw.text((MARGIN, y), exp_text, font=exp_font, fill=0)
        y += exp_h + 16
    else:
        y += 8
    image.paste(bars, (0, y))
    y += bars.height + 8
    hr_w = _text_width(draw, hr_text, hr_font)
    draw.text(((width - hr_w) // 2, y), hr_text, font=hr_font, fill=0)
    return image
