"""DOCX -> document model with python-docx: headings, paragraphs, lists, tables (merged cells), inline formatting,
hyperlinks, page breaks, inline PNG/JPEG/GIF images, core properties.

Not imported (reported in ``dropped``): headers/footers, footnotes/endnotes, comments, tracked-change authorship,
text boxes, fields' cached results beyond their visible text, fonts/sizes/colours, page setup, drawing shapes.
"""
from __future__ import annotations

import re
from typing import Any

from ..bridge import sldc
from ..errors import DependencyMissing, FileRejected

_NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
}
_W = "{%s}" % _NS["w"]
_HEADING = re.compile(r"^(?:heading|kop|überschrift|titre|encabezado|título)\s*(\d)$", re.I)
_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif"}


def read_docx(path: str) -> dict[str, Any]:
    try:
        import docx  # type: ignore
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError as exc:
        raise DependencyMissing("python-docx is not installed", details={"package": "python-docx"}) from exc
    try:
        document = docx.Document(path)
    except Exception as exc:  # noqa: BLE001 - python-docx raises several types for damaged files
        raise FileRejected(f"This DOCX could not be opened: {exc}") from exc

    blocks: list[dict[str, Any]] = []
    assets: list[Any] = []
    warnings: list[str] = []
    seen_assets: dict[str, str] = {}
    state = {"break": False, "images": 0, "skipped_images": 0}

    def add_asset(blob: bytes) -> str | None:
        if len(blob) > 8 * 1024 * 1024 or len(assets) >= 200:
            return None
        asset = sldc().make_asset(blob)
        if asset.id not in seen_assets:
            seen_assets[asset.id] = asset.id
            assets.append(asset)
        return asset.id

    def push(block: dict[str, Any]) -> None:
        if state["break"]:
            block.setdefault("attributes", {})["page_break_before"] = True
            state["break"] = False
        blocks.append(block)

    def runs_to_text(paragraph: Paragraph) -> tuple[str, list[dict[str, Any]]]:
        text_parts: list[str] = []
        spans: list[dict[str, Any]] = []
        length = 0

        def add(run_text: str, props: dict[str, Any], href: str | None = None) -> None:
            nonlocal length
            if not run_text:
                return
            text_parts.append(run_text)
            end = length + len(run_text)
            if props:
                spans.append({"type": "format", "start": length, "end": end, **props})
            if href:
                spans.append({"type": "link", "start": length, "end": end, "href": href})
            length = end

        try:
            items = list(paragraph.iter_inner_content())
        except AttributeError:  # python-docx < 1.0
            items = list(paragraph.runs)
        for item in items:
            href = None
            runs = [item]
            if item.__class__.__name__ == "Hyperlink":
                url = getattr(item, "url", "") or ""
                href = url if re.match(r"^(https?://|mailto:)", url, re.I) and len(url) < 2000 else None
                runs = list(item.runs)
            for run in runs:
                props: dict[str, Any] = {}
                if run.bold:
                    props["bold"] = True
                if run.italic:
                    props["italic"] = True
                if run.underline:
                    props["underline"] = True
                if run.font.strike:
                    props["strike"] = True
                if run.font.superscript:
                    props["superscript"] = True
                elif run.font.subscript:
                    props["subscript"] = True
                if _has_page_break(run):
                    state["break"] = True
                pieces = _run_pieces(run)
                for piece in pieces:
                    add(piece, props, href)
        return "".join(text_parts), spans

    def images_of(paragraph: Paragraph) -> list[dict[str, Any]]:
        found = []
        for blip in paragraph._p.iter("{%s}blip" % _NS["a"]):
            rid = blip.get("{%s}embed" % _NS["r"])
            if not rid:
                continue
            try:
                part = paragraph.part.related_parts[rid]
            except KeyError:
                continue
            if part.content_type not in _IMAGE_TYPES:
                state["skipped_images"] += 1
                continue
            asset_id = add_asset(part.blob)
            if asset_id is None:
                state["skipped_images"] += 1
                continue
            alt = ""
            node = blip
            for _ in range(8):
                node = node.getparent()
                if node is None:
                    break
                if node.tag.endswith("}inline") or node.tag.endswith("}anchor"):
                    doc_pr = node.find("{%s}docPr" % _NS["wp"])
                    if doc_pr is not None:
                        alt = (doc_pr.get("descr") or "").strip()
                    break
            figure = {"asset": asset_id, "alt": alt} if alt else {"asset": asset_id, "alt": "", "decorative": True}
            found.append(sldc().make_block("figure", "", figure=figure))
            state["images"] += 1
        return found

    def paragraph_block(paragraph: Paragraph) -> None:
        style_name = (paragraph.style.name if paragraph.style is not None else "") or ""
        text, spans = runs_to_text(paragraph)
        pf_break = bool(paragraph.paragraph_format.page_break_before)
        pictures = images_of(paragraph)
        stripped = text.strip()
        if not stripped and not pictures:
            return
        lead = len(text) - len(text.lstrip())
        if stripped:
            spans = [{**s, "start": max(s["start"] - lead, 0), "end": min(s["end"] - lead, len(stripped))}
                     for s in spans if s["end"] - lead > 0 and s["start"] - lead < len(stripped)]
            spans = [s for s in spans if s["start"] < s["end"]]
            heading = _HEADING.match(style_name.strip())
            attrs: dict[str, Any] = {}
            kind = "paragraph"
            if heading:
                kind, attrs = "heading", {"level": max(1, min(6, int(heading.group(1))))}
            elif style_name.lower() in ("title", "titel", "titre"):
                kind, attrs = "heading", {"level": 1}
            elif style_name.lower() in ("subtitle", "ondertitel", "sous-titre"):
                kind, attrs = "heading", {"level": 2}
            elif _is_list(paragraph, style_name):
                ordered = bool(re.search(r"number|numer|nummer|num", style_name, re.I)) or _numbering_is_ordered(document, paragraph)
                kind, attrs = "list_item", {"ordered": ordered}
                depth = _list_depth(paragraph, style_name)
                if depth:
                    attrs["depth"] = depth
            elif re.search(r"quote|citaat|citation", style_name, re.I):
                kind = "quote"
            elif re.search(r"^(code|source code|preformatted|plain text)", style_name, re.I):
                kind = "code"
            alignment = paragraph.alignment
            if alignment is not None and kind == "paragraph":
                name = {0: "left", 1: "center", 2: "right", 3: "justify"}.get(int(alignment))
                if name and name != "left":
                    attrs["align"] = name
            if pf_break:
                state["break"] = True
            block = sldc().make_block(kind, stripped, attributes=attrs or None, spans=spans or None)
            push(block)
        elif pf_break:
            state["break"] = True
        for figure_block in pictures:
            push(figure_block)

    def table_block(table: Table) -> None:
        rows = _table_rows(table)
        if not rows:
            return
        columns = max(sum(c.get("colspan", 1) for c in row) for row in rows)
        if columns > 256 or len(rows) > 10_000:
            warnings.append("A table larger than SLDC allows (256 columns / 10,000 rows) was skipped")
            return
        header_rows = 0
        for tr in table._tbl.tr_lst[:1]:
            if tr.trPr is not None and tr.trPr.find("{%s}tblHeader" % _NS["w"]) is not None:
                header_rows = 1
        push(sldc().make_table(rows, header_rows=header_rows, columns=columns))

    body = document.element.body
    for child in body.iterchildren():
        tag = child.tag
        if tag == _W + "p":
            paragraph_block(Paragraph(child, document))
        elif tag == _W + "tbl":
            table_block(Table(child, document))

    package_parts = {p.partname for p in document.part.package.iter_parts()}
    dropped: list[str] = []
    names = {str(n) for n in package_parts}
    if any("footnotes" in n for n in names):
        dropped.append("footnotes/endnotes")
    if any("comments" in n for n in names):
        dropped.append("review comments")
    if any(re.search(r"/(header|footer)\d*\.xml$", n) for n in names):
        dropped.append("headers and footers")
    if state["skipped_images"]:
        dropped.append(f"{state['skipped_images']} image(s) in unsupported formats or over the size limit")
    core = document.core_properties
    title = (core.title or "").strip()
    return {"blocks": blocks, "assets": assets, "title": title, "author": (core.author or "").strip(),
            "language": None, "dropped": dropped, "warnings": warnings, "images": state["images"]}


