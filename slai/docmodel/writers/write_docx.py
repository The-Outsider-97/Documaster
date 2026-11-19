"""DOCX writer (python-docx). Real Word structure: heading/list/quote styles, numbered lists that restart, tables with
merged cells and repeating header rows, hyperlinks, inline images with alt text, page setup, per-section headers and footers
with PAGE/NUMPAGES fields, page breaks, document properties.

Footnotes are written as a numbered "Notes" list at the end (python-docx has no footnote API); the result says so.
"""
from __future__ import annotations

import copy
import io
import re
from pathlib import Path
from typing import Any

from ...errors import DependencyMissing
from ..ir import has_page_break, heading_level, span_segments
from . import WriteOptions, WriteResult
from .band_shapes import page_items, place as place_band, store_of as band_store
from .toc_layout import MM_PT, resolve_look, tab_plan, word_leader
from .common import (Notes, PageSetup, book_of, even_page_template, uses_facing_pages, asset_bytes, char_style_table, clean, column_widths, group_blocks, page_setup,
                     style_table, table_grid, table_look, hex_rgb, rule_look)

MONO = "Consolas"


SLDC_WRITER_SHAPES = True  # WordprocessingShape DrawingML output.


def write_docx(package: Any, dest: Path, opts: WriteOptions) -> WriteResult:
    try:
        import docx
        from docx.enum.section import WD_ORIENT, WD_SECTION
        from docx.enum.table import WD_TABLE_ALIGNMENT
        from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
        from docx.opc.constants import RELATIONSHIP_TYPE as RT
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
        from docx.shared import Mm, Pt, RGBColor
    except ImportError as exc:
        raise DependencyMissing("Writing DOCX needs the python-docx package", details={"package": "python-docx"}) from exc

    doc_model = package.document
    notes = Notes.of(doc_model)
    styles_para = style_table(doc_model)
    styles_char = char_style_table(doc_model)
    sections = {s["id"]: s for s in doc_model.get("sections", [])}
    # Writer's column layouts (separator, unequal widths, flow, continuous). Optional: without them columns are equal and every section starts a page.
    column_layouts = (((doc_model.get("extensions") or {}).get("slai.writer") or {}).get("column_layouts") or {})
    section_order = [s["id"] for s in doc_model.get("sections", [])]
    document = docx.Document()
    warnings: list[str] = []

    base_font = opts.font_family or "Calibri"
    normal = document.styles["Normal"]
    normal.font.name = base_font
    normal.font.size = Pt(opts.font_size_pt)
    rpr = normal.element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        rfonts.set(qn(attr), base_font)
    lang = OxmlElement("w:lang")
    lang.set(qn("w:val"), doc_model.get("language", "en") if doc_model.get("language") != "und" else "en-US")
    rpr.append(lang)

    core = document.core_properties
    core.title = clean(opts.title or doc_model["title"])[:250]
    authors = (doc_model.get("publication") or {}).get("authors") or ([opts.author] if opts.author else [])
    if authors:
        core.author = clean("; ".join(authors))[:250]
    core.language = doc_model.get("language", "") if doc_model.get("language") != "und" else ""
    core.comments = "Created with SLAI Documaster"

    gutter_at_top = [False]
    mirrored_document = any((book_of(doc_model, sid) or {}).get("mirror") is True for sid in section_order)
    paired_document = uses_facing_pages(doc_model, section_order)                 # mirrored margins or odd and even running heads: Word keeps the two apart document-wide

    # ---- page setup helpers
    def apply_page(section: Any, setup: PageSetup, layout: dict[str, Any] | None = None) -> None:
        w, h = setup.width_mm, setup.height_mm
        section.orientation = WD_ORIENT.LANDSCAPE if w > h else WD_ORIENT.PORTRAIT
        section.page_width, section.page_height = Mm(w), Mm(h)
        # Book layout. Word's own fields carry it: the left margin is the inside one of a right-hand page when the document mirrors its margins, the
        # gutter is its own field and is added by Word on the inside (or at the top). A binding on the right has no field outside right-to-left
        # documents, so that one is part of the right margin (the page looks the same).
        bound = setup.gutter if setup.gutter and (setup.mirror or setup.gutter_position in ("left", "top")) else 0.0
        top = setup.top - (bound if setup.gutter_position == "top" else 0.0)
        left = setup.left - (bound if setup.gutter_position != "top" else 0.0)
        section.top_margin, section.bottom_margin = Mm(top), Mm(setup.bottom)
        section.left_margin, section.right_margin = Mm(left), Mm(setup.right)
        section.gutter = Mm(bound)
        if bound and setup.gutter_position == "top":
            gutter_at_top[0] = True
        layout = layout or {}
        sect = section._sectPr
        cols = sect.find(qn("w:cols"))
        if cols is None:
            cols = OxmlElement("w:cols")
            sect.append(cols)
        for stale in list(cols):                                   # a new section starts as a copy of the one before it
            cols.remove(stale)
        for attr in ("w:equalWidth", "w:sep"):
            if cols.get(qn(attr)) is not None:
                del cols.attrib[qn(attr)]
        count = max(1, setup.columns)
        cols.set(qn("w:num"), str(count))
        cols.set(qn("w:space"), str(int(setup.column_gap_mm * 56.7)))
        widths = column_widths(layout.get("widths_mm"), setup)
        if widths:
            cols.set(qn("w:equalWidth"), "0")
            for index, width in enumerate(widths):
                col = OxmlElement("w:col")
                col.set(qn("w:w"), str(int(float(width) * 56.7)))
                if index < count - 1:
                    col.set(qn("w:space"), str(int(setup.column_gap_mm * 56.7)))
                cols.append(col)
        if count > 1 and layout.get("rule"):
            cols.set(qn("w:sep"), "1")                              # Word draws a thin line; its colour and thickness are not part of the file
        for stale in sect.findall(qn("w:bidi")):
            sect.remove(stale)
        if count > 1 and layout.get("flow") == "rtl":
            sect.append(OxmlElement("w:bidi"))

    def text_width_mm(setup: PageSetup) -> float:
        return max(20.0, setup.width_mm - setup.left - setup.right)

    def effective_setup(section: dict[str, Any] | None) -> PageSetup:
        setup = page_setup(section, None, book_of(doc_model, (section or {}).get("id")))
        if opts.page_size and not (section and "page" in section):
            from .common import PAGE_SIZES_MM
            if opts.page_size in PAGE_SIZES_MM:
                setup.width_mm, setup.height_mm = PAGE_SIZES_MM[opts.page_size]
        if opts.margins_mm is not None and not (section and "page" in section):
            setup.top = setup.right = setup.bottom = setup.left = opts.margins_mm
        return setup

    # ---- fields and header/footer
    def add_field(paragraph: Any, instr: str, cached: str = "1") -> None:
        for kind, text in (("begin", None), (None, instr), ("separate", None), (None, cached), ("end", None)):
            run = paragraph.add_run()
            if kind:
                fc = OxmlElement("w:fldChar")
                fc.set(qn("w:fldCharType"), kind)
                run._r.append(fc)
            elif text == instr:
                it = OxmlElement("w:instrText")
                it.set(qn("xml:space"), "preserve")
                it.text = f" {instr} "
                run._r.append(it)
            else:
                run.text = text or ""

    def fill_template(paragraph: Any, template: str, props: dict[str, Any] | None = None) -> None:
        start = len(paragraph.runs)
        template = template.replace("{{", "\x01").replace("}}", "\x02")
        for token in re.split(r"(\{page\}|\{pages\}|\{title\})", template):
            if token == "{page}":
                add_field(paragraph, "PAGE")
            elif token == "{pages}":
                add_field(paragraph, "NUMPAGES")
            elif token == "{title}":
                paragraph.add_run(clean(opts.title or doc_model["title"]))
            elif token:
                paragraph.add_run(clean(token.replace("\x01", "{").replace("\x02", "}")))
        for run in paragraph.runs[start:]:
            props = props or {}
            if props.get("font_family"):
                run.font.name = props["font_family"]
            if props.get("font_size_pt"):
                run.font.size = Pt(props["font_size_pt"])
            if props.get("color"):
                run.font.color.rgb = RGBColor.from_string(props["color"].lstrip("#"))
            for key in ("bold", "italic", "underline"):
                if key in props:
                    setattr(run, key, props[key])

    band_ids = [510000]

    def add_band_shapes(part: Any, paragraph: Any, edge: str, variant: str, left_hand: bool, model_section: dict[str, Any] | None, setup: PageSetup) -> None:
        """The shapes of this header or footer definition, anchored to the page where Writer's band put them (the Word part is the definition: every page using it repeats them)."""
        from .vector_shapes import add_docx_shape
        sid = (model_section or {}).get("id")
        if not sid or not band_store(doc_model):
            return
        physical = 2 if left_hand else 1
        items, mirrored = page_items(doc_model, sid, edge, physical, variant == "first")
        if variant == "first":                       # Word's first-page part: the section's own first-page list (page_items asked for it)
            pass
        for index, item in enumerate(items):
            at = place_band(item, edge, setup.width_mm, setup.height_mm, setup.left_for(physical), setup.right_for(physical), mirrored)
            band_ids[0] += 1
            warnings.extend(add_docx_shape(document, {"id": item["id"], "extensions": {"slai.writer.shape": {**item["shape"], "width_mm": at["w"], "height_mm": at["h"]}}}, setup,
                                           paragraph=paragraph, anchor={"x_mm": at["x"], "y_mm": at["y"], "behind": item.get("layer", "front") == "behind", "relative_height": 251658240 + index * 1024},
                                           ident=band_ids[0]))

    def apply_header_footer(docx_section: Any, model_section: dict[str, Any] | None, setup: PageSetup) -> None:
        first_shapes = bool((band_store(doc_model).get((model_section or {}).get("id")) or {}).get("first_page"))
        targets = [("header", docx_section.header, False, "default"), ("footer", docx_section.footer, False, "default")]
        if paired_document:                                          # Word keeps right-hand and left-hand pages apart document-wide
            targets += [("header", docx_section.even_page_header, True, "even"), ("footer", docx_section.even_page_footer, True, "even")]
        if first_shapes:                                             # the first page has shapes of its own: Word needs a part for it, with the same text as the others
            docx_section.different_first_page_header_footer = True
            targets += [("header", docx_section.first_page_header, False, "first"), ("footer", docx_section.first_page_footer, False, "first")]
        for edge, part, left_hand, variant in targets:
            template = (model_section or {}).get(edge)
            if template is None and edge == "footer" and opts.page_numbers and model_section is None:
                template = {"center": "{page}"}
            if left_hand:                                            # its own even-page text, or the ordinary one with the edges swapped in a book
                template = even_page_template(edge, template, setup)
            part.is_linked_to_previous = False
            paragraph = part.paragraphs[0]
            paragraph.style = document.styles["Normal"]      # the built-in Header style carries its own tab stops
            for r in list(paragraph.runs):
                r._r.getparent().remove(r._r)
            add_band_shapes(part, paragraph, edge, variant, left_hand, model_section, setup)
            if not template:
                continue
            width = Mm(text_width_mm(setup))
            paragraph.paragraph_format.tab_stops.add_tab_stop(int(width / 2), WD_TAB_ALIGNMENT.CENTER)
            paragraph.paragraph_format.tab_stops.add_tab_stop(int(width), WD_TAB_ALIGNMENT.RIGHT)
            def fill_position(pos: str) -> None:
                rich = (template.get("runs") or {}).get(pos)
                fallback = (template.get("formats") or {}).get(pos, {})
                if rich and "".join(x["text"] for x in rich) == template.get(pos, ""):
                    for fragment in rich:
                        fill_template(paragraph, fragment["text"], {**fallback, **fragment.get("format", {})})
                else:
                    fill_template(paragraph, template.get(pos, ""), fallback)
            fill_position("left")
            paragraph.add_run("\t")
            fill_position("center")
            paragraph.add_run("\t")
            fill_position("right")
            for r in paragraph.runs:
                if r.font.size is None:
                    r.font.size = Pt(9)

    # The contents of the document: page-reference fields need a bookmark on each heading, and the page numbers Writer measured for the cache.
    toc_labels = ((doc_model.get("extensions") or {}).get("slai.writer") or {}).get("toc_page_labels") or {}
    toc_targets = {meta["heading"] for b in doc_model["blocks"]
                   if (meta := (b.get("extensions") or {}).get("slai.sldc.toc")) and meta.get("role") == "entry" and isinstance(meta.get("heading"), str)
                   } & {b["id"] for b in doc_model["blocks"] if b["type"] == "heading"}
    toc_title_meta = {meta["id"]: meta for b in doc_model["blocks"]
                      if (meta := (b.get("extensions") or {}).get("slai.sldc.toc")) and meta.get("role") == "title" and isinstance(meta.get("id"), str)}
    toc_looks = {b["extensions"]["slai.sldc.toc"]["id"]: b["extensions"]["slai.writer.toc_style"] for b in doc_model["blocks"]
                 if isinstance((b.get("extensions") or {}).get("slai.writer.toc_style"), dict)
                 and isinstance(((b["extensions"].get("slai.sldc.toc")) or {}).get("id"), str)}
    bookmark_id = [0]

    def toc_bookmark(block_id: str) -> str:
        return "_Toc" + re.sub(r"[^A-Za-z0-9]", "_", block_id)[:30]

    first_model = sections.get(section_order[0]) if section_order else None
    setup = effective_setup(first_model)
    apply_page(document.sections[0], setup, column_layouts.get(section_order[0]) if section_order else None)
    apply_header_footer(document.sections[0], first_model, setup)
    current_section_id = section_order[0] if section_order else None
    current_setup = [setup]
    column_breaks: list[Any] = []

    # ---- list numbering
    numbering_root = None
    try:
        numbering_root = document.part.numbering_part.element
    except Exception:  # noqa: BLE001
        numbering_root = None

    def new_num_for_style(style_name: str) -> int | None:
        if numbering_root is None:
            return None
        style = document.styles[style_name]
        ppr = style.element.pPr
        if ppr is None or ppr.numPr is None or ppr.numPr.numId is None:
            return None
        base_id = str(ppr.numPr.numId.val)
        abstract = None
        for num in numbering_root.findall(qn("w:num")):
            if num.get(qn("w:numId")) == base_id:
                abstract = num.find(qn("w:abstractNumId")).get(qn("w:val"))
        if abstract is None:
            return None
        ids = [int(n.get(qn("w:numId"))) for n in numbering_root.findall(qn("w:num"))]
        new_id = max(ids + [0]) + 1
        num = OxmlElement("w:num")
        num.set(qn("w:numId"), str(new_id))
        a = OxmlElement("w:abstractNumId")
        a.set(qn("w:val"), abstract)
        num.append(a)
        override = OxmlElement("w:lvlOverride")
        override.set(qn("w:ilvl"), "0")
        start = OxmlElement("w:startOverride")
        start.set(qn("w:val"), "1")
        override.append(start)
        num.append(override)
        numbering_root.append(num)
        return new_id

    # ---- inline content
    def hyperlink(paragraph: Any, url: str, text: str, props: dict[str, Any]) -> None:
        rid = paragraph.part.relate_to(url, RT.HYPERLINK, is_external=True)
        link = OxmlElement("w:hyperlink")
        link.set(qn("r:id"), rid)
        run = paragraph.add_run(text)
        style_run(run, props)
        run.font.color.rgb = RGBColor(0x05, 0x63, 0xC1)
        run.font.underline = True
        link.append(run._r)
        paragraph._p.append(link)

    def style_run(run: Any, attrs: dict[str, Any], base: dict[str, Any] | None = None) -> None:
        merged = {**(base or {}), **attrs}
        if merged.get("bold"):
            run.bold = True
        if merged.get("italic"):
            run.italic = True
        if merged.get("underline"):
            run.underline = True
        if merged.get("strike"):
            run.font.strike = True
        if merged.get("superscript"):
            run.font.superscript = True
        if merged.get("subscript"):
            run.font.subscript = True
        if merged.get("small_caps"):
            run.font.small_caps = True
        if merged.get("code"):
            run.font.name = MONO
            r = run._r.get_or_add_rPr().find(qn("w:rFonts"))
            if r is not None:
                for a in ("w:ascii", "w:hAnsi", "w:cs"):
                    r.set(qn(a), MONO)
        if merged.get("font_family") and not merged.get("code"):
            run.font.name = merged["font_family"]
        if merged.get("font_size_pt"):
            run.font.size = Pt(merged["font_size_pt"])
        if merged.get("color"):
            run.font.color.rgb = RGBColor.from_string(merged["color"][1:].upper())
        if merged.get("background"):
            shd = OxmlElement("w:shd")
            shd.set(qn("w:val"), "clear")
            shd.set(qn("w:color"), "auto")
            shd.set(qn("w:fill"), merged["background"][1:].upper())
            run._r.get_or_add_rPr().append(shd)

    def add_inline(paragraph: Any, block: dict[str, Any], base: dict[str, Any] | None = None) -> None:
        for seg, attrs in span_segments(block["text"], block.get("spans")):
            props = dict(attrs)
            if props.get("style") in styles_char:
                props = {**styles_char[props["style"]], **props}
            text = clean(seg)
            if props.get("href"):
                hyperlink(paragraph, props["href"], text, {**(base or {}), **props})
            else:
                run = paragraph.add_run(text)
                style_run(run, props, base)
            if props.get("ref"):
                kind, number = notes.use(props["ref"])
                if number:
                    marker = paragraph.add_run(f"[{number}]")
                    marker.font.superscript = True

    def apply_paragraph_style(paragraph: Any, block: dict[str, Any]) -> dict[str, Any]:
        base: dict[str, Any] = {}
        props = styles_para.get(block.get("style", ""), {})
        pf = paragraph.paragraph_format
        align = (block.get("attributes") or {}).get("align") or props.get("align")
        if align:
            paragraph.alignment = {"left": WD_ALIGN_PARAGRAPH.LEFT, "center": WD_ALIGN_PARAGRAPH.CENTER,
                                   "right": WD_ALIGN_PARAGRAPH.RIGHT, "justify": WD_ALIGN_PARAGRAPH.JUSTIFY}[align]
        if "line_height" in props:
            pf.line_spacing = props["line_height"]
        if "space_before_pt" in props:
            pf.space_before = Pt(props["space_before_pt"])
        if "space_after_pt" in props:
            pf.space_after = Pt(props["space_after_pt"])
        if "indent_mm" in props:
            pf.left_indent = Mm(props["indent_mm"])
        if "first_line_indent_mm" in props:
            pf.first_line_indent = Mm(props["first_line_indent_mm"])
        if props.get("keep_with_next"):
            pf.keep_with_next = True
        for key in ("bold", "italic", "underline", "strike", "small_caps", "font_family", "font_size_pt", "color", "background"):
            if key in props:
                base[key] = props[key]
        if has_page_break(block):
            pf.page_break_before = True
        return base

    def style_exists(name: str) -> bool:
        try:
            document.styles[name]
            return True
        except KeyError:
            return False

    body_width_mm = [text_width_mm(setup)]

    def maybe_new_section(block: dict[str, Any]) -> None:
        nonlocal current_section_id
        sid = block.get("section")
        if sid and sid != current_section_id:
            model = sections.get(sid)
            new_setup = effective_setup(model)
            layout = column_layouts.get(sid) or {}
            before = current_setup[0]
            # A section Writer made for a selection (or the text after it) continues on the same page when the sheet is the same.
            same_sheet = (before.width_mm, before.height_mm, before.top, before.right, before.bottom, before.left, before.mirror, before.gutter) == \
                (new_setup.width_mm, new_setup.height_mm, new_setup.top, new_setup.right, new_setup.bottom, new_setup.left, new_setup.mirror, new_setup.gutter)
            kind = WD_SECTION.CONTINUOUS if layout.get("continuous") is True and same_sheet else WD_SECTION.NEW_PAGE
            docx_section = document.add_section(kind)
            apply_page(docx_section, new_setup, layout)
            apply_header_footer(docx_section, model, new_setup)
            body_width_mm[0] = text_width_mm(new_setup)
            current_setup[0] = new_setup
            current_section_id = sid
        if (block.get("extensions") or {}).get("slai.writer.column_break"):
            marker = document.add_paragraph()                       # moved into the block's own paragraph once it exists (see below)
            column_breaks.append(marker._p)

    from .vector_shapes import supported as is_vector_shape, add_docx_shape

    # ---- blocks
    for kind, item in group_blocks(doc_model["blocks"]):
        if kind == "list":
            run_blocks = item
            nums: dict[int, int | None] = {}
            for block in run_blocks:
                maybe_new_section(block)
                attrs = block.get("attributes") or {}
                depth = min(int(attrs.get("depth", 0)), 8)
                ordered = bool(attrs.get("ordered"))
                for d in [d for d in nums if d > depth]:
                    del nums[d]
                level_name = ("List Number" if ordered else "List Bullet") + ("" if depth == 0 else f" {min(depth + 1, 3)}")
                style_name = level_name if style_exists(level_name) else ("List Number" if ordered else "List Bullet")
                p = document.add_paragraph(style=style_name)
                if ordered:
                    if depth not in nums:
                        nums[depth] = new_num_for_style(style_name)
                    if nums[depth] is not None:
                        ppr = p._p.get_or_add_pPr()
                        num_pr = OxmlElement("w:numPr")
                        ilvl = OxmlElement("w:ilvl")
                        ilvl.set(qn("w:val"), "0")
                        nid = OxmlElement("w:numId")
                        nid.set(qn("w:val"), str(nums[depth]))
                        num_pr.append(ilvl)
                        num_pr.append(nid)
                        ppr.append(num_pr)
                else:
                    nums.pop(depth, None)
                if depth >= 3:
                    p.paragraph_format.left_indent = Mm(6.35 * (depth + 1))
                base = apply_paragraph_style(p, block)
                add_inline(p, block, base)
            continue
        block = item
        maybe_new_section(block)
        t = block["type"]
        if is_vector_shape(block):
            warnings.extend(add_docx_shape(document, block, effective_setup(sections.get(current_section_id))))
        elif t == "heading":
            level = heading_level(block)
            style_name = f"Heading {level}" if style_exists(f"Heading {level}") else "Normal"
            p = document.add_paragraph(style=style_name)
            base = apply_paragraph_style(p, block)
            add_inline(p, block, base)
            if block["id"] in toc_targets:               # a bookmark the contents entries point to with a page-reference field
                bookmark_id[0] += 1
                start = OxmlElement("w:bookmarkStart")
                start.set(qn("w:id"), str(bookmark_id[0]))
                start.set(qn("w:name"), toc_bookmark(block["id"]))
                end = OxmlElement("w:bookmarkEnd")
                end.set(qn("w:id"), str(bookmark_id[0]))
                properties = p._p.find(qn("w:pPr"))
                p._p.insert(1 if properties is not None else 0, start)
                p._p.append(end)
        elif (block.get("extensions") or {}).get("slai.sldc.toc", {}).get("role") == "entry":
            p = document.add_paragraph()
            base = apply_paragraph_style(p, block)
            meta = block["extensions"]["slai.sldc.toc"]
            title_meta = toc_title_meta.get(meta.get("id"), {})
            look = resolve_look(toc_looks.get(meta.get("id")), show_page_numbers=bool(title_meta.get("show_page_numbers", True)),
                                leader_dots=bool(title_meta.get("leader_dots", True)))
            label = toc_labels.get(meta.get("heading", "")) if look["position"] != "hidden" else None
            target = toc_bookmark(meta.get("heading", "")) if label and meta.get("heading") in toc_targets else None
            if not target:
                add_inline(p, block, base)
                continue
            # The parts of the entry in the order the layout asks for, with the tab stops that put them where it asks. The width of the page-number column is
            # estimated from the digits (a word processor measures them itself when it updates the fields).
            from docx.enum.text import WD_TAB_LEADER
            props = {**(styles_para.get(block.get("style", ""), {})), **(block.get("attributes") or {})}
            size = float(props.get("font_size_pt") or opts.font_size_pt)
            widest = max((len(v) for v in toc_labels.values()), default=1) * 0.56 * size
            column_w = float(look["number_column_pt"] or widest)
            plan = tab_plan(look, body_width_mm[0] * MM_PT, float(props.get("indent_mm", 0)) * MM_PT, 0.0, column_w, props.get("align", "left"))
            leader_name = word_leader(look)
            kinds = {"left": WD_TAB_ALIGNMENT.LEFT, "center": WD_TAB_ALIGNMENT.CENTER, "right": WD_TAB_ALIGNMENT.RIGHT}
            leaders = {"dot": WD_TAB_LEADER.DOTS, "hyphen": WD_TAB_LEADER.DASHES, "underscore": WD_TAB_LEADER.LINES, "middleDot": WD_TAB_LEADER.MIDDLE_DOT, None: WD_TAB_LEADER.SPACES}
            for stop in plan["stops"]:
                p.paragraph_format.tab_stops.add_tab_stop(Pt(stop["x"]), kinds[stop["align"]], leaders[leader_name if stop["leader"] else None])
            for part in plan["sequence"]:
                if part == "text":
                    add_inline(p, block, base)
                elif part == "tab":
                    p.add_run("\t")
                elif part == "gap":
                    p.add_run("   ")
                else:
                    add_field(p, f"PAGEREF {target} \\h", label)
        elif t == "quote":
            p = document.add_paragraph(style="Quote" if style_exists("Quote") else "Normal")
            base = apply_paragraph_style(p, block)
            add_inline(p, block, base)
        elif t == "code":
            p = document.add_paragraph()
            p.paragraph_format.space_after = Pt(6)
            ppr = p._p.get_or_add_pPr()
            shd = OxmlElement("w:shd")
            shd.set(qn("w:val"), "clear")
            shd.set(qn("w:color"), "auto")
            shd.set(qn("w:fill"), "F2F2F2")
            ppr.append(shd)
            if has_page_break(block):
                p.paragraph_format.page_break_before = True
            run = p.add_run(clean(block["text"]))
            style_run(run, {"code": True})
            run.font.size = Pt(max(8, opts.font_size_pt - 1.5))
        elif t == "table":
            if block["text"]:
                cap = document.add_paragraph(style="Caption" if style_exists("Caption") else "Normal")
                cap.add_run(clean(block["text"]))
                cap.paragraph_format.keep_with_next = True
                if has_page_break(block):
                    cap.paragraph_format.page_break_before = True
            _docx_table(document, block, style_exists, qn, OxmlElement, WD_TABLE_ALIGNMENT, WD_ALIGN_PARAGRAPH, Pt, style_run, body_width_mm[0],
                        page_break=has_page_break(block) and not block["text"])
            document.add_paragraph()
        elif t == "figure":
            _docx_figure(document, package, block, body_width_mm[0], style_exists, qn, Mm, WD_ALIGN_PARAGRAPH, warnings)
        elif rule_look(block) is not None:
            _docx_rule(document, rule_look(block) or {}, has_page_break(block), OxmlElement, qn, Pt)
        else:
            if not block["text"].strip():
                if has_page_break(block):  # an empty anchor (a page without extractable text) keeps its page
                    p = document.add_paragraph()
                    p.paragraph_format.page_break_before = True
                continue
            p = document.add_paragraph()
            base = apply_paragraph_style(p, block)
            add_inline(p, block, base)

    for marker in column_breaks:        # a column break belongs at the start of the block's own paragraph, not in a line of its own
        run = OxmlElement("w:r")
        br = OxmlElement("w:br")
        br.set(qn("w:type"), "column")
        run.append(br)
        following = marker.getnext()
        if following is not None and following.tag == qn("w:p") and following.find(qn("w:pPr") + "/" + qn("w:sectPr")) is None:
            marker.getparent().remove(marker)
            properties = following.find(qn("w:pPr"))
            following.insert(1 if properties is not None else 0, run)
        else:
            marker.append(run)
    if opts.include_notes and (notes.note_numbers or notes.cited):
        h = document.add_paragraph(style="Heading 2" if style_exists("Heading 2") else "Normal")
        h.add_run("Notes")
        for n, text in notes.footnotes():
            p = document.add_paragraph()
            r = p.add_run(f"[{n}] ")
            r.font.superscript = True
            p.add_run(clean(text))
        for line in notes.citation_lines():
            document.add_paragraph(clean(line))
        warnings.append("Footnotes are written as a numbered 'Notes' list at the end (python-docx cannot create native footnotes)")
    if paired_document or gutter_at_top[0]:
        settings = document.settings.element
        # The order of the children of w:settings is fixed by the format; each new one goes before the first child that must come after it.
        later = ["alignBordersAndEdges", "bordersDoNotSurroundHeader", "bordersDoNotSurroundFooter", "gutterAtTop", "hideSpellingErrors", "hideGrammaticalErrors",
                 "activeWritingStyle", "proofState", "formsDesign", "attachedTemplate", "linkStyles", "stylePaneFormatFilter", "stylePaneSortMethod",
                 "documentType", "mailMerge", "revisionView", "trackRevisions", "doNotTrackMoves", "doNotTrackFormatting", "documentProtection",
                 "autoFormatOverride", "styleLockTheme", "styleLockQFSet", "defaultTabStop", "autoHyphenation", "evenAndOddHeaders", "characterSpacingControl",
                 "compat", "rsids", "themeFontLang", "clrSchemeMapping", "decimalSymbol", "listSeparator"]

        def put(name: str) -> None:
            if settings.find(qn("w:" + name)) is not None:
                return
            node = OxmlElement("w:" + name)
            successors = later[later.index(name) + 1:] if name in later else later
            for child in settings:
                if child.tag in {qn("w:" + n) for n in successors}:
                    child.addprevious(node)
                    return
            settings.append(node)
        if mirrored_document:
            put("mirrorMargins")
        if paired_document:
            document.settings.odd_and_even_pages_header_footer = True
        if gutter_at_top[0]:
            put("gutterAtTop")
    if mirrored_document:
        plain = [sid for sid in section_order if not (book_of(doc_model, sid) or {}).get("mirror") is True
                 and (sections[sid].get("page", {}).get("margins_mm", {}).get("left", 25) != sections[sid].get("page", {}).get("margins_mm", {}).get("right", 25))]
        if plain:
            warnings.append("Word mirrors margins for the whole document, so " + ", ".join(plain) + " (whose left and right margins differ) alternate their margins in Word")
    document.save(str(dest))
    return WriteResult(dest, "docx", "python-docx", warnings=warnings,
                       kept=["headings (Heading 1-6)", "lists with restarting numbers", "tables with merged cells and header rows",
                             "bold/italic/underline/strike/super/subscript/colour", "hyperlinks", "images with alt text", "native editable DrawingML vector shapes",
                             "page size, margins, mirrored margins and the binding gutter, columns (continuous sections for a selection, unequal widths, column breaks, right-to-left flow)", "contents entries with a leader tab and page-reference fields", "headers and footers with page numbers", "page breaks",
                             "document title, author, language"],
                       lost=["native footnotes (written as a Notes list)", "colour, thickness and style of the line between columns (Word draws its own thin line)", "comments and revision history", "signatures"])


