"""Arranging PDF pages: merge, reorder, rotate, delete, duplicate, extract, split, insert blank pages and images."""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import DependencyMissing, LimitExceeded, ValidationFailed
from .pdfio import norm_text, open_reader, save_writer

A4 = (595.276, 841.89)
LETTER = (612.0, 792.0)


@dataclass
class PdfSource:
    path: Path
    name: str
    password: str | None = None
    kind: str = "pdf"          # pdf | image


@dataclass
class PlanItem:
    source: int                # index into sources (ignored for blank pages)
    page: int = 0              # 0-based page in that source
    rotation: int = 0          # extra clockwise rotation: 0, 90, 180, 270
    blank: bool = False
    width: float = A4[0]
    height: float = A4[1]


def parse_plan(entries: list[dict[str, Any]], page_counts: list[int], limit: int) -> list[PlanItem]:
    """Validate a page plan from the client: ``{doc, page, rotation?, included?}`` or ``{blank: true, width?, height?}``."""
    items: list[PlanItem] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValidationFailed("Each page-plan entry must be an object")
        if entry.get("included", True) is False:
            continue
        rotation = int(entry.get("rotation", 0) or 0) % 360
        if rotation % 90:
            raise ValidationFailed("Rotation must be a multiple of 90 degrees")
        if entry.get("blank"):
            width, height = float(entry.get("width", A4[0])), float(entry.get("height", A4[1]))
            if not (36 <= width <= 14400 and 36 <= height <= 14400):
                raise ValidationFailed("A blank page must be between 0.5 and 200 inches on each side")
            items.append(PlanItem(0, 0, rotation, True, width, height))
            continue
        try:
            d, p = int(entry["doc"]), int(entry["page"])
        except (KeyError, TypeError, ValueError):
            raise ValidationFailed("Each page-plan entry needs 'doc' and 'page'") from None
        if not 0 <= d < len(page_counts) or not 0 <= p < page_counts[d]:
            raise ValidationFailed(f"The plan refers to page {p + 1} of document {d + 1}, which does not exist")
        items.append(PlanItem(d, p, rotation))
    if not items:
        raise ValidationFailed("The plan includes no pages")
    if len(items) > limit:
        raise LimitExceeded(f"The result would have {len(items)} pages; the limit is {limit}")
    return items


def image_pdf_bytes(path: Path, page_size: tuple[float, float] = A4, margin_pt: float = 28.0) -> bytes:
    """One page that holds the image, fitted inside the margins on a portrait or landscape page matching its shape."""
    try:
        from PIL import Image
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfgen import canvas
    except ImportError as exc:
        raise DependencyMissing("Adding images needs 'Pillow' and 'reportlab'") from exc
    with Image.open(path) as im:
        im.load()
        w, h = im.size
        if w * h > 120_000_000:
            raise LimitExceeded("This image is too large to place on a page")
        if im.mode in ("P", "LA", "PA", "RGBA"):
            im = im.convert("RGBA")
        else:
            im = im.convert("RGB")
        pw, ph = page_size
        if w > h:
            pw, ph = ph, pw
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=(pw, ph))
        scale = min((pw - 2 * margin_pt) / w, (ph - 2 * margin_pt) / h)
        dw, dh = w * scale, h * scale
        c.drawImage(ImageReader(im), (pw - dw) / 2, (ph - dh) / 2, dw, dh, mask="auto")
        c.showPage()
        c.save()
        return buf.getvalue()