def _run_pieces(run: Any) -> list[str]:
    """Text of a run including tabs and line breaks (python-docx's ``run.text`` already maps both)."""
    text = run.text or ""
    return [text.replace(" ", " ")] if text else []


def _has_page_break(run: Any) -> bool:
    for br in run._r.findall("{%s}br" % _NS["w"]):
        if br.get("{%s}type" % _NS["w"]) == "page":
            return True
    return False


def _is_list(paragraph: Paragraph, style_name: str) -> bool:
    if re.match(r"^(list|lijst|liste|lista|elenco)", style_name, re.I):
        return True
    ppr = paragraph._p.pPr
    return bool(ppr is not None and ppr.numPr is not None and ppr.numPr.numId is not None
                and ppr.numPr.numId.val not in (None, 0))


def _list_depth(paragraph: Paragraph, style_name: str) -> int:
    ppr = paragraph._p.pPr
    if ppr is not None and ppr.numPr is not None and ppr.numPr.ilvl is not None and ppr.numPr.ilvl.val:
        return min(int(ppr.numPr.ilvl.val), 32)
    match = re.search(r"(\d)\s*$", style_name)
    return min(int(match.group(1)) - 1, 32) if match and int(match.group(1)) > 1 else 0


def _numbering_is_ordered(document: Any, paragraph: Paragraph) -> bool:
    ppr = paragraph._p.pPr
    if ppr is None or ppr.numPr is None or ppr.numPr.numId is None:
        return False
    try:
        numbering = document.part.numbering_part.element
    except Exception:  # noqa: BLE001
        return False
    num_id = ppr.numPr.numId.val
    ilvl = ppr.numPr.ilvl.val if ppr.numPr.ilvl is not None and ppr.numPr.ilvl.val else 0
    for num in numbering.findall("{%s}num" % _NS["w"]):
        if num.get("{%s}numId" % _NS["w"]) == str(num_id):
            abstract = num.find("{%s}abstractNumId" % _NS["w"])
            if abstract is None:
                return False
            for an in numbering.findall("{%s}abstractNum" % _NS["w"]):
                if an.get("{%s}abstractNumId" % _NS["w"]) == abstract.get("{%s}val" % _NS["w"]):
                    for lvl in an.findall("{%s}lvl" % _NS["w"]):
                        if lvl.get("{%s}ilvl" % _NS["w"]) == str(ilvl):
                            fmt = lvl.find("{%s}numFmt" % _NS["w"])
                            value = fmt.get("{%s}val" % _NS["w"]) if fmt is not None else ""
                            return value not in ("bullet", "none", "")
    return False


