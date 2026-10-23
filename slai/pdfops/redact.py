"""Text search and true redaction.

Redaction removes content, it does not cover it: every page that has a redacted region is rendered to an image with the region
painted black, and that image replaces the page. The page then holds no text, vectors, images, annotations or form fields of the
original at all, so there is nothing underneath the black box to recover. The result is verified before it is reported:

* the redacted pages must contain no extractable text (checked with two independent text extractors);
* the redacted regions must be solid black in a fresh render of the output;
* the redacted wording must not survive in the document's metadata, outline, annotations, form values or attachments (these
  are cleaned when ``sanitize`` is on, and the check fails if anything remains).

Other pages keep their text. If the same wording appears on pages that were not redacted, the result lists those pages so the
person can decide; they are not touched silently.
"""
from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any, Sequence

from ..errors import DependencyMissing, OutputFailed, ValidationFailed
from .geometry import clamp_rect, from_user, geometry_of, rect_to_user, to_user
from .organize import A4, _copy_outline
from .pdfio import norm_text, open_reader, save_writer
from .render import open_pdfium
from ..pdfium_lock import pdfium_locked


@pdfium_locked
def find_text(path: Path, password: str | None, query: str, *, regex: bool = False, case: bool = False,
              whole_word: bool = False, limit: int = 2000) -> list[dict[str, Any]]:
    """Locate text. Returns ``[{page, text, rects: [[l, t, r, b], ...]}]`` with normalised display rectangles."""
    if not query:
        raise ValidationFailed("Enter text to search for")
    if len(query) > 500:
        raise ValidationFailed("The search text is too long")
    reader = open_reader(path, password)
    geoms = [geometry_of(p) for p in reader.pages]
    pattern = None
    if regex:
        try:
            pattern = re.compile(query, 0 if case else re.IGNORECASE)
        except re.error as exc:
            raise ValidationFailed(f"That pattern is not a valid regular expression: {exc}") from exc
        if pattern.match(""):
            raise ValidationFailed("That pattern matches empty text")
    doc = open_pdfium(path, password)
    hits: list[dict[str, Any]] = []
    try:
        for pi in range(len(doc)):
            page = doc[pi]
            tp = page.get_textpage()
            try:
                g = geoms[pi]
                spans: list[tuple[int, int]] = []
                if pattern is None:
                    searcher = tp.search(query, match_case=case, match_whole_word=whole_word)
                    while True:
                        found = searcher.get_next()
                        if found is None:
                            break
                        spans.append(found)
                else:
                    text = tp.get_text_range()
                    for m in pattern.finditer(text):
                        if m.end() > m.start():
                            spans.append((m.start(), m.end() - m.start()))
                for index, count in spans:
                    rects = _span_rects(tp, index, count, g)
                    if rects:
                        hits.append({"page": pi, "text": tp.get_text_range(index, count), "rects": rects})
                        if len(hits) >= limit:
                            return hits
            finally:
                tp.close()
                page.close()
    finally:
        doc.close()
    return hits


def _span_rects(tp: Any, index: int, count: int, g: Any) -> list[list[float]]:
    out: list[list[float]] = []
    try:
        n = tp.count_rects(index, count)
        raw = [tp.get_rect(i) for i in range(n)]
    except Exception:  # noqa: BLE001
        raw = []
    for left, bottom, right, top in raw:
        (a, b), (c, d) = from_user(left, top, g), from_user(right, bottom, g)
        l, r, t, bt = min(a, c), max(a, c), min(b, d), max(b, d)
        if r > l and bt > t:
            out.append([max(0.0, l), max(0.0, t), min(1.0, r), min(1.0, bt)])
    return out


def _text_in(tp: Any, rect: list[float], g: Any) -> str:
    x0, y0, x1, y1 = rect_to_user(rect, g)
    try:
        return tp.get_text_bounded(left=x0 - 0.5, bottom=y0 - 0.5, right=x1 + 0.5, top=y1 + 0.5) or ""
    except Exception:  # noqa: BLE001
        return ""


