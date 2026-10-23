"""AcroForm fields: list, fill, flatten."""
from __future__ import annotations

from ..pdfium_lock import pdfium_locked

from typing import Any

from ..errors import UnsupportedOperation, ValidationFailed
from .geometry import from_user, geometry_of


def list_fields(reader: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        fields = reader.get_fields() or {}
    except Exception:  # noqa: BLE001
        return out
    pages = {}
    for pi, page in enumerate(reader.pages):
        g = geometry_of(page)
        for ref in page.get("/Annots") or []:
            a = ref.get_object()
            if a.get("/Subtype") != "/Widget":
                continue
            name = a.get("/T")
            parent = a.get("/Parent")
            full = str(name) if name is not None else None
            if full is None and parent is not None:
                full = str(parent.get_object().get("/T", ""))
            try:
                r = [float(v) for v in a["/Rect"]]
                (l, t), (rr, b) = from_user(r[0], r[3], g), from_user(r[2], r[1], g)
                rect = [min(l, rr), min(t, b), max(l, rr), max(t, b)]
            except Exception:  # noqa: BLE001
                rect = None
            pages.setdefault(full, []).append({"page": pi, "rect": rect})
    for name, f in fields.items():
        ft = str(f.get("/FT", ""))
        kind = {"/Tx": "text", "/Btn": "checkbox/button", "/Ch": "choice", "/Sig": "signature"}.get(ft, ft or "unknown")
        value = f.get("/V")
        out.append({"name": name, "type": kind, "value": None if value is None else str(value),
                    "options": [str(o) for o in f.get("/_States_", [])] if f.get("/_States_") else None,
                    "readOnly": bool(int(f.get("/Ff", 0)) & 1) if f.get("/Ff") is not None else False,
                    "locations": pages.get(name.split(".")[-1], [])})
    return out


def fill(writer: Any, values: dict[str, Any], *, flatten: bool = False) -> dict[str, Any]:
    if not isinstance(values, dict) or not values:
        raise ValidationFailed("Give at least one field value")
    if writer._root_object.get("/AcroForm") is None:
        raise UnsupportedOperation("This PDF has no fillable form fields")
    known = {f["name"]: f for f in list_fields(writer)}
    missing = [k for k in values if k not in known]
    if missing:
        raise ValidationFailed("Unknown field(s): " + ", ".join(missing[:10]), details={"fields": sorted(known)[:50]})
    for k in values:
        if known[k]["type"] == "signature":
            raise UnsupportedOperation("Signature fields are not filled by Documaster (it never signs for anyone)")
    applied = 0
    for page in writer.pages:
        names = {str(ref.get_object().get("/T")) for ref in (page.get("/Annots") or [])
                 if ref.get_object().get("/T") is not None}
        subset = {k: str(v) if not isinstance(v, bool) else ("/Yes" if v else "/Off") for k, v in values.items() if k in names}
        if subset:
            writer.update_page_form_field_values(page, subset, auto_regenerate=False)
            applied += len(subset)
    writer.set_need_appearances_writer(True)
    return {"filled": len(values), "applied": applied}


@pdfium_locked
def flatten_file(src: Any, dst: Any, password: str | None = None, *, forms_only: bool = False) -> int:
    """Make form fields (and, unless ``forms_only``, annotations) part of the page content using pdfium. Returns pages touched."""
    from .render import open_pdfium
    doc = open_pdfium(src, password)
    try:
        try:
            doc.init_forms()
        except Exception:  # noqa: BLE001
            pass
        n = len(doc)
        for i in range(n):
            page = doc[i]
            try:
                page.flatten()
            finally:
                page.close()
        doc.save(str(dst))
    finally:
        doc.close()
    return n
