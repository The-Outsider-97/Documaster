"""Standard PDF annotations with explicit appearance streams, so every viewer draws them the same way.

Supported: highlight, underline, strikeout, note (sticky comment), rect, ellipse, ink. Each carries an ``/NM`` id so it can be
listed and removed later. Colours are #rrggbb; geometry is normalised display coordinates (see ``geometry``).
"""
from __future__ import annotations

import datetime as _dt
import uuid
from typing import Any

from ..errors import ValidationFailed
from .geometry import clamp_rect, geometry_of, hex_colour, rect_to_user, to_user, from_user
from .stamp import resolve_pages

KINDS = ("highlight", "underline", "strikeout", "note", "rect", "ellipse", "ink")


def _pdf_date() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("D:%Y%m%d%H%M%SZ")


def _num(v: float) -> str:
    return f"{v:.3f}".rstrip("0").rstrip(".") or "0"


def _form(writer: Any, bbox: tuple[float, float, float, float], content: str, alpha: float = 1.0, multiply: bool = False) -> Any:
    from pypdf.generic import (ArrayObject, DecodedStreamObject, DictionaryObject, FloatObject, NameObject)
    stream = DecodedStreamObject()
    stream.set_data(content.encode("latin-1"))
    gs = DictionaryObject({NameObject("/Type"): NameObject("/ExtGState"), NameObject("/CA"): FloatObject(alpha),
                           NameObject("/ca"): FloatObject(alpha)})
    if multiply:
        gs[NameObject("/BM")] = NameObject("/Multiply")
    stream.update({
        NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Form"),
        NameObject("/BBox"): ArrayObject([FloatObject(v) for v in bbox]),
        NameObject("/Resources"): DictionaryObject({NameObject("/ExtGState"): DictionaryObject({NameObject("/GS"): gs})}),
    })
    return writer._add_object(stream)


def _ellipse_path(x0: float, y0: float, x1: float, y1: float) -> str:
    cx, cy, rx, ry = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2, (y1 - y0) / 2
    k = 0.5522847498
    p = [(cx + rx, cy), (cx + rx, cy + k * ry, cx + k * rx, cy + ry, cx, cy + ry),
         (cx - k * rx, cy + ry, cx - rx, cy + k * ry, cx - rx, cy),
         (cx - rx, cy - k * ry, cx - k * rx, cy - ry, cx, cy - ry),
         (cx + k * rx, cy - ry, cx + rx, cy - k * ry, cx + rx, cy)]
    s = f"{_num(p[0][0])} {_num(p[0][1])} m "
    for seg in p[1:]:
        s += " ".join(_num(v) for v in seg) + " c "
    return s + "h "


