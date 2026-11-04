"""Structure-aware DOCX -> SLDC import, including page geometry and typography.

Only representable, explicit formatting is imported. Unsupported Word constructs
are reported rather than silently flattened into misleading SLDC structures.
"""
from __future__ import annotations

import re
from typing import Any

from ..bridge import sldc
from ..errors import DependencyMissing, FileRejected
from .read_vector_shapes import shapes_in_paragraph, SHAPE_KEY

_NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
}
_W = "{%s}" % _NS["w"]
_HEADING = re.compile(r"^(?:heading|kop|überschrift|titre|encabezado|título)\s*(\d)$", re.I)
_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif"}


def _mm(length: Any) -> float | None:
    return round(float(length.mm), 3) if length is not None else None


def _run_properties(run: Any, paragraph: Any = None) -> dict[str, Any]:
    """Resolve direct, character-style and paragraph-style formatting, if known."""
    sources = [getattr(run, "font", None)]
    if getattr(run, "style", None) is not None:
        sources.append(run.style.font)
    if paragraph is not None and getattr(paragraph, "style", None) is not None:
        sources.append(paragraph.style.font)
    out: dict[str, Any] = {}
    for name, attr in (("font_family", "name"), ("font_size_pt", "size"),
                       ("bold", "bold"), ("italic", "italic"), ("underline", "underline"),
                       ("strike", "strike"), ("superscript", "superscript"),
                       ("subscript", "subscript")):
        for font in sources:
            value = getattr(font, attr, None) if font is not None else None
            if value is None:
                continue
            if name == "font_size_pt":
                value = round(float(value.pt), 2)
                if not 4 <= value <= 400:
                    continue
            elif name == "font_family":
                if not isinstance(value, str) or not value.strip() or len(value) > 100 or re.search(r'[\x00-\x1f\x7f{};<>\\"\x27`]', value):
                    continue
            elif name in ("bold", "italic", "underline", "strike", "superscript", "subscript"):
                value = bool(value)
            out[name] = value
            break
    for font in sources:
        color = getattr(getattr(font, "color", None), "rgb", None) if font is not None else None
        if color is not None:
            out["color"] = f"#{str(color)}".lower()
            break
    return out


def _paragraph_properties(paragraph: Any) -> dict[str, Any]:
    pf = paragraph.paragraph_format
    style_pf = paragraph.style.paragraph_format if paragraph.style is not None else None
    attrs: dict[str, Any] = {}
    align = paragraph.alignment
    if align is None and style_pf is not None:
        align = style_pf.alignment
    if align is not None:
        mapped = {0: "left", 1: "center", 2: "right", 3: "justify"}.get(int(align))
        if mapped:
            attrs["align"] = mapped
    for key, attr in (("space_before_pt", "space_before"), ("space_after_pt", "space_after")):
        value = getattr(pf, attr, None)
        if value is None and style_pf is not None:
            value = getattr(style_pf, attr, None)
        if value is not None and 0 <= value.pt <= 2000:
            attrs[key] = round(float(value.pt), 2)
    for key, attr in (("indent_mm", "left_indent"), ("first_line_indent_mm", "first_line_indent")):
        value = getattr(pf, attr, None)
        if value is None and style_pf is not None:
            value = getattr(style_pf, attr, None)
        minimum = -100 if key == "first_line_indent_mm" else 0
        if value is not None and minimum <= value.mm <= 500:
            attrs[key] = _mm(value)
    spacing = pf.line_spacing
    if spacing is None and style_pf is not None:
        spacing = style_pf.line_spacing
    if isinstance(spacing, (int, float)) and .5 <= spacing <= 5:
        attrs["line_height"] = round(float(spacing), 3)
    return attrs


def _header_field_text(paragraph: Any) -> str:
    """Keep dynamic PAGE/NUMPAGES/TITLE fields instead of their cached numbers."""
    out: list[str] = []
    in_field = False
    field_instr = ""
    for run in paragraph._p.iter("{%s}r" % _NS["w"]):
        for part in run:
            if part.tag == _W + "fldChar":
                kind = part.get(_W + "fldCharType")
                if kind == "begin":
                    in_field, field_instr = True, ""
                elif kind == "separate":
                    cmd = field_instr.strip().split(" ")[0].upper()
                    out.append({"PAGE": "{page}", "NUMPAGES": "{pages}", "TITLE": "{title}"}.get(cmd, ""))
                elif kind == "end":
                    in_field = False
            elif part.tag == _W + "instrText" and in_field:
                field_instr += part.text or ""
            elif part.tag == _W + "t" and not in_field:
                out.append(part.text or "")
            elif part.tag == _W + "tab" and not in_field:
                out.append("\t")
    return "".join(out)


