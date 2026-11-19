"""Shapes in headers and footers: reading them from a Writer document and saying where a page draws them (PDF and Word).

Writer stores them in its own document extension ``slai.writer.band_shapes`` (see Writer's ``bands.py``, which this file's arithmetic is held to by a test over
the same cases). Nothing here edits a document. A document without the extension, or an item this file cannot draw, leaves the page as it was.
"""
from __future__ import annotations

from typing import Any

WRITER_EXT = "slai.writer"
KEY = "band_shapes"
EDGES = ("header", "footer")
_KINDS = frozenset({"rectangle", "ellipse", "line", "triangle", "diamond", "pentagon", "hexagon", "star", "arrow", "plus"})


# --------------------------------------------------------------------------------------------------------------------------- reading
def store_of(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return ((doc.get("extensions") or {}).get(WRITER_EXT) or {}).get(KEY) or {}


def owner(doc: dict[str, Any], section_id: str, edge: str) -> str:
    """The section whose definition of ``edge`` this section shows: itself, or, while it is linked to the previous one, whoever that one shows."""
    sections = [s["id"] for s in doc.get("sections") or []]
    store = store_of(doc)
    at = sections.index(section_id) if section_id in sections else 0
    while at > 0 and (store.get(sections[at]) or {}).get("link", {}).get(edge):
        at -= 1
    return sections[at] if sections else section_id


def items_of(doc: dict[str, Any], section_id: str, edge: str, variant: str) -> list[dict[str, Any]]:
    return list((((store_of(doc).get(owner(doc, section_id, edge)) or {}).get(edge) or {}).get(variant)) or [])


def variant_for(doc: dict[str, Any], section_id: str, physical: int, first_of_section: bool) -> str:
    """Which list a page shows: the section's first page its own (when asked for), a left-hand page its own when the section has odd and even pages and gave it one."""
    entry = store_of(doc).get(section_id) or {}
    if entry.get("first_page") and first_of_section:
        return "first"
    book = ((((doc.get("extensions") or {}).get(WRITER_EXT) or {}).get("book_layouts")) or {}).get(section_id) or {}
    if physical % 2 == 0 and book.get("odd_even") is True:
        return "even"
    return "default"


def shapes_for_page(doc: dict[str, Any], section_id: str, edge: str, physical: int, first_of_section: bool) -> tuple[list[dict[str, Any]], str, bool]:
    """The shapes a page shows in its header or footer, the variant they come from, and whether the page mirrors them (a left-hand page of a book whose
    edges are swapped: it has no list of its own, so it shows the ordinary one, from the other side)."""
    variant = variant_for(doc, section_id, physical, first_of_section)
    items = items_of(doc, section_id, edge, variant)
    book = ((((doc.get("extensions") or {}).get(WRITER_EXT) or {}).get("book_layouts")) or {}).get(section_id) or {}
    mirrored = False
    if variant == "even" and not items:
        variant, items = "default", items_of(doc, section_id, edge, "default")
    if physical % 2 == 0 and variant == "default" and book.get("mirror") is True and book.get("mirror_edges") is not False:
        mirrored = True
    return items, variant, mirrored


def place(item: dict[str, Any], edge: str, page_w: float, page_h: float, left: float, right: float, mirrored: bool = False) -> dict[str, float]:
    """Where a shape's box is on its page, in millimetres from the top-left corner of the page: ``{"x", "y", "w", "h"}`` (the unrotated box; the shape turns about its centre)."""
    shape = item["shape"]
    frame = page_w - left - right
    h = float(shape["height_mm"])
    if item.get("span"):
        w, x = frame, left
    else:
        w = float(shape["width_mm"])
        x = left + frame - float(item["x_mm"]) - w if mirrored else left + float(item["x_mm"])
    y = float(item["y_mm"]) if edge == "header" else page_h - float(item["y_mm"]) - h
    return {"x": x, "y": y, "w": w, "h": h}




def drawable(item: Any) -> bool:
    """An item this writer can draw: a body shape of a kind it knows, of a size the writers take."""
    shape = item.get("shape") if isinstance(item, dict) else None
    return (isinstance(shape, dict) and shape.get("version") == 1 and shape.get("kind") in _KINDS and isinstance(shape.get("width_mm"), (int, float))
            and isinstance(shape.get("height_mm"), (int, float)) and 2 <= shape["width_mm"] <= 1000 and 2 <= shape["height_mm"] <= 1000
            and isinstance(item.get("x_mm", 0), (int, float)) and isinstance(item.get("y_mm", 0), (int, float)))


def page_items(doc: dict[str, Any], section_id: str | None, edge: str, physical: int, first_of_section: bool) -> tuple[list[dict[str, Any]], bool]:
    """What a page draws in its header or footer (the items that can be drawn) and whether it draws them from the other side."""
    if not section_id or not store_of(doc):
        return [], False
    items, _variant, mirrored = shapes_for_page(doc, section_id, edge, physical, first_of_section)
    return [i for i in items if drawable(i)], mirrored
