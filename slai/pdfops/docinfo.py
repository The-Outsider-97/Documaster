"""Metadata, bookmarks, attachments and sanitising."""
from __future__ import annotations

from typing import Any

from ..errors import ValidationFailed

META_KEYS = {"title": "/Title", "author": "/Author", "subject": "/Subject", "keywords": "/Keywords", "creator": "/Creator"}


def set_metadata(writer: Any, values: dict[str, Any]) -> list[str]:
    changed: list[str] = []
    meta = {}
    for key, pdf_key in META_KEYS.items():
        if key in values:
            v = values[key]
            if v is not None and not isinstance(v, str):
                raise ValidationFailed(f"{key} must be text")
            if v and len(v) > 2000:
                raise ValidationFailed(f"{key} is too long")
            meta[pdf_key] = v or ""
            changed.append(key)
    if not changed:
        raise ValidationFailed("Give at least one of: " + ", ".join(META_KEYS))
    writer.add_metadata(meta)
    return changed


def read_metadata(reader: Any) -> dict[str, Any]:
    m = reader.metadata
    out = {k: (str(m.get(v)) if m and m.get(v) is not None else "") for k, v in META_KEYS.items()}
    out["pages"] = len(reader.pages)
    out["pdfVersion"] = getattr(reader, "pdf_header", "")
    return out


def outline_tree(reader: Any) -> list[dict[str, Any]]:
    def walk(items: list[Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for it in items:
            if isinstance(it, list):
                if out:
                    out[-1]["children"] = walk(it)
                continue
            try:
                page = reader.get_destination_page_number(it)
            except Exception:  # noqa: BLE001
                page = None
            out.append({"title": str(it.title), "page": page, "children": []})
        return out

    try:
        return walk(reader.outline)
    except Exception:  # noqa: BLE001
        return []


def set_outline(writer: Any, entries: list[dict[str, Any]], total: int) -> int:
    """Replace the bookmarks with ``[{title, page, children?}]`` (pages 0-based)."""
    from pypdf.generic import NameObject
    root = writer._root_object
    if "/Outlines" in root:
        del root["/Outlines"]
    count = 0

    def add(items: list[dict[str, Any]], parent: Any, depth: int) -> None:
        nonlocal count
        if depth > 8:
            raise ValidationFailed("Bookmarks may be nested at most 8 levels")
        for e in items:
            title = str(e.get("title", "")).strip()
            page = e.get("page")
            if not title or len(title) > 500:
                raise ValidationFailed("Each bookmark needs a title (up to 500 characters)")
            if not isinstance(page, int) or not 0 <= page < total:
                raise ValidationFailed(f"Bookmark '{title[:40]}' points at a page that does not exist")
            item = writer.add_outline_item(title, page, parent=parent)
            count += 1
            if e.get("children"):
                add(e["children"], item, depth + 1)

    add(entries, None, 1)
    return count


def sanitize(writer: Any) -> dict[str, int]:
    """Remove metadata, XMP, attachments, JavaScript, open actions and (optionally) nothing else; page content is untouched."""
    removed = {"metadata": 0, "attachments": 0, "javascript": 0, "actions": 0}
    writer.add_metadata({"/Producer": "SLAI Documaster"})
    root = writer._root_object
    from pypdf.generic import NameObject, ArrayObject
    for key in ("/Metadata", "/OpenAction", "/AA", "/URI"):
        if key in root:
            del root[key]
            removed["actions" if key != "/Metadata" else "metadata"] += 1
    names = root.get("/Names")
    if names is not None:
        names = names.get_object()
        if "/EmbeddedFiles" in names:
            del names["/EmbeddedFiles"]
            removed["attachments"] += 1
        if "/JavaScript" in names:
            del names["/JavaScript"]
            removed["javascript"] += 1
    for page in writer.pages:
        annots = page.get("/Annots")
        if annots:
            keep = []
            for ref in annots:
                a = ref.get_object()
                if a.get("/Subtype") == "/FileAttachment":
                    removed["attachments"] += 1
                    continue
                if "/A" in a and str(a["/A"].get_object().get("/S")) == "/JavaScript":
                    del a["/A"]
                    removed["javascript"] += 1
                keep.append(ref)
            page[NameObject("/Annots")] = ArrayObject(keep)
        if "/AA" in page:
            del page["/AA"]
            removed["actions"] += 1
    return removed
