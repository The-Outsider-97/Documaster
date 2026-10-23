"""Burned-in page content: text stamps, watermarks, page numbers, headers/footers and images.

Content is drawn with reportlab on an overlay in *display* coordinates and merged under the page's own rotation, so a stamp
placed at a normalised position appears there and upright on rotated pages too. Fonts come from the system (TrueType, embedded),
so non-Latin text works when a covering font is installed; uncovered characters are an error, not a silent substitution.
"""
from __future__ import annotations

import io
import math
from pathlib import Path
from typing import Any, Callable

from ..docmodel import fonts as fontmod
from ..errors import DependencyMissing, ValidationFailed
from .geometry import PageGeometry, geometry_of, hex_colour
from .pdfio import parse_ranges

_registered: dict[str, str] = {}


def resolve_pages(spec: Any, total: int) -> list[int]:
    """``"all"``, ``"odd"``, ``"even"``, ``"first"``, ``"last"``, a range string, or a list of 1-based numbers -> 0-based."""
    if spec in (None, "all", ""):
        return list(range(total))
    if spec == "odd":
        return list(range(0, total, 2))
    if spec == "even":
        return list(range(1, total, 2))
    if spec == "first":
        return [0]
    if spec == "last":
        return [total - 1]
    if isinstance(spec, list):
        out = []
        for n in spec:
            if not isinstance(n, int) or not 1 <= n <= total:
                raise ValidationFailed(f"Page {n} is outside the document (pages 1-{total})")
            out.append(n - 1)
        return out
    if isinstance(spec, int):
        return resolve_pages([spec], total)
    return parse_ranges(str(spec), total)


def pick_font(text: str, bold: bool = False, family: str | None = None) -> str:
    """Register and return a reportlab font name that covers ``text`` (or raise with the uncovered characters)."""
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
    except ImportError as exc:
        raise DependencyMissing("Stamping text needs the 'reportlab' package") from exc
    plan = fontmod.plan(family)
    needs_cjk = any(fontmod.is_cjk(c) for c in text)
    candidates: list[tuple[str, Path, int]] = []
    if needs_cjk and plan.cjk:
        label, path, idx = plan.cjk
        candidates.append((f"DMS-{label}", path, idx))
    if plan.body:
        candidates.append((f"DMS-{plan.body.label}-{'B' if bold else 'R'}", plan.body.files[1 if bold else 0], 0))
    if plan.fallback:
        candidates.append((f"DMS-{plan.fallback.label}-fb", plan.fallback.files[0], 0))
    for name, path, idx in candidates:
        if name not in _registered:
            try:
                pdfmetrics.registerFont(TTFont(name, str(path), subfontIndex=idx))
                _registered[name] = str(path)
            except Exception:  # noqa: BLE001
                continue
        covered = fontmod.coverage_of(path, idx)
        if not fontmod.missing_chars(text, [covered]):
            return name
    if all(ord(c) < 256 for c in text):
        return "Helvetica-Bold" if bold else "Helvetica"
    cov = [fontmod.coverage_of(p, i) for _, p, i in candidates if _registered.get(_)]
    bad = sorted(fontmod.missing_chars(text, cov))[:12]
    raise ValidationFailed("No installed font has these characters: " + " ".join(bad)
                           + ". Install a font that covers them, or use other text.", details={"characters": bad})


Draw = Callable[[Any, int, float, float], None]