def _header_template(part: Any) -> dict[str, Any]:
    if not part.paragraphs:
        return {}
    template: dict[str, Any] = {}
    formats: dict[str, Any] = {}
    rich: dict[str, list[dict[str, Any]]] = {}
    for paragraph in part.paragraphs:
        text = _header_field_text(paragraph)
        if not text.strip():
            continue
        segments = text.split("\t")
        if len(segments) >= 3:
            fields = dict(zip(("left", "center", "right"),
                              (segments[0], "\t".join(segments[1:-1]), segments[-1])))
        else:
            align = paragraph.alignment
            pos = {1: "center", 2: "right"}.get(int(align) if align is not None else 0, "left")
            fields = {pos: text}
        for pos, value in fields.items():
            if value.strip():
                template[pos] = (template.get(pos, "") + ("\n" if pos in template else "") + value).strip()[:500]
                for run in paragraph.runs:
                    if run.text.strip():
                        props = _run_properties(run, paragraph)
                        formats[pos] = {k: v for k, v in props.items()
                                        if k in {"font_family", "font_size_pt", "color", "bold", "italic", "underline"}}
                        break
        # For literal text we can preserve independently styled runs. Word
        # fields are processed separately because their visible cached result
        # must not be mistaken for persistent user-authored text.
        if "{" not in text and "}" not in text:
            field_index = 0
            for run in paragraph.runs:
                chunks = run.text.split("\t")
                for k, chunk in enumerate(chunks):
                    if k:
                        field_index += 1
                    if not chunk:
                        continue
                    if len(segments) >= 3:
                        pos = ("left", "center", "right")[min(field_index, 2)]
                    else:
                        pos = next(iter(fields))
                    props = _run_properties(run, paragraph)
                    style = {key: value for key, value in props.items()
                             if key in {"font_family", "font_size_pt", "color", "bold", "italic", "underline"}}
                    rich.setdefault(pos, []).append({"text": chunk, **({"format": style} if style else {})})
    if formats:
        template["formats"] = formats
    matching = {pos: runs for pos, runs in rich.items()
                if "".join(r["text"] for r in runs) == template.get(pos, "") and len(runs) <= 200}
    if matching:
        template["runs"] = matching
    return template