_DOCX_LINE = {"solid": "single", "dashed": "dashed", "dotted": "dotted", "double": "double"}


def _docx_rule(document: Any, look: dict[str, Any], page_break: bool, OxmlElement: Any, qn: Any, Pt: Any) -> None:
    """A horizontal rule as Word draws one: an empty one-point paragraph with a bottom border, no indents (so it spans the text width) and the rule's spacing."""
    p = document.add_paragraph()
    fmt = p.paragraph_format
    fmt.space_before, fmt.space_after = Pt(look["space_before_pt"]), Pt(look["space_after_pt"])
    fmt.left_indent = fmt.right_indent = Pt(0)
    if page_break:
        fmt.page_break_before = True
    ppr = p._p.get_or_add_pPr()
    spacing = ppr.find(qn("w:spacing"))
    spacing.set(qn("w:line"), "20")                                    # exactly one point high, so the line is all there is
    spacing.set(qn("w:lineRule"), "exact")
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), _DOCX_LINE.get(look["style"], "single"))
    bottom.set(qn("w:sz"), str(max(2, min(96, round(look["width_pt"] * 8)))))      # eighths of a point; a double line draws two of them
    bottom.set(qn("w:space"), "0")
    bottom.set(qn("w:color"), look["color"].lstrip("#").upper())
    borders.append(bottom)
    ppr.insert_element_before(borders, "w:shd", "w:tabs", "w:suppressAutoHyphens", "w:kinsoku", "w:wordWrap", "w:overflowPunct", "w:topLinePunct", "w:autoSpaceDE",
                              "w:autoSpaceDN", "w:bidi", "w:adjustRightInd", "w:snapToGrid", "w:spacing", "w:ind", "w:contextualSpacing", "w:mirrorIndents",
                              "w:suppressOverlap", "w:jc", "w:textDirection", "w:textAlignment", "w:textboxTightWrap", "w:outlineLvl", "w:divId", "w:cnfStyle",
                              "w:rPr", "w:sectPr", "w:pPrChange")
    mark = OxmlElement("w:rPr")                                        # the paragraph mark is tiny too
    size = OxmlElement("w:sz")
    size.set(qn("w:val"), "2")
    mark.append(size)
    ppr.append(mark)