def overlay_pages(writer: Any, pages: list[int], draw: Draw) -> None:
    """Draw on the given pages (0-based). ``draw(canvas, index, display_width, display_height)`` works in display space with
    the origin at the bottom-left of the page as shown."""
    import pypdf
    from pypdf import Transformation
    from reportlab.pdfgen import canvas
    for i in pages:
        page = writer.pages[i]
        g = geometry_of(page)
        dw, dh = g.display_size
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=(dw, dh))
        c.saveState()
        draw(c, i, dw, dh)
        c.restoreState()
        c.showPage()
        c.save()
        overlay = pypdf.PdfReader(io.BytesIO(buf.getvalue())).pages[0]
        w, h = g.width, g.height
        if g.rotate == 0:
            ctm = (1, 0, 0, 1, g.x0, g.y0)
        elif g.rotate == 90:
            ctm = (0, 1, -1, 0, g.x0 + w, g.y0)
        elif g.rotate == 180:
            ctm = (-1, 0, 0, -1, g.x0 + w, g.y0 + h)
        else:
            ctm = (0, -1, 1, 0, g.x0, g.y0 + h)
        page.merge_transformed_page(overlay, Transformation(ctm=ctm))


def _alpha(c: Any, opacity: float) -> None:
    if opacity < 1:
        c.setFillAlpha(opacity)
        c.setStrokeAlpha(opacity)


def text_stamp(writer: Any, spec: dict[str, Any], total: int) -> int:
    """``{text, pages?, point:[u,v] | position, size, color, opacity, rotation, bold, font, align}``."""
    text = str(spec.get("text", ""))
    if not text.strip():
        raise ValidationFailed("The text stamp is empty")
    if len(text) > 2000:
        raise ValidationFailed("The text stamp is too long")
    size = float(spec.get("size", 24))
    if not 4 <= size <= 400:
        raise ValidationFailed("Text size must be between 4 and 400 pt")
    color = hex_colour(spec.get("color"), (0.8, 0, 0))
    opacity = float(spec.get("opacity", 1.0))
    if not 0.02 <= opacity <= 1:
        raise ValidationFailed("Opacity must be between 0.02 and 1")
    angle = float(spec.get("rotation", 0))
    font = pick_font(text, bool(spec.get("bold")), spec.get("font"))
    pages = resolve_pages(spec.get("pages"), total)
    align = spec.get("align", "center")
    point = spec.get("point") or [0.5, 0.5]
    lines = text.split("\n")

    def draw(c: Any, i: int, dw: float, dh: float) -> None:
        _alpha(c, opacity)
        c.setFillColorRGB(*color)
        c.setFont(font, size)
        c.translate(point[0] * dw, (1 - point[1]) * dh)
        c.rotate(angle)
        y = 0.0
        for line in lines:
            if align == "left":
                c.drawString(0, y, line)
            elif align == "right":
                c.drawRightString(0, y, line)
            else:
                c.drawCentredString(0, y, line)
            y -= size * 1.2

    overlay_pages(writer, pages, draw)
    return len(pages)


def watermark(writer: Any, spec: dict[str, Any], total: int) -> int:
    """Diagonal, light text across the page centre. ``{text, pages?, size?, color?, opacity?, angle?}``"""
    spec = {"size": 72, "color": "#808080", "opacity": 0.25, "rotation": 45, "bold": True, **spec}
    spec.setdefault("point", [0.5, 0.5])
    return text_stamp(writer, spec, total)


def page_numbers(writer: Any, spec: dict[str, Any], total: int) -> int:
    """``{format: "Page {n} of {N}", pages?, position: bottom-center|..., size, color, start, prefix (Bates), digits, margin_mm}``"""
    fmt = str(spec.get("format", "Page {n} of {N}"))
    start = int(spec.get("start", 1))
    pages = resolve_pages(spec.get("pages"), total)
    size = float(spec.get("size", 10))
    color = hex_colour(spec.get("color"), (0.2, 0.2, 0.2))
    margin = float(spec.get("margin_mm", 10)) * 72 / 25.4
    position = spec.get("position", "bottom-center")
    digits = int(spec.get("digits", 0))
    sample = fmt.replace("{n}", "0" * max(1, digits)).replace("{N}", str(total + start - 1))
    font = pick_font(sample + "0123456789", False, spec.get("font"))
    if "{" in fmt.replace("{n}", "").replace("{N}", ""):
        raise ValidationFailed("Only {n} and {N} can be used in the page number format")

    def draw(c: Any, i: int, dw: float, dh: float) -> None:
        n = start + pages.index(i)
        label = fmt.replace("{n}", str(n).zfill(digits)).replace("{N}", str(total + start - 1))
        c.setFont(font, size)
        c.setFillColorRGB(*color)
        vert, _, horiz = position.partition("-")
        y = margin if vert == "bottom" else dh - margin - size
        if horiz == "left":
            c.drawString(margin, y, label)
        elif horiz == "right":
            c.drawRightString(dw - margin, y, label)
        else:
            c.drawCentredString(dw / 2, y, label)

    overlay_pages(writer, pages, draw)
    return len(pages)