def _document_sections(document: Any) -> list[dict[str, Any]]:
    sections = []
    for index, section in enumerate(document.sections):
        page = {"width_mm": _mm(section.page_width), "height_mm": _mm(section.page_height),
                "margins_mm": {name: _mm(getattr(section, name + "_margin")) for name in ("left", "right", "top", "bottom")},
                "header_mm": _mm(section.header_distance), "footer_mm": _mm(section.footer_distance)}
        for key, value in list(page["margins_mm"].items()):
            if value is None:
                page["margins_mm"].pop(key)
        if page["width_mm"] is None or page["height_mm"] is None:
            page = {"size": "A4"}
        else:
            page = {key: value for key, value in page.items() if value is not None}
        model: dict[str, Any] = {"id": f"import_section_{index + 1}", "page": page}
        for edge in ("header", "footer"):
            template = _header_template(getattr(section, edge))
            if template:
                model[edge] = template
        sections.append(model)
    return sections


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
    state = {"break": False, "images": 0, "skipped_images": 0, "shapes": 0}
    sections = _document_sections(document)
    current_section = 0

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
                props = _run_properties(run, paragraph)
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
        setup = sections[min(current_section, len(sections)-1)].get("page", {})
        margins = setup.get("margins_mm", {})
        shape_values, shape_warnings = shapes_in_paragraph(
            paragraph, margins.get("left",25.0), margins.get("top",25.0))
        warnings.extend(shape_warnings)
        stripped = text.strip()
        if not stripped and not pictures and not shape_values:
            return
        lead = len(text) - len(text.lstrip())
        if stripped:
            spans = [{**s, "start": max(s["start"] - lead, 0), "end": min(s["end"] - lead, len(stripped))}
                     for s in spans if s["end"] - lead > 0 and s["start"] - lead < len(stripped)]
            spans = [s for s in spans if s["start"] < s["end"]]
            heading = _HEADING.match(style_name.strip())
            attrs: dict[str, Any] = _paragraph_properties(paragraph)
            kind = "paragraph"
            if heading:
                kind, attrs["level"] = "heading", max(1, min(6, int(heading.group(1))))
            elif style_name.lower() in ("title", "titel", "titre"):
                kind, attrs["level"] = "heading", 1
            elif style_name.lower() in ("subtitle", "ondertitel", "sous-titre"):
                kind, attrs["level"] = "heading", 2
            elif _is_list(paragraph, style_name):
                ordered = bool(re.search(r"number|numer|nummer|num", style_name, re.I)) or _numbering_is_ordered(document, paragraph)
                kind, attrs["ordered"] = "list_item", ordered
                depth = _list_depth(paragraph, style_name)
                if depth:
                    attrs["depth"] = depth
            elif re.search(r"quote|citaat|citation", style_name, re.I):
                kind = "quote"
            elif re.search(r"^(code|source code|preformatted|plain text)", style_name, re.I):
                kind = "code"
            if pf_break:
                state["break"] = True
            block = sldc().make_block(kind, stripped, attributes=attrs or None, spans=spans or None)
            block["section"] = sections[min(current_section, len(sections) - 1)]["id"]
            push(block)
        elif pf_break:
            state["break"] = True
        for figure_block in pictures:
            push(figure_block)
        for shape in shape_values:
            state["shapes"] += 1
            # Shape carriers are inert empty paragraphs, not untrusted XML/SVG.
            # Preserve edits through Writer's existing canonical SLDC extension.
            imported = sldc().make_block("paragraph", "", extensions={SHAPE_KEY:shape})
            imported["section"] = sections[min(current_section, len(sections)-1)]["id"]
            push(imported)

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
        block = sldc().make_table(rows, header_rows=header_rows, columns=columns)
        block["section"] = sections[min(current_section, len(sections) - 1)]["id"]
        push(block)

    body = document.element.body
    for child in body.iterchildren():
        tag = child.tag
        if tag == _W + "p":
            paragraph_block(Paragraph(child, document))
            ppr = child.find(_W + "pPr")
            if ppr is not None and ppr.find(_W + "sectPr") is not None:
                current_section = min(current_section + 1, len(sections) - 1)
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
        warnings.append("Rich text and page-variant header/footer formatting may need manual review")
    if state["skipped_images"]:
        dropped.append(f"{state['skipped_images']} image(s) in unsupported formats or over the size limit")
    core = document.core_properties
    title = (core.title or "").strip()
    if document.settings.odd_and_even_pages_header_footer or any(s.different_first_page_header_footer for s in document.sections):
        dropped.append("different first-page or odd/even headers and footers (not yet supported by SLDC 1.x)")
    return {"blocks": blocks, "assets": assets, "sections": sections,
            "title": title, "author": (core.author or "").strip(),
            "language": None, "dropped": dropped, "warnings": warnings, "images": state["images"],
            "shapes": state["shapes"]}


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
            pieces: list[str] = []
            cell_spans: list[dict[str, Any]] = []
            align = None
            for paragraph in _cell_paragraphs(tc):
                if pieces:
                    pieces.append("\n")
                start = sum(len(piece) for piece in pieces)
                para_text = paragraph.text.replace(" ", " ")
                pieces.append(para_text)
                cursor = 0
                for run in paragraph.runs:
                    segment = run.text.replace(" ", " ")
                    if not segment:
                        continue
                    local = para_text.find(segment, cursor)
                    if local < 0:
                        continue
                    props = _run_properties(run)
                    if props:
                        cell_spans.append({"type": "format", "start": start + local,
                                           "end": start + local + len(segment), **props})
                    cursor = local + len(segment)
                if align is None and paragraph.alignment is not None:
                    align = {0: "left", 1: "center", 2: "right", 3: "justify"}.get(int(paragraph.alignment))
            original = "".join(pieces)
            text = original.strip()
            lead = len(original) - len(original.lstrip())
            cell_spans = [{**s, "start": max(0, s["start"] - lead), "end": min(len(text), s["end"] - lead)}
                          for s in cell_spans if s["end"] > lead and s["start"] - lead < len(text)]
            cell_spans.sort(key=lambda s: (s["start"], s["end"], s["type"]))
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
            cells.append({"text": text, "span": span, "vmerge": vmerge,
                          **({"spans": cell_spans} if cell_spans else {}), **({"align": align} if align else {})})
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
            for key in ("spans", "align"):
                if key in cell:
                    item[key] = cell[key]
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