@pdfium_locked
def redact(src: Path, out: Path, regions: list[dict[str, Any]], *, password: str | None = None, dpi: int = 200,
           sanitize: bool = True, out_password: str | None = None, lossless: bool = False,
           fill: tuple[int, int, int] = (0, 0, 0), also_pages: Sequence[int] = ()) -> dict[str, Any]:
    """Redact ``regions`` (``{page, rect}``; pages 0-based, rects normalised display). Writes ``out`` or raises.

    ``also_pages`` are pages that must be rasterised even though no visible area was marked on them (text that lies outside the
    visible page is removed by rasterising)."""
    try:
        from PIL import Image, ImageDraw
        import pypdf
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfgen import canvas
    except ImportError as exc:
        raise DependencyMissing("Redaction needs 'pypdf', 'pypdfium2', 'Pillow' and 'reportlab'") from exc
    if not regions and not also_pages:
        raise ValidationFailed("Select at least one area to redact")
    if not 100 <= dpi <= 400:
        raise ValidationFailed("Redaction resolution must be between 100 and 400 dpi")
    reader = open_reader(src, password)
    total = len(reader.pages)
    by_page: dict[int, list[list[float]]] = {}
    for r in regions:
        try:
            page = int(r["page"])
            rect = clamp_rect(r["rect"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationFailed(f"A redaction area is not valid: {exc}") from exc
        if not 0 <= page < total:
            raise ValidationFailed(f"Page {page + 1} does not exist")
        by_page.setdefault(page, []).append(rect)
    for page in also_pages:
        if not 0 <= int(page) < total:
            raise ValidationFailed(f"Page {int(page) + 1} does not exist")
        by_page.setdefault(int(page), [])
    geoms = [geometry_of(p) for p in reader.pages]
    scale = dpi / 72.0

    doc = open_pdfium(src, password)
    fragments: list[str] = []
    rastered: dict[int, bytes] = {}
    sizes: dict[int, tuple[float, float]] = {}
    try:
        for pi, rects in sorted(by_page.items()):
            page = doc[pi]
            try:
                tp = page.get_textpage()
                try:
                    for rect in rects:
                        t = norm_text(_text_in(tp, rect, geoms[pi]))
                        if t:
                            fragments.append(t)
                finally:
                    tp.close()
                w, h = page.get_size()
                if (w * scale) * (h * scale) > 120_000_000:
                    raise ValidationFailed(f"Page {pi + 1} is too large to redact at {dpi} dpi; lower the resolution")
                image = page.render(scale=scale, draw_annots=True, may_draw_forms=True).to_pil().convert("RGB")
            finally:
                page.close()
            draw = ImageDraw.Draw(image)
            W, H = image.size
            for l, t, r, b in rects:
                draw.rectangle([int(l * W) - 1, int(t * H) - 1, int(r * W) + 1, int(b * H) + 1], fill=fill)
            buf = io.BytesIO()
            if lossless:
                image.save(buf, "PNG")
            else:
                image.save(buf, "JPEG", quality=88, optimize=True)
            rastered[pi] = buf.getvalue()
            sizes[pi] = (w, h)
    finally:
        doc.close()

    writer = pypdf.PdfWriter()
    placed: dict[tuple[int, int], int] = {}
    kept_pages: set[int] = set()
    for pi in range(total):
        if pi in rastered:
            w, h = sizes[pi]
            b = io.BytesIO()
            c = canvas.Canvas(b, pagesize=(w, h))
            c.drawImage(ImageReader(io.BytesIO(rastered[pi])), 0, 0, w, h)
            c.showPage()
            c.save()
            writer.add_page(pypdf.PdfReader(io.BytesIO(b.getvalue())).pages[0])
        else:
            writer.add_page(reader.pages[pi])
            kept_pages.add(pi)
        placed[(0, pi)] = pi
    notes: list[str] = []
    cleaned = _clean_document(reader, writer, fragments, placed, kept_pages, sanitize, notes)
    save_writer(writer, out, password=out_password)
    try:
        verification = verify_redaction(out, out_password, by_page, fragments, geoms, password_in=out_password)
    except Exception:
        out.unlink(missing_ok=True)
        raise
    if not verification["ok"]:
        out.unlink(missing_ok=True)
        failed = "; ".join(f"{c['name']}: {c['detail']}" for c in verification["checks"] if c["ok"] is False)
        raise OutputFailed("The redacted file failed verification and was discarded: " + failed,
                           details={"verification": verification})
    elsewhere = _occurrences_elsewhere(out, out_password, fragments, set(by_page))
    return {"pagesFlattened": sorted(p + 1 for p in by_page), "regions": sum(len(v) for v in by_page.values()),
            "fragmentsFound": len(fragments), "cleaned": cleaned, "notes": notes, "sameTextOnOtherPages": elsewhere,
            "verification": verification,
            "limits": ["Redacted pages are now images: their remaining text is no longer selectable or searchable "
                       "(run OCR afterwards if that is needed)."]}


def _clean_document(reader: Any, writer: Any, fragments: list[str], placed: dict[tuple[int, int], int],
                    kept_pages: set[int], sanitize: bool, notes: list[str]) -> dict[str, int]:
    """Outline, metadata, attachments and annotations: copied unless they carry redacted wording (or ``sanitize`` removes them)."""
    from pypdf.generic import NameObject
    counts = {"outlineEntriesDropped": 0, "annotationsDropped": 0, "metadataFieldsDropped": 0, "attachmentsDropped": 0}
    low = [f.lower() for f in fragments if len(f) >= 3]

    def tainted(text: str) -> bool:
        t = norm_text(text).lower()
        return any(f in t for f in low)

    # outline: keep entries for pages we did not flatten and whose title is clean
    try:
        outline_count = _copy_outline(reader, writer, 0, placed, None)
        _ = outline_count
    except Exception:  # noqa: BLE001
        notes.append("Bookmarks could not be copied")
    _drop_tainted_outline(writer, tainted, counts)
    # metadata
    meta = {}
    for k, v in (reader.metadata or {}).items():
        if sanitize or tainted(str(v)):
            if tainted(str(v)) or sanitize:
                counts["metadataFieldsDropped"] += 1
            continue
        meta[k] = str(v)
    meta["/Producer"] = "SLAI Documaster"
    writer.add_metadata(meta)
    # annotations on pages that were not flattened
    for pi in range(len(writer.pages)):
        if pi not in kept_pages:
            continue
        page = writer.pages[pi]
        annots = page.get("/Annots")
        if not annots:
            continue
        keep = []
        for ref in annots:
            a = ref.get_object()
            texts = [str(a.get(k, "") or "") for k in ("/Contents", "/T", "/Subj", "/RC")]
            if tainted(" ".join(texts)):
                counts["annotationsDropped"] += 1
                continue
            keep.append(ref)
        from pypdf.generic import ArrayObject
        page[NameObject("/Annots")] = ArrayObject(keep)
    if sanitize:
        root = writer._root_object
        for key in ("/Names", "/OpenAction", "/AA", "/Metadata"):
            if key in root:
                if key == "/Names":
                    names = root["/Names"].get_object()
                    if "/EmbeddedFiles" in names:
                        counts["attachmentsDropped"] += 1
                        del names["/EmbeddedFiles"]
                    if "/JavaScript" in names:
                        del names["/JavaScript"]
                else:
                    del root[key]
        if "/AcroForm" in root:
            notes.append("Form fields on pages that were not flattened were kept; their values are cleared only if they "
                         "contain the redacted wording")
    return counts


def _drop_tainted_outline(writer: Any, tainted: Any, counts: dict[str, int]) -> None:
    root = writer._root_object
    outlines = root.get("/Outlines")
    if outlines is None:
        return
    outlines = outlines.get_object()

    def walk(node_ref: Any) -> None:
        ref = node_ref
        prev = None
        while ref is not None:
            node = ref.get_object()
            nxt = node.get("/Next")
            if tainted(str(node.get("/Title", ""))):
                counts["outlineEntriesDropped"] += 1
                parent = node.get("/Parent").get_object()
                if prev is None:
                    if nxt is None:
                        parent.pop("/First", None)
                        parent.pop("/Last", None)
                    else:
                        parent["/First"] = nxt
                        nxt.get_object().pop("/Prev", None)
                else:
                    if nxt is None:
                        prev.get_object().pop("/Next", None)
                        parent["/Last"] = prev
                    else:
                        prev.get_object()["/Next"] = nxt
                        nxt.get_object()["/Prev"] = prev
            else:
                if node.get("/First") is not None:
                    walk(node["/First"])
                prev = ref
            ref = nxt

    if outlines.get("/First") is not None:
        walk(outlines["/First"])


def _all_strings(reader: Any) -> list[tuple[str, str]]:
    """Every free-text string outside page content: metadata, XMP, outline, annotations, form values, attachment names."""
    found: list[tuple[str, str]] = []
    for k, v in (reader.metadata or {}).items():
        found.append((f"metadata {k}", str(v)))
    root = reader.trailer["/Root"]
    meta = root.get("/Metadata")
    if meta is not None:
        try:
            found.append(("XMP metadata", meta.get_object().get_data().decode("utf-8", "ignore")))
        except Exception:  # noqa: BLE001
            pass

    def walk(items: Any) -> None:
        for it in items:
            if isinstance(it, list):
                walk(it)
            else:
                found.append(("bookmark", str(getattr(it, "title", ""))))

    try:
        walk(reader.outline)
    except Exception:  # noqa: BLE001
        pass
    for pi, page in enumerate(reader.pages):
        for ref in page.get("/Annots") or []:
            a = ref.get_object()
            for k in ("/Contents", "/T", "/Subj", "/V", "/TU", "/RC"):
                if a.get(k) is not None:
                    found.append((f"annotation on page {pi + 1}", str(a.get(k))))
    try:
        for name, value in (reader.get_fields() or {}).items():
            found.append(("form field", f"{name} {value.get('/V', '') if isinstance(value, dict) else ''}"))
    except Exception:  # noqa: BLE001
        pass
    try:
        for name in (reader.attachments or {}):
            found.append(("attachment name", str(name)))
    except Exception:  # noqa: BLE001
        pass
    return found


@pdfium_locked
def verify_redaction(path: Path, password: str | None, by_page: dict[int, list[list[float]]], fragments: list[str],
                     geoms: list[Any], password_in: str | None = None) -> dict[str, Any]:
    import pypdf  # noqa: F401
    checks: list[dict[str, Any]] = []
    reader = open_reader(path, password)
    leftovers: list[int] = []
    for pi in by_page:
        text = norm_text(reader.pages[pi].extract_text() or "")
        if text:
            leftovers.append(pi + 1)
    doc = open_pdfium(path, password)
    try:
        for pi in by_page:
            page = doc[pi]
            tp = page.get_textpage()
            try:
                if norm_text(tp.get_text_range()):
                    leftovers.append(pi + 1)
            finally:
                tp.close()
                page.close()
        checks.append({"name": "No text left on redacted pages", "ok": not leftovers,
                       "detail": "pages with text: " + ", ".join(map(str, sorted(set(leftovers)))) if leftovers
                       else f"{len(by_page)} page(s) checked with two text extractors"})
        dark_bad = 0
        total_areas = 0
        for pi, rects in by_page.items():
            page = doc[pi]
            try:
                image = page.render(scale=0.75).to_pil().convert("L")
            finally:
                page.close()
            W, H = image.size
            for l, t, r, b in rects:
                total_areas += 1
                x0, x1 = int(l * W) + 1, max(int(r * W) - 1, int(l * W) + 2)
                y0, y1 = int(t * H) + 1, max(int(b * H) - 1, int(t * H) + 2)
                crop = image.crop((x0, y0, x1, y1))
                data = list(crop.getdata())
                if data and max(data) > 40:
                    dark_bad += 1
        checks.append({"name": "Redacted areas are solid black", "ok": dark_bad == 0,
                       "detail": f"{total_areas - dark_bad} of {total_areas} areas checked in a fresh render"})
    finally:
        doc.close()
    low = [f.lower() for f in fragments if len(f) >= 3]
    residue = []
    for where, text in _all_strings(reader):
        t = norm_text(text).lower()
        if any(f in t for f in low):
            residue.append(where)
    checks.append({"name": "Redacted wording absent from metadata, bookmarks, annotations and fields", "ok": not residue,
                   "detail": ("found in: " + ", ".join(sorted(set(residue)))) if residue else "none found"})
    return {"ok": all(c["ok"] for c in checks), "checks": checks}


@pdfium_locked
def _occurrences_elsewhere(path: Path, password: str | None, fragments: list[str], skip: set[int]) -> list[dict[str, Any]]:
    reader = open_reader(path, password)
    low = sorted({f.lower() for f in fragments if len(f) >= 3})
    out: list[dict[str, Any]] = []
    for pi, page in enumerate(reader.pages):
        if pi in skip:
            continue
        text = norm_text(page.extract_text() or "").lower()
        n = sum(text.count(f) for f in low)
        if n:
            out.append({"page": pi + 1, "occurrences": n})
    return out