def add(writer: Any, spec: dict[str, Any], total: int, author: str) -> list[str]:
    """Add the annotation(s) described by ``spec`` (one per target page). Returns their ``/NM`` ids."""
    from pypdf.generic import (ArrayObject, DictionaryObject, FloatObject, NameObject, NumberObject, TextStringObject)
    kind = spec.get("type")
    if kind not in KINDS:
        raise ValidationFailed(f"Unknown annotation type {kind!r}", details={"supported": list(KINDS)})
    color = hex_colour(spec.get("color"), {"highlight": (1, 0.92, 0.2), "note": (1, 0.85, 0.1)}.get(kind, (0.85, 0.1, 0.1)))
    opacity = float(spec.get("opacity", 0.4 if kind == "highlight" else 1.0))
    width = float(spec.get("width", 1.5))
    contents = str(spec.get("note") or spec.get("text") or "")[:20000]
    if "page" in spec:
        pages = resolve_pages([int(spec["page"])], total)
    else:
        pages = resolve_pages(spec.get("pages"), total)
    ids: list[str] = []
    for pi in pages:
        page = writer.pages[pi]
        g = geometry_of(page)
        nm = "dm-" + uuid.uuid4().hex[:12]
        ann = DictionaryObject({
            NameObject("/Type"): NameObject("/Annot"), NameObject("/NM"): TextStringObject(nm),
            NameObject("/F"): NumberObject(4), NameObject("/T"): TextStringObject(author or "Documaster"),
            NameObject("/M"): TextStringObject(_pdf_date()),
        })
        if contents:
            ann[NameObject("/Contents")] = TextStringObject(contents)
        rgb = " ".join(_num(c) for c in color)
        if kind in ("highlight", "underline", "strikeout"):
            rects = spec.get("rects") or ([spec["rect"]] if "rect" in spec else None)
            if not rects:
                raise ValidationFailed(f"A {kind} needs at least one rectangle")
            user = [rect_to_user(clamp_rect(r), g) for r in rects]
            quad: list[float] = []
            for x0, y0, x1, y1 in user:
                quad += [x0, y1, x1, y1, x0, y0, x1, y0]
            bx0, by0 = min(r[0] for r in user), min(r[1] for r in user)
            bx1, by1 = max(r[2] for r in user), max(r[3] for r in user)
            if kind == "highlight":
                body = f"/GS gs {rgb} rg " + "".join(f"{_num(x0)} {_num(y0)} {_num(x1 - x0)} {_num(y1 - y0)} re f " for x0, y0, x1, y1 in user)
                subtype = "/Highlight"
                ap = _form(writer, (bx0, by0, bx1, by1), body, opacity, multiply=True)
            else:
                lines = ""
                for x0, y0, x1, y1 in user:
                    yy = y0 + 1 if kind == "underline" else (y0 + y1) / 2
                    lines += f"{_num(x0)} {_num(yy)} m {_num(x1)} {_num(yy)} l S "
                body = f"/GS gs {rgb} RG 1 w " + lines
                subtype = "/Underline" if kind == "underline" else "/StrikeOut"
                ap = _form(writer, (bx0, by0, bx1, by1), body, opacity)
            ann[NameObject("/Subtype")] = NameObject(subtype)
            ann[NameObject("/Rect")] = ArrayObject([FloatObject(v) for v in (bx0, by0, bx1, by1)])
            ann[NameObject("/QuadPoints")] = ArrayObject([FloatObject(v) for v in quad])
            ann[NameObject("/C")] = ArrayObject([FloatObject(v) for v in color])
            if kind == "highlight":
                ann[NameObject("/CA")] = FloatObject(opacity)
        elif kind == "note":
            u, v = spec.get("point", [0.5, 0.5])
            x, y = to_user(float(u), float(v), g)
            s = 18.0
            bx0, by0, bx1, by1 = x - s / 2, y - s / 2, x + s / 2, y + s / 2
            body = (f"{rgb} rg {_num(bx0)} {_num(by0)} {_num(s)} {_num(s)} re f 0.4 0.3 0 RG 1 w "
                    f"{_num(bx0)} {_num(by0)} {_num(s)} {_num(s)} re S "
                    f"{_num(bx0 + 3)} {_num(by0 + 6)} m {_num(bx1 - 3)} {_num(by0 + 6)} l S "
                    f"{_num(bx0 + 3)} {_num(by0 + 10)} m {_num(bx1 - 3)} {_num(by0 + 10)} l S ")
            ap = _form(writer, (bx0, by0, bx1, by1), "/GS gs " + body)
            ann[NameObject("/Subtype")] = NameObject("/Text")
            ann[NameObject("/Rect")] = ArrayObject([FloatObject(v) for v in (bx0, by0, bx1, by1)])
            ann[NameObject("/Name")] = NameObject("/Comment")
            ann[NameObject("/C")] = ArrayObject([FloatObject(c) for c in color])
            ann[NameObject("/F")] = NumberObject(4 | 8 | 16)     # print, no zoom, no rotate
            if not contents:
                raise ValidationFailed("A note needs text")
        elif kind in ("rect", "ellipse"):
            x0, y0, x1, y1 = rect_to_user(clamp_rect(spec["rect"]), g)
            pad = width
            fill = spec.get("fill")
            path = f"{_num(x0)} {_num(y0)} {_num(x1 - x0)} {_num(y1 - y0)} re " if kind == "rect" else _ellipse_path(x0, y0, x1, y1)
            op = "B" if fill else "S"
            fill_rgb = " ".join(_num(c) for c in hex_colour(fill, (1, 1, 1))) if fill else ""
            body = f"/GS gs {rgb} RG {_num(width)} w " + (f"{fill_rgb} rg " if fill else "") + path + op
            ap = _form(writer, (x0 - pad, y0 - pad, x1 + pad, y1 + pad), body, opacity)
            ann[NameObject("/Subtype")] = NameObject("/Square" if kind == "rect" else "/Circle")
            ann[NameObject("/Rect")] = ArrayObject([FloatObject(v) for v in (x0 - pad, y0 - pad, x1 + pad, y1 + pad)])
            ann[NameObject("/C")] = ArrayObject([FloatObject(c) for c in color])
            ann[NameObject("/BS")] = DictionaryObject({NameObject("/W"): FloatObject(width)})
            if fill:
                ann[NameObject("/IC")] = ArrayObject([FloatObject(c) for c in hex_colour(fill, (1, 1, 1))])
        else:  # ink
            strokes = spec.get("strokes")
            if not strokes or len(strokes) > 500:
                raise ValidationFailed("A drawing needs between 1 and 500 strokes")
            ink: list[Any] = []
            body = f"/GS gs {rgb} RG {_num(width)} w 1 J 1 j "
            xs: list[float] = []
            ys: list[float] = []
            for stroke in strokes:
                if len(stroke) < 2 or len(stroke) > 20000:
                    raise ValidationFailed("Each stroke needs at least two points")
                pts = [to_user(min(1, max(0, float(p[0]))), min(1, max(0, float(p[1]))), g) for p in stroke]
                ink.append(ArrayObject([FloatObject(c) for pt in pts for c in pt]))
                body += f"{_num(pts[0][0])} {_num(pts[0][1])} m " + "".join(f"{_num(x)} {_num(y)} l " for x, y in pts[1:]) + "S "
                xs += [p[0] for p in pts]
                ys += [p[1] for p in pts]
            pad = width + 1
            bbox = (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)
            ap = _form(writer, bbox, body, opacity)
            ann[NameObject("/Subtype")] = NameObject("/Ink")
            ann[NameObject("/Rect")] = ArrayObject([FloatObject(v) for v in bbox])
            ann[NameObject("/InkList")] = ArrayObject(ink)
            ann[NameObject("/C")] = ArrayObject([FloatObject(c) for c in color])
            ann[NameObject("/BS")] = DictionaryObject({NameObject("/W"): FloatObject(width)})
        ann[NameObject("/AP")] = DictionaryObject({NameObject("/N"): ap})
        if "/Annots" not in page:
            page[NameObject("/Annots")] = ArrayObject()
        page["/Annots"].append(writer._add_object(ann))
        ids.append(nm)
    return ids