def _docx_table(document: Any, block: dict[str, Any], style_exists: Any, qn: Any, OxmlElement: Any,
                WD_TABLE_ALIGNMENT: Any, WD_ALIGN_PARAGRAPH: Any, Pt: Any, style_run: Any, text_width_mm: float = 160.0, page_break: bool = False) -> None:
    from docx.shared import Mm, RGBColor
    grid = table_grid(block["table"])
    look = table_look(block, grid)
    cells_look = (look or {}).get("cells", {})
    table = document.add_table(rows=len(grid.rows), cols=grid.columns)
    if look is None and style_exists("Table Grid"):
        table.style = "Table Grid"
    table.alignment = {"center": WD_TABLE_ALIGNMENT.CENTER, "right": WD_TABLE_ALIGNMENT.RIGHT}.get((look or {}).get("align"), WD_TABLE_ALIGNMENT.CENTER if look is None else WD_TABLE_ALIGNMENT.LEFT)
    origins = []
    for r, row in enumerate(grid.rows):
        for c, cell in enumerate(row):
            if cell is None:
                continue
            origins.append((r, c, cell))
            target = table.cell(r, c)
            target.text = ""
            para = target.paragraphs[0]
            mine = cells_look.get(f"{r},{c}", {})
            bold = bool(mine.get("bold")) if look is not None else bool(r < grid.header_rows or cell.get("header"))
            ink = hex_rgb(mine.get("color"))
            for segment, props in span_segments(cell["text"], cell.get("spans")):
                run = para.add_run(clean(segment))
                style_run(run, props, {"bold": bold})
                if ink:
                    run.font.color.rgb = RGBColor(*(round(v * 255) for v in ink))
            align = cell.get("align")
            if align:
                para.alignment = {"left": WD_ALIGN_PARAGRAPH.LEFT, "center": WD_ALIGN_PARAGRAPH.CENTER,
                                  "right": WD_ALIGN_PARAGRAPH.RIGHT, "justify": WD_ALIGN_PARAGRAPH.JUSTIFY}[align]
    for r, c, cell in origins:
        cs, rs = cell.get("colspan", 1), cell.get("rowspan", 1)
        if cs > 1 or rs > 1:
            table.cell(r, c).merge(table.cell(r + rs - 1, c + cs - 1))
    if page_break:                                     # a table has no break of its own: the first paragraph in it carries it (Word and LibreOffice both honour that)
        table.cell(0, 0).paragraphs[0].paragraph_format.page_break_before = True
    for r in range(grid.header_rows):
        tr = table.rows[r]._tr
        trpr = tr.get_or_add_trPr()
        flag = OxmlElement("w:tblHeader")
        trpr.append(flag)
    if look is None:
        return

    def child(parent: Any, tag: str) -> Any:
        found = parent.find(qn(tag))
        if found is None:
            found = OxmlElement(tag)
            parent.append(found)
        return found

    def border(parent: Any, side: str, spec: dict[str, Any] | None) -> None:
        el = OxmlElement(f"w:{side}")
        if spec is None:
            el.set(qn("w:val"), "nil")
        else:
            el.set(qn("w:val"), _DOCX_LINE.get(spec.get("style", "solid"), "single"))
            el.set(qn("w:sz"), str(max(2, min(96, round(float(spec["width_pt"]) * 8)))))
            el.set(qn("w:space"), "0")
            el.set(qn("w:color"), spec["color"][1:].upper())
        parent.append(el)

    tblPr = table._tbl.tblPr
    # ---- widths: the ones the person set, otherwise equal columns across the text width (how the editor lays a table out), or the width asked for
    widths_mm = look.get("column_widths_mm")
    fit = look.get("fit", "auto")
    if isinstance(widths_mm, list) and len(widths_mm) == grid.columns:
        widths = [float(w) for w in widths_mm]
        if fit == "window":
            widths = [w * text_width_mm / sum(widths) for w in widths]
    else:
        total = min(float(look["width_mm"]), text_width_mm) if look.get("width_mm") and fit != "window" else text_width_mm
        widths = [total / grid.columns] * grid.columns
    widths = [w * min(1.0, text_width_mm / sum(widths)) if sum(widths) > text_width_mm else w for w in widths]
    twips = [round(w * 56.6929) for w in widths]
    for tag in ("w:tblW", "w:tblLayout"):
        old = tblPr.find(qn(tag))
        if old is not None:
            tblPr.remove(old)
    tblW = OxmlElement("w:tblW")
    tblW.set(qn("w:w"), str(sum(twips)))
    tblW.set(qn("w:type"), "dxa")
    tblPr.append(tblW)
    layout = OxmlElement("w:tblLayout")
    layout.set(qn("w:type"), "fixed")
    tblPr.append(layout)
    if look.get("indent_mm") and look.get("align") not in ("center", "right"):
        ind = OxmlElement("w:tblInd")
        ind.set(qn("w:w"), str(round(float(look["indent_mm"]) * 56.6929)))
        ind.set(qn("w:type"), "dxa")
        tblPr.append(ind)
    if look.get("cell_padding_mm") is not None:
        mar = OxmlElement("w:tblCellMar")
        pad = str(round(float(look["cell_padding_mm"]) * 56.6929))
        for side in ("top", "left", "bottom", "right"):
            el = OxmlElement(f"w:{side}")
            el.set(qn("w:w"), pad)
            el.set(qn("w:type"), "dxa")
            mar.append(el)
        tblPr.append(mar)
    tblGrid = table._tbl.tblGrid
    for col, w in zip(tblGrid.findall(qn("w:gridCol")), twips):
        col.set(qn("w:w"), str(w))
    h_edges, v_edges = look["h"], look["v"]
    rows_n = len(grid.rows)
    heights = look.get("row_heights_mm")
    for r, tr in enumerate(table._tbl.tr_lst):
        if isinstance(heights, list) and len(heights) == rows_n:
            trpr = tr.get_or_add_trPr()
            height = OxmlElement("w:trHeight")
            height.set(qn("w:val"), str(round(float(heights[r]) * 56.6929)))
            height.set(qn("w:hRule"), "exact" if look.get("row_height_rule") == "exact" else "atLeast")
            trpr.append(height)
        col = 0
        for tc in tr.tc_lst:
            tcPr = tc.get_or_add_tcPr()
            span = tcPr.find(qn("w:gridSpan"))
            cs = int(span.get(qn("w:val"))) if span is not None else 1
            vmerge = tcPr.find(qn("w:vMerge"))
            origin = vmerge is None or vmerge.get(qn("w:val")) == "restart"
            rs = 1
            if origin:
                owner = grid.rows[r][col] if col < grid.columns else None
                rs = (owner or {}).get("rowspan", 1) if owner else 1
            width = child(tcPr, "w:tcW")
            width.set(qn("w:w"), str(sum(twips[col:col + cs])))
            width.set(qn("w:type"), "dxa")
            for stale in tcPr.findall(qn("w:tcBorders")) + tcPr.findall(qn("w:shd")):
                tcPr.remove(stale)
            borders = OxmlElement("w:tcBorders")
            if vmerge is None or origin:
                border(borders, "top", h_edges.get(f"{r},{col}"))
            if vmerge is None or vmerge.get(qn("w:val")) is None:
                border(borders, "bottom", h_edges.get(f"{r + 1},{col}"))
            elif origin:
                border(borders, "bottom", h_edges.get(f"{r + rs},{col}"))
            border(borders, "left", v_edges.get(f"{r},{col}"))
            border(borders, "right", v_edges.get(f"{r},{col + cs}"))
            order = {"top": 0, "left": 1, "bottom": 2, "right": 3}
            borders[:] = sorted(borders, key=lambda e: order[e.tag.split('}')[1]])
            mine = cells_look.get(f"{r},{col}", {})
            fill = mine.get("fill") if origin else None
            tcPr.append(borders)
            if fill:
                shd = OxmlElement("w:shd")
                shd.set(qn("w:val"), "clear")
                shd.set(qn("w:color"), "auto")
                shd.set(qn("w:fill"), fill[1:].upper())
                tcPr.append(shd)
            if mine.get("padding_mm") is not None and origin:
                mar = OxmlElement("w:tcMar")
                pad = str(round(float(mine["padding_mm"]) * 56.6929))
                for side in ("top", "left", "bottom", "right"):
                    el = OxmlElement(f"w:{side}")
                    el.set(qn("w:w"), pad)
                    el.set(qn("w:type"), "dxa")
                    mar.append(el)
                tcPr.append(mar)
            if mine.get("valign") and origin:
                va = OxmlElement("w:vAlign")
                va.set(qn("w:val"), {"top": "top", "middle": "center", "bottom": "bottom"}[mine["valign"]])
                tcPr.append(va)
            col += cs