def _table_rows(table: Table) -> list[list[dict[str, Any]]]:
    """Rows of cells with column/row spans from the underlying ``w:tc`` elements (merged cells handled)."""
    raw: list[list[dict[str, Any]]] = []
    for tr in table._tbl.tr_lst:
        cells = []
        for tc in tr.tc_lst:
            text = "\n".join(p.text for p in _cell_paragraphs(tc)).strip().replace(" ", " ")
            tc_pr = tc.tcPr
            span = 1
            vmerge = None
            if tc_pr is not None:
                grid = tc_pr.find("{%s}gridSpan" % _NS["w"])
                if grid is not None and (grid.get("{%s}val" % _NS["w"]) or "").isdigit():
                    span = max(1, min(256, int(grid.get("{%s}val" % _NS["w"]))))
                vm = tc_pr.find("{%s}vMerge" % _NS["w"])
                if vm is not None:
                    vmerge = vm.get("{%s}val" % _NS["w"]) or "continue"
            cells.append({"text": text, "span": span, "vmerge": vmerge})
        raw.append(cells)
    # column index of every cell, then rowspans from vMerge chains
    grid_rows: list[list[tuple[int, dict[str, Any]]]] = []
    for cells in raw:
        col = 0
        placed = []
        for cell in cells:
            placed.append((col, cell))
            col += cell["span"]
        grid_rows.append(placed)
    out: list[list[dict[str, Any]]] = []
    for r, placed in enumerate(grid_rows):
        row: list[dict[str, Any]] = []
        for col, cell in placed:
            if cell["vmerge"] == "continue":
                continue
            item: dict[str, Any] = {"text": cell["text"]}
            if cell["span"] > 1:
                item["colspan"] = cell["span"]
            if cell["vmerge"] == "restart":
                extent = 1
                for below in grid_rows[r + 1:]:
                    match = next((c for start, c in below if start == col), None)
                    if match is not None and match["vmerge"] == "continue":
                        extent += 1
                    else:
                        break
                if extent > 1:
                    item["rowspan"] = extent
            row.append(item)
        out.append(row)
    return _rectangular(out)


def _rectangular(rows: list[list[dict[str, Any]]]) -> list[list[dict[str, Any]]]:
    from .read_html import _normalise_rows
    rows = [r for r in rows if r]
    return _normalise_rows(rows) if rows else []


def _cell_paragraphs(tc: Any):
    from docx.text.paragraph import Paragraph
    for p in tc.findall("{%s}p" % _NS["w"]):
        yield Paragraph(p, None)