def image_stamp(writer: Any, spec: dict[str, Any], image_path: Path, total: int) -> int:
    """``{rect:[l,t,r,b] (normalised), pages?, opacity?, keep_aspect?}`` with the image read from ``image_path``."""
    try:
        from PIL import Image
        from reportlab.lib.utils import ImageReader
    except ImportError as exc:
        raise DependencyMissing("Placing images needs 'Pillow' and 'reportlab'") from exc
    from .geometry import clamp_rect
    try:
        rect = clamp_rect(spec["rect"])
    except (KeyError, ValueError) as exc:
        raise ValidationFailed(f"image placement needs a rectangle: {exc}") from exc
    opacity = float(spec.get("opacity", 1.0))
    with Image.open(image_path) as im:
        im.load()
        if im.width * im.height > 100_000_000:
            raise ValidationFailed("This image is too large")
        im = im.convert("RGBA")
    reader = ImageReader(im)
    pages = resolve_pages(spec.get("pages"), total)
    keep = bool(spec.get("keep_aspect", True))

    def draw(c: Any, i: int, dw: float, dh: float) -> None:
        _alpha(c, opacity)
        l, t, r, b = rect
        x, y, w, h = l * dw, (1 - b) * dh, (r - l) * dw, (b - t) * dh
        if keep:
            s = min(w / im.width, h / im.height)
            nw, nh = im.width * s, im.height * s
            x, y, w, h = x + (w - nw) / 2, y + (h - nh) / 2, nw, nh
        c.drawImage(reader, x, y, w, h, mask="auto")

    overlay_pages(writer, pages, draw)
    return len(pages)


def text_box(writer: Any, spec: dict[str, Any], total: int) -> int:
    """Wrapped text inside a normalised rectangle (``{rect, text, size, color, pages?, align?}``), burned into the page."""
    from reportlab.lib.utils import simpleSplit
    from .geometry import clamp_rect
    text = str(spec.get("text", ""))
    if not text.strip():
        raise ValidationFailed("The text box is empty")
    rect = clamp_rect(spec["rect"])
    size = float(spec.get("size", 12))
    color = hex_colour(spec.get("color"), (0, 0, 0))
    font = pick_font(text, bool(spec.get("bold")), spec.get("font"))
    pages = resolve_pages(spec.get("pages"), total)
    align = spec.get("align", "left")
    fill = spec.get("background")

    def draw(c: Any, i: int, dw: float, dh: float) -> None:
        l, t, r, b = rect
        x, top, w, h = l * dw, (1 - t) * dh, (r - l) * dw, (b - t) * dh
        if fill:
            c.setFillColorRGB(*hex_colour(fill, (1, 1, 1)))
            c.rect(x, top - h, w, h, stroke=0, fill=1)
        c.setFillColorRGB(*color)
        c.setFont(font, size)
        y = top - size
        for para in text.split("\n"):
            for line in simpleSplit(para, font, size, max(w - 4, 10)) or [""]:
                if y < top - h:
                    return
                if align == "center":
                    c.drawCentredString(x + w / 2, y, line)
                elif align == "right":
                    c.drawRightString(x + w - 2, y, line)
                else:
                    c.drawString(x + 2, y, line)
                y -= size * 1.2

    overlay_pages(writer, pages, draw)
    return len(pages)