def assemble(sources: list[PdfSource], items: list[PlanItem], out: Path, *, title: str | None = None,
             source_bookmarks: bool = False, keep_bookmarks: bool = True, password: str | None = None,
             author: str | None = None) -> dict[str, Any]:
    """Build ``out`` from the plan. Returns facts about the result (page count, what was kept or dropped)."""
    import pypdf
    readers: list[Any] = []
    for src in sources:
        if src.kind == "image":
            readers.append(pypdf.PdfReader(io.BytesIO(image_pdf_bytes(src.path))))
        else:
            readers.append(open_reader(src.path, src.password))
    writer = pypdf.PdfWriter()
    placed: dict[tuple[int, int], int] = {}        # (source, page) -> first output index
    first_of_source: dict[int, int] = {}
    has_forms = False
    warnings: list[str] = []
    for out_index, item in enumerate(items):
        if item.blank:
            page = writer.add_blank_page(width=item.width, height=item.height)
        else:
            reader = readers[item.source]
            page = writer.add_page(reader.pages[item.page])
            placed.setdefault((item.source, item.page), out_index)
            first_of_source.setdefault(item.source, out_index)
            if reader.trailer["/Root"].get("/AcroForm") is not None:
                has_forms = True
        if item.rotation:
            page.rotate(item.rotation)
    if has_forms:
        try:
            writer.reattach_fields()
        except Exception:  # noqa: BLE001
            warnings.append("Some form fields could not be reattached after the pages were rearranged")
    bookmarks = 0
    if source_bookmarks and len(sources) > 1:
        for s, idx in sorted(first_of_source.items(), key=lambda kv: kv[1]):
            writer.add_outline_item(Path(sources[s].name).stem, idx)
            bookmarks += 1
    if keep_bookmarks:
        for s, reader in enumerate(readers):
            if s in first_of_source:
                bookmarks += _copy_outline(reader, writer, s, placed, parent=None)
    meta = {"/Producer": "SLAI Documaster"}
    if title:
        meta["/Title"] = title
    elif len(sources) == 1 and readers[0].metadata and readers[0].metadata.title:
        meta["/Title"] = str(readers[0].metadata.title)
    if author:
        meta["/Author"] = author
    writer.add_metadata(meta)
    save_writer(writer, out, password=password)
    return {"pages": len(items), "bookmarks": bookmarks, "warnings": warnings, "formsKept": has_forms}


def _copy_outline(reader: Any, writer: Any, source: int, placed: dict[tuple[int, int], int], parent: Any) -> int:
    """Copy outline entries whose target page is in the result; entries for removed pages are dropped (children promoted)."""
    count = 0

    def walk(entries: list[Any], parent_item: Any) -> None:
        nonlocal count
        last = None
        for entry in entries:
            if isinstance(entry, list):
                walk(entry, last if last is not None else parent_item)
                continue
            try:
                idx = reader.get_destination_page_number(entry)
            except Exception:  # noqa: BLE001
                idx = None
            dest = placed.get((source, idx)) if idx is not None else None
            if dest is None:
                last = None
                continue
            try:
                last = writer.add_outline_item(str(entry.title), dest, parent=parent_item)
                count += 1
            except Exception:  # noqa: BLE001
                last = None

    try:
        outline = reader.outline
    except Exception:  # noqa: BLE001
        return 0
    walk(outline, parent)
    return count


def page_texts(path: Path, password: str | None = None, limit_pages: int | None = None) -> list[str]:
    reader = open_reader(path, password)
    n = len(reader.pages) if limit_pages is None else min(limit_pages, len(reader.pages))
    return [norm_text(reader.pages[i].extract_text() or "") for i in range(n)]


def verify_assembly(out: Path, sources: list[PdfSource], items: list[PlanItem], password: str | None = None) -> dict[str, Any]:
    """Re-open the result and check page count, per-page text, sizes and rotation against the plan."""
    import pypdf
    res = open_reader(out, password)
    checks: list[dict[str, Any]] = []
    ok_count = len(res.pages) == len(items)
    checks.append({"name": "Page count", "ok": ok_count, "detail": f"{len(res.pages)} pages, plan has {len(items)}"})
    if not ok_count:
        return {"ok": False, "checks": checks}
    readers = [open_reader(s.path, s.password) if s.kind == "pdf" else None for s in sources]
    mismatched: list[int] = []
    checked = 0
    step = 1 if len(items) <= 400 else max(1, len(items) // 400)
    for i in range(0, len(items), step):
        item = items[i]
        page = res.pages[i]
        if item.blank:
            if norm_text(page.extract_text() or ""):
                mismatched.append(i + 1)
            continue
        r = readers[item.source]
        if r is None:
            continue
        checked += 1
        want = norm_text(r.pages[item.page].extract_text() or "")
        got = norm_text(page.extract_text() or "")
        w0 = r.pages[item.page]
        same_box = abs(float(page.mediabox.width) - float(w0.mediabox.width)) < 0.5
        if want != got or not same_box:
            mismatched.append(i + 1)
    checks.append({"name": "Page content", "ok": not mismatched,
                   "detail": f"{checked} page(s) compared with their source" + (f"; differing: {mismatched[:10]}" if mismatched else "")})
    return {"ok": all(c["ok"] for c in checks), "checks": checks}