def listing(reader: Any) -> list[dict[str, Any]]:
    """Existing annotations (links and form widgets excluded)."""
    out: list[dict[str, Any]] = []
    for pi, page in enumerate(reader.pages):
        annots = page.get("/Annots")
        if not annots:
            continue
        g = geometry_of(page)
        for ai, ref in enumerate(annots):
            try:
                a = ref.get_object()
            except Exception:  # noqa: BLE001
                continue
            sub = str(a.get("/Subtype", ""))
            if sub in ("/Link", "/Widget", "/Popup"):
                continue
            try:
                r = [float(v) for v in a.get("/Rect", [0, 0, 0, 0])]
                (l, t), (rr, b) = from_user(r[0], r[3], g), from_user(r[2], r[1], g)
                rect = [min(l, rr), min(t, b), max(l, rr), max(t, b)]
            except Exception:  # noqa: BLE001
                rect = None
            out.append({"page": pi, "index": ai, "id": str(a.get("/NM")) if a.get("/NM") is not None else None,
                        "type": sub.lstrip("/"), "text": str(a.get("/Contents", "") or ""), "author": str(a.get("/T", "") or ""),
                        "rect": rect, "ours": str(a.get("/NM", "")).startswith("dm-")})
    return out


def remove(writer: Any, selectors: list[dict[str, Any]]) -> int:
    """Remove annotations by ``{id}`` or ``{page, index}``. Returns how many were removed."""
    removed = 0
    by_page: dict[int, set[int]] = {}
    ids = {s["id"] for s in selectors if s.get("id")}
    for s in selectors:
        if "page" in s and "index" in s:
            by_page.setdefault(int(s["page"]), set()).add(int(s["index"]))
    for pi, page in enumerate(writer.pages):
        annots = page.get("/Annots")
        if not annots:
            continue
        keep = []
        for ai, ref in enumerate(annots):
            a = ref.get_object()
            if str(a.get("/NM", "")) in ids or ai in by_page.get(pi, ()):
                removed += 1
                continue
            keep.append(ref)
        from pypdf.generic import ArrayObject, NameObject
        page[NameObject("/Annots")] = ArrayObject(keep)
    return removed


def edit_note(writer: Any, selector: dict[str, Any], text: str) -> bool:
    """Change the text of an existing sticky-note annotation without replacing its identity.

    Editing other annotation kinds can invalidate their appearance/geometry, so this
    deliberately accepts only /Text annotations. The visible note icon has no text in
    its appearance stream; viewers read the updated /Contents field when opened.
    """
    from pypdf.generic import NameObject, TextStringObject

    if not isinstance(text, str) or not text.strip() or len(text) > 20000:
        raise ValidationFailed("A comment needs between 1 and 20000 characters")
    name = selector.get("id")
    page_no, index = selector.get("page"), selector.get("index")
    if not name and (not isinstance(page_no, int) or not isinstance(index, int)):
        raise ValidationFailed("Identify the comment by id or page and index")
    for pi, page in enumerate(writer.pages):
        for ai, ref in enumerate(page.get("/Annots", [])):
            a = ref.get_object()
            found = str(a.get("/NM", "")) == name if name else (pi == page_no and ai == index)
            if not found:
                continue
            if str(a.get("/Subtype")) != "/Text":
                raise ValidationFailed("Only sticky-note comments can be edited here")
            if str(a.get("/Contents", "")) == text:
                return False
            a[NameObject("/Contents")] = TextStringObject(text)
            a[NameObject("/M")] = TextStringObject(_pdf_date())
            return True
    raise ValidationFailed("The selected comment no longer exists")
