"""Best-effort structural PDF reader built on PyMuPDF's positioned text/tables.

PDFs are paint instructions, not semantic word-processing files. We only promote
clear geometry to editable structure and keep provenance for uncertain content.
"""
from __future__ import annotations

import re
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

from ..bridge import sldc

PT_TO_MM = 25.4 / 72.0
_PAGE_NUMBER = re.compile(r"^\s*(?:page\s+)?\d{1,5}\s*$", re.I)


def _style(span: dict[str, Any]) -> dict[str, Any]:
    props: dict[str, Any] = {}
    font = str(span.get("font", "")).split("+")[-1]
    if font and len(font) <= 100 and not re.search(r'[\x00-\x1f\x7f{};<>\\"\x27`]', font):
        props["font_family"] = font
    size = round(float(span.get("size") or 0), 2)
    if 4 <= size <= 400:
        props["font_size_pt"] = size
    flags = int(span.get("flags", 0))
    if flags & 16 or "bold" in font.lower():
        props["bold"] = True
    if flags & 2 or "italic" in font.lower() or "oblique" in font.lower():
        props["italic"] = True
    color = int(span.get("color", 0))
    if color != 0:
        props["color"] = f"#{color & 0xffffff:06x}"
    return props


def _lines(page: Any) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for block in page.get_text("dict", sort=True).get("blocks", []):
        if "lines" not in block:
            continue
        for line in block["lines"]:
            spans = [s for s in line.get("spans", []) if s.get("text", "").strip()]
            if not spans:
                continue
            lines.append({"text": "".join(str(s["text"]) for s in spans).strip(),
                          "bbox": tuple(line["bbox"]), "spans": spans,
                          "size": max(float(s.get("size", 0)) for s in spans)})
    return sorted(lines, key=lambda x: (round(x["bbox"][1], 1), x["bbox"][0]))


def _overlaps(rect: Any, boxes: list[tuple[float, ...]]) -> bool:
    x0, y0, x1, y1 = rect
    for a, b, c, d in boxes:
        overlap_x = max(0, min(x1, c) - max(x0, a))
        overlap_y = max(0, min(y1, d) - max(y0, b))
        if overlap_x * overlap_y >= .5 * max(1, (x1 - x0) * (y1 - y0)):
            return True
    return False


def _page_tables(page: Any) -> tuple[list[dict[str, Any]], list[tuple[float, ...]]]:
    result: list[dict[str, Any]] = []
    boxes: list[tuple[float, ...]] = []
    try:
        found = page.find_tables()
    except (AttributeError, ValueError, RuntimeError):
        return result, boxes
    for table in found.tables:
        try:
            rows = table.extract()
        except (ValueError, RuntimeError):
            continue
        rows = [[(cell or "").strip() for cell in row] for row in rows]
        if len(rows) < 2 or not rows[0] or len(rows[0]) > 256 or len(rows) > 10_000:
            continue
        width = len(rows[0])
        if any(len(row) != width for row in rows):
            continue
        header_rows = 1 if getattr(table, "header", None) and table.header.external is False else 0
        block = sldc().make_table(rows, header_rows=header_rows, columns=width)
        result.append({"bbox": tuple(table.bbox), "block": block})
        boxes.append(tuple(table.bbox))
    return result, boxes


def _line_key(line: dict[str, Any]) -> str:
    # Keep a running page number as the same repeated footer on every page.
    text = re.sub(r"\s+", " ", line["text"]).casefold().strip()
    return "{page}" if _PAGE_NUMBER.fullmatch(text) else text


def _parse_header_footer(pages: list[list[dict[str, Any]]], dimensions: list[tuple[float, float]]) -> tuple[list[set[int]], dict[str, Any], dict[str, Any]]:
    zones = [set() for _ in pages]
    tops: list[tuple[int, int, str]] = []
    bottoms: list[tuple[int, int, str]] = []
    for i, lines in enumerate(pages):
        height = dimensions[i][1]
        for j, line in enumerate(lines):
            y = (line["bbox"][1] + line["bbox"][3]) / 2
            if y < height * .105:
                tops.append((i, j, _line_key(line)))
            elif y > height * .89:
                bottoms.append((i, j, _line_key(line)))
    def collect(candidates: list[tuple[int, int, str]]) -> dict[str, Any]:
        template: dict[str, Any] = {}
        formats: dict[str, Any] = {}
        counts = Counter(key for _, _, key in candidates)
        for i, j, key in candidates:
            # Repeated content on multiple pages is the strongest indicator
            # of page furniture. Never strip a single-page title by position.
            if key and counts[key] >= 2 and len(pages) >= 2:
                zones[i].add(j)
                line = pages[i][j]
                x0, _, x1, _ = line["bbox"]
                width = dimensions[i][0]
                midpoint = (x0 + x1) / 2
                pos = "left" if midpoint < width * .40 else "right" if midpoint > width * .60 else "center"
                value = "{page}" if key == "{page}" else line["text"]
                if pos not in template:
                    template[pos] = value[:500]
                    props = _style(line["spans"][0])
                    formats[pos] = {k: v for k, v in props.items()
                                    if k in {"font_family", "font_size_pt", "color", "bold", "italic"}}
        if formats:
            template["formats"] = formats
        return template
    return zones, collect(tops), collect(bottoms)


