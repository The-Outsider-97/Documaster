"""Rendering PDF pages to images (thumbnails and previews) with pypdfium2. The PDF is never executed: pdfium draws it."""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from ..errors import DependencyMissing, ValidationFailed, WrongPassword
from ..pdfium_lock import pdfium_locked


def _pdfium() -> Any:
    try:
        import pypdfium2
    except ImportError as exc:
        raise DependencyMissing("Rendering PDF pages needs the 'pypdfium2' package (pip install pypdfium2)") from exc
    return pypdfium2


def open_pdfium(path: Path, password: str | None = None) -> Any:
    pdfium = _pdfium()
    try:
        return pdfium.PdfDocument(str(path), password=password)
    except pdfium.PdfiumError as exc:  # type: ignore[attr-defined]
        text = str(exc).lower()
        if "password" in text:
            raise WrongPassword("That password does not open this PDF") from exc
        raise ValidationFailed(f"This PDF could not be rendered: {exc}") from exc


@pdfium_locked
def render_page(path: Path, index: int, *, width_px: int | None = None, scale: float | None = None,
                password: str | None = None, fmt: str = "png", annotations: bool = True, forms: bool = True,
                quality: int = 85) -> tuple[bytes, str, tuple[int, int]]:
    """Return ``(bytes, media_type, (w, h))`` for page ``index`` (0-based) as displayed (rotation applied)."""
    doc = open_pdfium(path, password)
    try:
        if not 0 <= index < len(doc):
            raise ValidationFailed(f"Page {index + 1} does not exist (the document has {len(doc)} pages)")
        page = doc[index]
        try:
            w, h = page.get_size()
            if scale is None:
                scale = (width_px / w) if width_px else 1.0
            scale = min(max(scale, 0.05), 8.0)
            if w * scale * h * scale > 80_000_000:
                raise ValidationFailed("That render size is too large")
            bitmap = page.render(scale=scale, may_draw_forms=forms, draw_annots=annotations)
            image = bitmap.to_pil().convert("RGB")
        finally:
            page.close()
    finally:
        doc.close()
    buf = io.BytesIO()
    if fmt == "jpeg":
        image.save(buf, "JPEG", quality=quality, optimize=True)
        media = "image/jpeg"
    else:
        image.save(buf, "PNG", optimize=False)
        media = "image/png"
    return buf.getvalue(), media, image.size


@pdfium_locked
def page_sizes(path: Path, password: str | None = None) -> list[dict[str, Any]]:
    """Displayed size (points) and rotation of every page."""
    doc = open_pdfium(path, password)
    try:
        out = []
        for i in range(len(doc)):
            page = doc[i]
            try:
                w, h = page.get_size()
                out.append({"index": i, "width": round(w, 2), "height": round(h, 2), "rotation": page.get_rotation()})
            finally:
                page.close()
        return out
    finally:
        doc.close()