def _docx_figure(document: Any, package: Any, block: dict[str, Any], width_mm: float, style_exists: Any, qn: Any,
                 Mm: Any, WD_ALIGN_PARAGRAPH: Any, warnings: list[str]) -> None:
    fig = block["figure"]
    got = asset_bytes(package, fig["asset"])
    if not got:
        warnings.append(f"Image {fig['asset']} is missing from the package and was skipped")
        return
    data, _ = got
    paragraph = document.add_paragraph()
    if has_page_break(block):
        paragraph.paragraph_format.page_break_before = True
    align = fig.get("align", "center")
    paragraph.alignment = {"left": WD_ALIGN_PARAGRAPH.LEFT, "center": WD_ALIGN_PARAGRAPH.CENTER,
                           "right": WD_ALIGN_PARAGRAPH.RIGHT}[align]
    target = min(float(fig.get("width_mm", width_mm)), width_mm)
    run = paragraph.add_run()
    picture = run.add_picture(io.BytesIO(data), width=Mm(target))
    doc_pr = picture._inline.docPr
    alt = fig.get("alt", "")
    if alt:
        doc_pr.set("descr", clean(alt))
    if block["text"]:
        cap = document.add_paragraph(style="Caption" if style_exists("Caption") else "Normal")
        cap.add_run(clean(block["text"]))
        cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