def read_pdf_layout(path: Path, name: str, password: str | None = None) -> dict[str, Any] | None:
    """Return editable SLDC blocks/section when PyMuPDF can recover positioned text.

    Return None if the optional library is absent or the document is image-only.
    Password validation and OCR are controlled by the caller.
    """
    try:
        import fitz
    except ImportError:
        return None
    with fitz.open(str(path)) as pdf:
        if pdf.needs_pass and not pdf.authenticate(password or ""):
            return None
        if not pdf.page_count:
            return None
        page_lines = [_lines(page) for page in pdf]
        if not any(page_lines):
            return None
        dims = [(float(page.rect.width), float(page.rect.height)) for page in pdf]
        excluded, header, footer = _parse_header_footer(page_lines, dims)
        text_sizes = [line["size"] for i, lines in enumerate(page_lines)
                      for j, line in enumerate(lines) if j not in excluded[i]]
        typical = statistics.median(text_sizes) if text_sizes else 11.0
        body_lines = [line for i, lines in enumerate(page_lines) for j, line in enumerate(lines)
                      if j not in excluded[i]]
        first_w, first_h = dims[0]
        # Margins are derived from the visible body geometry. This is a layout
        # estimate, not an invented exact value from a non-semantic PDF.
        xs = [line["bbox"][0] for line in body_lines]
        ys = [line["bbox"][1] for line in body_lines]
        margin = lambda p: round(max(0, p * PT_TO_MM), 2)
        # The last printed line is NOT the bottom page margin. A short chapter
        # can intentionally leave most of a page blank; inferring a 200 mm
        # margin there would destroy pagination on import.
        left = min(40.0, margin(min(xs)))
        top = min(45.0, margin(min(ys)))
        page = {"width_mm": margin(first_w), "height_mm": margin(first_h),
                "margins_mm": {"left": left, "right": left,
                               "top": top, "bottom": min(top, 30.0)}}
        if page["width_mm"] - page["margins_mm"]["left"] - page["margins_mm"]["right"] < 20:
            page["margins_mm"]["left"] = page["margins_mm"]["right"] = 10
        section: dict[str, Any] = {"id": "pdf_import_body", "page": page}
        if header:
            section["header"] = header
        if footer:
            section["footer"] = footer
        blocks: list[dict[str, Any]] = []
        tables_found = 0
        for i, pdf_page in enumerate(pdf):
            tables, table_boxes = _page_tables(pdf_page)
            tables_found += len(tables)
            items: list[tuple[float, float, dict[str, Any]]] = []
            for tab in tables:
                items.append((tab["bbox"][1], tab["bbox"][0], tab["block"]))
            lines = [line for j, line in enumerate(page_lines[i])
                     if j not in excluded[i] and not _overlaps(line["bbox"], table_boxes)]
            current: list[dict[str, Any]] = []
            def flush() -> None:
                if not current:
                    return
                text_parts: list[str] = []
                spans: list[dict[str, Any]] = []
                offset = 0
                for k, line in enumerate(current):
                    if k:
                        text_parts.append(" ")
                        offset += 1
                    for source in line["spans"]:
                        piece = str(source.get("text", ""))
                        if not piece:
                            continue
                        text_parts.append(piece)
                        props = _style(source)
                        if props:
                            spans.append({"type": "format", "start": offset, "end": offset + len(piece), **props})
                        offset += len(piece)
                content = "".join(text_parts).strip()
                if content:
                    lead = len("".join(text_parts)) - len("".join(text_parts).lstrip())
                    spans = [{**s, "start": max(0, s["start"] - lead),
                              "end": min(len(content), s["end"] - lead)} for s in spans
                             if s["end"] > lead and s["start"] - lead < len(content)]
                    spans.sort(key=lambda s: (s["start"], s["end"], s["type"]))
                    x0 = min(line["bbox"][0] for line in current)
                    x1 = max(line["bbox"][2] for line in current)
                    size = max(line["size"] for line in current)
                    attrs: dict[str, Any] = {}
                    if abs((x0 + x1) / 2 - first_w / 2) < first_w * .06 and x0 > first_w * .15:
                        attrs["align"] = "center"
                    else:
                        attrs["align"] = "left"
                    heading = size >= typical * 1.24 and len(content) < 180
                    kind = "heading" if heading else "paragraph"
                    if heading:
                        attrs["level"] = 1 if size >= typical * 1.65 else 2
                    block = sldc().make_block(kind, content, spans=spans or None,
                                              attributes=attrs, source_ref=f"{name}#p{i + 1}")
                    items.append((current[0]["bbox"][1], x0, block))
                current.clear()
            for line in lines:
                if current:
                    prev = current[-1]
                    vertical_gap = line["bbox"][1] - prev["bbox"][3]
                    same_size = abs(line["size"] - prev["size"]) <= 1.0
                    similar_start = abs(line["bbox"][0] - current[0]["bbox"][0]) < 20
                    if vertical_gap > max(6, prev["size"] * .75) or not same_size or not similar_start:
                        flush()
                current.append(line)
            flush()
            items.sort(key=lambda t: (t[0], t[1]))
            if not items:
                anchor = sldc().make_block("paragraph", "", source_ref=f"{name}#p{i + 1}")
                items = [(0, 0, anchor)]
            for k, (_, _, block) in enumerate(items):
                block["section"] = section["id"]
                if i > 0 and k == 0:
                    block.setdefault("attributes", {})["page_break_before"] = True
                blocks.append(block)
        return {"blocks": blocks, "sections": [section], "tables": tables_found,
                "features": ["positioned text", "font families, sizes, weight and colour", "page geometry",
                             "paragraphs and headings", "page boundaries"]
                            + (["tables"] if tables_found else [])
                            + (["repeated headers/footers"] if header or footer else []),
                "warnings": ["PDF import reconstructs editable layout heuristically; please review paragraphs, "
                             "table boundaries, page margins and font substitutions"],
                "dropped": ["images and vector artwork", "character underlining when not encoded as font style",
                            "complex columns and floating objects"]}
