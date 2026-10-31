"""PDF writer (reportlab). Produces a real, text-searchable PDF with embedded fonts: headings with a PDF outline, paragraphs
with inline formatting and links, nested lists, tables (merged cells, repeating header rows), quotes, code, images, page
size/margins/columns per section, headers and footers with page numbers (two passes, so {pages} is the real total), page
breaks, document properties.

Honest limits (always reported in the result): footnotes are written as endnotes; the PDF carries no accessibility tags or
alt text (reportlab cannot write them); right-to-left scripts are not shaped; CJK needs a TrueType CJK font on the machine
(Adobe CID fallback fonts are not embedded).
"""
from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any

from ...errors import DependencyMissing
from .. import fonts as fontmod
from ..ir import has_page_break, heading_level, span_segments
from . import WriteOptions, WriteResult
from .common import (Notes, PAGE_SIZES_MM, PageSetup, asset_bytes, char_style_table, clean, format_number, group_blocks,
                     list_labels, page_setup, resolve_header_footer, style_table, table_grid)

MM = 72.0 / 25.4
_registered: dict[str, str] = {}


def write_pdf(package: Any, dest: Path, opts: WriteOptions) -> WriteResult:
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.pdfmetrics import registerFontFamily
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.pdfgen import canvas as canvasmod
        from reportlab.platypus import (BaseDocTemplate, Flowable, Frame, Image, NextPageTemplate, PageBreak, PageTemplate,
                                        Paragraph, Spacer, Table, TableStyle)
        from reportlab.platypus.doctemplate import LayoutError
    except ImportError as exc:
        raise DependencyMissing("Writing PDF needs the reportlab package", details={"package": "reportlab"}) from exc

    doc = package.document
    notes = Notes.of(doc)
    styles_para = style_table(doc)
    styles_char = char_style_table(doc)
    sections = {s["id"]: s for s in doc.get("sections", [])}
    order = [s["id"] for s in doc.get("sections", [])]
    title = opts.title or doc["title"]
    warnings: list[str] = []
    all_text = "\n".join(b.get("text", "") for b in doc["blocks"]) + "".join(
        c["text"] for b in doc["blocks"] if b["type"] == "table" for row in b["table"]["rows"] for c in row)
    if fontmod.has_rtl(all_text):
        warnings.append("This document contains right-to-left text (Arabic/Hebrew). The PDF writer does not shape or reorder "
                        "it, so it will not display correctly")

    # ---------------------------------------------------------------- fonts
    plan = fontmod.plan(opts.font_family)
    warnings.extend(plan.notes)
    key = f"{plan.body.label if plan.body else 'std'}|{plan.mono.label if plan.mono else 'std'}"

    def register(prefix: str, family: Any, fallback_name: str) -> str:
        names = [f"{prefix}-R", f"{prefix}-B", f"{prefix}-I", f"{prefix}-BI"]
        tag = prefix + ":" + (family.label if family else fallback_name)
        if _registered.get(prefix) != tag:
            if family is None:
                return fallback_name
            for name, path in zip(names, family.files):
                try:
                    pdfmetrics.registerFont(TTFont(name, str(path)))
                except Exception:  # noqa: BLE001
                    return fallback_name
            registerFontFamily(prefix + "-R", normal=names[0], bold=names[1], italic=names[2], boldItalic=names[3])
            _registered[prefix] = tag
        return names[0]

    body_font = register("DMBody", plan.body, "Helvetica")
    mono_font = register("DMMono", plan.mono, "Courier")
    fb_font = register("DMFall", plan.fallback, body_font) if plan.fallback else body_font
    if plan.body is None:
        warnings.append("No TrueType font was found on this machine; the built-in Helvetica is used, which covers Western "
                        "European characters only")
    cover_body = fontmod.coverage_of(plan.body.files[0]) if plan.body else set(range(32, 256))
    cover_mono = fontmod.coverage_of(plan.mono.files[0]) if plan.mono else set(range(32, 256))
    cover_fb = fontmod.coverage_of(plan.fallback.files[0]) if plan.fallback else set()
    cjk_font = None
    cover_cjk: set[int] = set()
    if plan.cjk and any(fontmod.is_cjk(ch) for ch in all_text):
        label, path, index = plan.cjk
        try:
            pdfmetrics.registerFont(TTFont("DMCJK", str(path), subfontIndex=index))
            registerFontFamily("DMCJK", normal="DMCJK", bold="DMCJK", italic="DMCJK", boldItalic="DMCJK")
            cjk_font = "DMCJK"
            cover_cjk = fontmod.coverage_of(path, index)
        except Exception:  # noqa: BLE001
            cjk_font = None
    if cjk_font is None and any(fontmod.is_cjk(ch) for ch in all_text):
        try:
            from reportlab.pdfbase.cidfonts import UnicodeCIDFont
            for cid in ("STSong-Light", "HeiseiMin-W3", "HYSMyeongJo-Medium"):
                pdfmetrics.registerFont(UnicodeCIDFont(cid))
            cjk_font = "CID"
            warnings.append("No TrueType CJK font was found; Chinese/Japanese/Korean text uses Adobe's standard CJK fonts, which "
                            "are not embedded. Viewers without them substitute a font")
        except Exception:  # noqa: BLE001
            warnings.append("CJK text cannot be written: no CJK font is available")
    missing: set[str] = set()

    def cid_for(ch: str) -> str:
        c = ord(ch)
        if 0x3040 <= c <= 0x30FF:
            return "HeiseiMin-W3"
        if 0xAC00 <= c <= 0xD7AF or 0x1100 <= c <= 0x11FF:
            return "HYSMyeongJo-Medium"
        return "STSong-Light"

    def font_for(ch: str, mono: bool) -> str | None:
        """Font name to switch to for ``ch``, or None when the current (body/mono) font covers it."""
        code = ord(ch)
        if ch.isspace() or code < 32:
            return None
        if fontmod.is_cjk(ch):
            if cjk_font == "DMCJK" and code in cover_cjk:
                return "DMCJK"
            if cjk_font == "CID":
                return cid_for(ch)
            if cjk_font == "DMCJK":
                pass
        if mono:
            if code in cover_mono:
                return None
        elif code in cover_body:
            return None
        if code in cover_fb:
            return fb_font
        if cjk_font == "DMCJK" and code in cover_cjk:
            return "DMCJK"
        missing.add(ch)
        return None

    # ---------------------------------------------------------------- styles
    base = opts.font_size_pt
    leading = round(base * 1.35, 1)

    def pstyle(name: str, **kw: Any) -> ParagraphStyle:
        kw.setdefault("fontName", body_font)
        kw.setdefault("fontSize", base)
        kw.setdefault("leading", leading)
        return ParagraphStyle(name, **kw)

    cjk_wrap = "CJK" if any(fontmod.is_cjk(ch) for ch in all_text) else None
    st_body = pstyle("body", spaceAfter=base * 0.55, alignment=TA_LEFT, wordWrap=cjk_wrap, splitLongWords=1)
    heading_sizes = {1: 1.9, 2: 1.5, 3: 1.25, 4: 1.1, 5: 1.0, 6: 0.95}
    st_head = {lv: pstyle(f"h{lv}", fontName=body_font, fontSize=round(base * mult, 1), leading=round(base * mult * 1.2, 1),
                          spaceBefore=base * (1.2 if lv < 3 else 0.9), spaceAfter=base * 0.4, keepWithNext=1,
                          wordWrap=cjk_wrap) for lv, mult in heading_sizes.items()}
    st_list = pstyle("li", spaceAfter=base * 0.25, wordWrap=cjk_wrap, bulletFontName=body_font, bulletFontSize=base)
    st_quote = pstyle("quote", leftIndent=10 * MM, rightIndent=6 * MM, textColor=colors.HexColor("#444444"),
                      borderPadding=(2, 2, 2, 6), spaceBefore=base * 0.4, spaceAfter=base * 0.8, wordWrap=cjk_wrap)
    st_code = pstyle("code", fontName=mono_font, fontSize=max(7.5, base - 1.5), leading=max(9, round((base - 1.5) * 1.3, 1)),
                     backColor=colors.HexColor("#f3f4f6"), borderPadding=4, spaceBefore=base * 0.3, spaceAfter=base * 0.8)
    st_cell = pstyle("cell", fontSize=max(8, base - 1), leading=max(10, round((base - 1) * 1.3, 1)), wordWrap=cjk_wrap)
    st_caption = pstyle("caption", fontSize=max(8, base - 1.5), alignment=TA_CENTER, textColor=colors.HexColor("#444444"),
                        spaceAfter=base * 0.8)
    ALIGN = {"left": TA_LEFT, "center": TA_CENTER, "right": TA_RIGHT, "justify": TA_JUSTIFY}
    xref_ids = {s["target"] for b in doc["blocks"] for s in b.get("spans", []) if s["type"] == "xref"}
    block_ids = {b["id"] for b in doc["blocks"]}
    measured_pages: dict[str, int] = {}
    printed_labels: list[str | None] = []
    template_ids: list[str] = []
    # A TOC's page labels are drawn in the PDF pass itself. The first pass
    # measures heading positions; later passes converge if the TOC reflows.
    reference_labels: dict[str, str] = {}
    toc_titles = {meta["id"]: meta for block in doc["blocks"]
                  if (meta := block.get("extensions", {}).get("slai.sldc.toc"))
                  and meta.get("role") == "title" and isinstance(meta.get("id"), str)}

    # ---------------------------------------------------------------- inline markup
    def markup_text(text: str, mono: bool) -> str:
        """Escape and wrap runs whose characters need another font."""
        out: list[str] = []
        run: list[str] = []
        current: str | None = None

        def flush() -> None:
            nonlocal run
            if not run:
                return
            chunk = "".join(run)
            out.append(f'<font name="{current}">{chunk}</font>' if current else chunk)
            run = []

        for ch in clean(text):
            if ch == "\n":
                flush()
                current = None
                out.append("<br/>")
                continue
            # A tab expands to printable whitespace in the PDF, but font_for
            # accepts exactly one Unicode scalar.  Passing the expanded four
            # spaces to ord() used to abort every PDF containing a tab.
            target = None if ch == "\t" else font_for(ch, mono)
            if ch == "\t":
                ch = "    "
            if target != current and not (ch.isspace() and current):
                flush()
                current = target
            run.append(ch.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        flush()
        return "".join(out)

    def inline(block: dict[str, Any], base_props: dict[str, Any] | None = None) -> str:
        parts = []
        for seg, attrs in span_segments(block["text"], block.get("spans")):
            props = dict(attrs)
            if props.get("style") in styles_char:
                props = {**styles_char[props["style"]], **props}
            merged = {**(base_props or {}), **props}
            xml = markup_text(seg, bool(merged.get("code")))
            if merged.get("code"):
                xml = f'<font name="{mono_font}">{xml}</font>'
            for flag, tag in (("bold", "b"), ("italic", "i"), ("underline", "u"), ("strike", "strike"),
                              ("superscript", "super"), ("subscript", "sub")):
                if merged.get(flag):
                    xml = f"<{tag}>{xml}</{tag}>"
            if merged.get("color"):
                xml = f'<font color="{merged["color"]}">{xml}</font>'
            if merged.get("background"):
                xml = f'<span backColor="{merged["background"]}">{xml}</span>'
            if props.get("href"):
                url = props["href"].replace("&", "&amp;").replace('"', "&quot;")
                xml = f'<a href="{url}" color="#0563C1">{xml}</a>'
            if props.get("target") in block_ids:
                xml = f'<a href="#{props["target"]}" color="#0563C1">{xml}</a>'
            if props.get("ref"):
                kind, number = notes.use(props["ref"])
                if number:
                    anchor = f"note-{number}" if kind == "footnote" else f"cite-{number}"
                    xml += f'<a href="#{anchor}" color="#0563C1"><super>[{number}]</super></a>'
            parts.append(xml)
        return "".join(parts)

    def style_for(block: dict[str, Any], default: ParagraphStyle) -> tuple[ParagraphStyle, dict[str, Any]]:
        # Direct paragraph formatting has precedence over inherited named styles.
        props = {**styles_para.get(block.get("style", ""), {}), **(block.get("attributes") or {})}
        align = props.get("align")
        kw: dict[str, Any] = {}
        if align:
            kw["alignment"] = ALIGN[align]
        size = props.get("font_size_pt")
        if size:
            kw["fontSize"] = size
            kw["leading"] = round(size * props.get("line_height", 1.3), 1)
        elif "line_height" in props:
            kw["leading"] = round(default.fontSize * props["line_height"], 1)
        if "space_before_pt" in props:
            kw["spaceBefore"] = props["space_before_pt"]
        if "space_after_pt" in props:
            kw["spaceAfter"] = props["space_after_pt"]
        if "indent_mm" in props:
            kw["leftIndent"] = default.leftIndent + props["indent_mm"] * MM
        if "first_line_indent_mm" in props:
            kw["firstLineIndent"] = props["first_line_indent_mm"] * MM
        if props.get("keep_with_next"):
            kw["keepWithNext"] = 1
        if props.get("font_family") and props["font_family"] != (plan.body.label if plan.body else ""):
            warnings.append(f"Style font '{props['font_family']}' is not embedded by the PDF writer; the body font is used") \
                if f"Style font '{props['font_family']}'" not in "".join(warnings) else None
        flags = {k: props[k] for k in ("bold", "italic", "underline", "strike", "color", "background", "small_caps") if k in props}
        return (ParagraphStyle(default.name + "x", parent=default, **kw) if kw else default), flags

    # ---------------------------------------------------------------- page templates
    def setup_for(section: dict[str, Any] | None) -> PageSetup:
        s = page_setup(section)
        if opts.page_size and not (section and "page" in section) and opts.page_size in PAGE_SIZES_MM:
            s.width_mm, s.height_mm = PAGE_SIZES_MM[opts.page_size]
        if opts.margins_mm is not None and not (section and "page" in section):
            s.top = s.right = s.bottom = s.left = opts.margins_mm
        s.top, s.bottom = max(s.top, 12.0), max(s.bottom, 12.0)
        return s

    section_info: dict[str, dict[str, Any]] = {}   # template id -> data for the final pass
    templates: list[PageTemplate] = []

    def make_template(tid: str, section: dict[str, Any] | None) -> PageSetup:
        s = setup_for(section)
        pw, ph = s.width_mm * MM, s.height_mm * MM
        usable_w = pw - (s.left + s.right) * MM
        gap = s.column_gap_mm * MM
        cols = max(1, s.columns)
        col_w = (usable_w - gap * (cols - 1)) / cols
        frames = [Frame(s.left * MM + i * (col_w + gap), s.bottom * MM, col_w, ph - (s.top + s.bottom) * MM,
                        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0, id=f"{tid}-c{i}") for i in range(cols)]

        def on_page(canvas: Any, document: Any, _tid: str = tid) -> None:
            canvas._dm_template = _tid

        templates.append(PageTemplate(id=tid, frames=frames, onPage=on_page, pagesize=(pw, ph)))
        section_info[tid] = {"section": section, "setup": s, "page": (pw, ph)}
        return s

    first_id = order[0] if order else "main"
    make_template(first_id, sections.get(first_id) if order else None)
    made = {first_id}
    for sid in order[1:]:                       # every section's template must exist before the document is built
        make_template(sid, sections[sid])
        made.add(sid)

    # ---------------------------------------------------------------- story
    story: list[Any] = []
    state = {"emitted": False, "outline_level": 0, "degraded": False}
    current_section = first_id

    def emit(flowable: Any) -> None:
        story.append(flowable)
        state["emitted"] = True

    def page_break() -> None:
        if state["emitted"]:
            story.append(PageBreak())
            state["emitted"] = False

    class Outline(Flowable):
        def __init__(self, text: str, level: int, key: str) -> None:
            super().__init__()
            self.text, self.level, self.key = text, level, key
            self.width = self.height = 0

        def wrap(self, *_: Any) -> tuple[int, int]:
            return 0, 0

        def draw(self) -> None:
            self.canv.bookmarkPage(self.key)
            self.canv.addOutlineEntry(self.text[:120], self.key, self.level, closed=False)

    class Anchor(Flowable):
        def __init__(self, key: str) -> None:
            super().__init__()
            self.key = key
            self.width = self.height = 0

        def wrap(self, *_: Any) -> tuple[int, int]:
            return 0, 0

        def draw(self) -> None:
            self.canv.bookmarkPage(self.key)

    class TrackedParagraph(Paragraph):
        """Keep the originating SLDC block ID even when ReportLab splits a paragraph."""

        def __init__(self, xml: str, style: Any, block_id: str, **kwargs: Any) -> None:
            super().__init__(xml, style, **kwargs)
            self._sldc_block_id = block_id

        def split(self, availWidth: float, availHeight: float) -> list[Any]:  # noqa: N802
            pieces = super().split(availWidth, availHeight)
            for piece in pieces:
                piece._sldc_block_id = self._sldc_block_id
            return pieces

    def start_block(block: dict[str, Any]) -> None:
        nonlocal current_section
        sid = block.get("section")
        if sid and sid != current_section:
            if sid not in made:
                make_template(sid, sections.get(sid))
                made.add(sid)
            story.append(NextPageTemplate(sid))
            page_break()
            current_section = sid
            state["emitted"] = False
        elif has_page_break(block):
            page_break()
        if block["id"] in xref_ids:
            story.append(Anchor(block["id"]))

    def frame_width(section_id: str) -> float:
        s = section_info[section_id]["setup"]
        usable = (s.width_mm - s.left - s.right) * MM
        cols = max(1, s.columns)
        return (usable - s.column_gap_mm * MM * (cols - 1)) / cols

    def frame_height(section_id: str) -> float:
        s = section_info[section_id]["setup"]
        return (s.height_mm - s.top - s.bottom) * MM

    def table_flowable(block: dict[str, Any], width: float) -> Any:
        grid = table_grid(block["table"])
        if state["degraded"]:
            rows = []
            for row in grid.rows:
                rows.append(" | ".join(c["text"].replace("\n", " ") for c in row if c is not None))
            return [Paragraph(markup_text(r, False), st_cell) for r in rows]
        data: list[list[Any]] = []
        weights = [6.0] * grid.columns
        for r, row in enumerate(grid.rows):
            line = []
            for c, cell in enumerate(row):
                if cell is None:
                    line.append("")
                    continue
                text = cell["text"]
                style = st_cell
                if cell.get("align"):
                    style = ParagraphStyle("cellx", parent=st_cell, alignment=ALIGN[cell["align"]])
                if r < grid.header_rows or cell.get("header"):
                    xml = f"<b>{markup_text(text, False)}</b>"
                else:
                    xml = markup_text(text, False)
                line.append(Paragraph(xml, style))
                if cell.get("colspan", 1) == 1:
                    longest = max([len(w) for w in text.split()] + [1])
                    weights[c] = max(weights[c], min(max(len(text) ** 0.7, longest), 40))
            data.append(line)
        total = sum(weights)
        widths = [max(width * w / total, 14.0) for w in weights]
        scale = width / sum(widths)
        widths = [w * scale for w in widths]
        table = Table(data, colWidths=widths, repeatRows=grid.header_rows, splitByRow=1)
        cmds: list[tuple] = [("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#9aa0a6")),
                             ("VALIGN", (0, 0), (-1, -1), "TOP"),
                             ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                             ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
        if grid.header_rows:
            cmds.append(("BACKGROUND", (0, 0), (-1, grid.header_rows - 1), colors.HexColor("#eceff3")))
        for r, row in enumerate(grid.rows):
            for c, cell in enumerate(row):
                if cell is not None and (cell.get("colspan", 1) > 1 or cell.get("rowspan", 1) > 1):
                    cmds.append(("SPAN", (c, r), (c + cell.get("colspan", 1) - 1, r + cell.get("rowspan", 1) - 1)))
        table.setStyle(TableStyle(cmds))
        return table

    def build_story() -> None:
        nonlocal current_section
        story.clear()
        state.update(emitted=False, outline_level=0)
        current_section = first_id
        story.append(NextPageTemplate(first_id))
        for kind, item in group_blocks(doc["blocks"]):
            if kind == "list":
                start_block(item[0])
                labels = list_labels(item)
                for label, block in zip(labels, item):
                    if block is not item[0]:
                        start_block(block)
                    depth = min(int((block.get("attributes") or {}).get("depth", 0)), 8)
                    style, flags = style_for(block, st_list)
                    style = ParagraphStyle("lix", parent=style, leftIndent=(depth + 1) * 7 * MM, bulletIndent=depth * 7 * MM + 1.5 * MM)
                    emit(TrackedParagraph(inline(block, flags), style, block["id"], bulletText=label))
                continue
            block = item
            start_block(block)
            t = block["type"]
            fw = frame_width(current_section)
            fh = frame_height(current_section)
            if t == "heading":
                lv = heading_level(block)
                style, flags = style_for(block, st_head[lv])
                flags = {"bold": True, **flags}
                key = f"h-{block['id']}"
                level = min(lv - 1, state["outline_level"] + 1)
                state["outline_level"] = level
                story.append(Outline(block["text"].split("\n")[0] or "Heading", level, key))
                emit(TrackedParagraph(inline(block, flags), style, block["id"]))
            elif (block.get("extensions", {}).get("slai.sldc.toc") or {}).get("role") == "entry":
                toc = block["extensions"]["slai.sldc.toc"]
                level = max(1, min(6, int(toc.get("level", 1))))
                title_info = toc_titles.get(toc["id"], {})
                style, flags = style_for(block, st_body)
                if block.get("style") is None and level == 1:
                    flags["bold"] = True
                heading_id = toc.get("heading", "")
                number = reference_labels.get(heading_id, "") if title_info.get("show_page_numbers", True) else ""
                if not number:
                    emit(TrackedParagraph(inline(block, flags), style, block["id"]))
                    continue
                width = max(50.0, fw - style.leftIndent)
                page_w = min(max(35.0, len(number) * base * 0.75 + 8), width * 0.15)
                if title_info.get("leader_dots", True):
                    try:
                        estimated_label_w = pdfmetrics.stringWidth(clean(block["text"]), style.fontName, style.fontSize)
                    except Exception:  # noqa: BLE001 - unsupported glyph; actual Paragraph still draws with fallback
                        estimated_label_w = len(block["text"]) * style.fontSize * 0.5
                    text_w = min(width * 0.76, max(width * 0.2, estimated_label_w + 12))
                    dots_w = max(0.0, width - page_w - text_w)
                else:
                    dots_w = 0.0
                    text_w = width - page_w
                left = Paragraph(inline(block, flags), style)
                right_style = ParagraphStyle("toc-number", parent=st_body, alignment=TA_RIGHT,
                                             fontSize=style.fontSize, leading=style.leading, spaceAfter=0)
                cells: list[Any] = [left]
                widths = [text_w]
                if dots_w:
                    dot_width = pdfmetrics.stringWidth(".", body_font, max(7, style.fontSize * 0.8))
                    dots = "." * min(120, max(2, int(dots_w / max(1, dot_width)) - 1))
                    cells.append(Paragraph(dots, ParagraphStyle("toc-dots", parent=st_body, fontSize=max(7, style.fontSize * 0.8),
                                                                 leading=style.leading, spaceAfter=0)))
                    widths.append(dots_w)
                cells.append(Paragraph(markup_text(number, False), right_style))
                widths.append(page_w)
                table = Table([cells], colWidths=widths, hAlign="LEFT")
                table.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0),
                                           ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                           ("TOPPADDING", (0, 0), (-1, -1), 0),
                                           ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                                           ("VALIGN", (0, 0), (-1, -1), "BOTTOM")]))
                table._sldc_block_id = block["id"]
                emit(table)
            elif t == "quote":
                style, flags = style_for(block, st_quote)
                emit(TrackedParagraph(inline(block, {"italic": True, **flags}), style, block["id"]))
            elif t == "code":
                lines = clean(block["text"]).split("\n")
                xml = "<br/>".join(re.sub(r"^( +)", lambda m: "&nbsp;" * len(m.group(1)), markup_text(line, True)) for line in lines)
                emit(TrackedParagraph(xml, st_code, block["id"]))
            elif t == "table":
                if block["text"]:
                    emit(TrackedParagraph(f"<i>{markup_text(block['text'], False)}</i>", ParagraphStyle("tc", parent=st_caption, spaceAfter=3,
                                                                                                        keepWithNext=1), block["id"]))
                flow = table_flowable(block, fw)
                if isinstance(flow, list):
                    for f in flow:
                        f._sldc_block_id = block["id"]
                        emit(f)
                else:
                    flow._sldc_block_id = block["id"]
                    emit(flow)
                emit(Spacer(1, base * 0.6))
            elif t == "figure":
                fig = block["figure"]
                got = asset_bytes(package, fig["asset"])
                if not got:
                    warnings.append(f"Image {fig['asset']} is missing from the package and was skipped")
                    continue
                try:
                    reader = ImageReader(io.BytesIO(got[0]))
                    iw, ih = reader.getSize()
                    target_w = min(float(fig.get("width_mm", fw / MM)) * MM, fw)
                    target_h = target_w * ih / max(iw, 1)
                    max_h = fh - 30
                    if target_h > max_h:
                        target_w, target_h = target_w * max_h / target_h, max_h
                    img = Image(io.BytesIO(got[0]), width=target_w, height=target_h)
                    img._sldc_block_id = block["id"]
                    img.hAlign = {"left": "LEFT", "center": "CENTER", "right": "RIGHT"}[fig.get("align", "center")]
                    emit(img)
                    if block["text"]:
                        emit(TrackedParagraph(markup_text(block["text"], False), st_caption, block["id"]))
                    else:
                        emit(Spacer(1, base * 0.5))
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"Image {fig['asset']} could not be placed: {exc}")
            else:
                if not block["text"].strip():
                    if has_page_break(block):
                        emit(Spacer(1, 1))     # keep an empty page (for example a scanned page without text)
                    continue
                style, flags = style_for(block, st_body)
                emit(TrackedParagraph(inline(block, flags), style, block["id"]))
        if opts.include_notes and (notes.note_numbers or notes.cited):
            story.append(Spacer(1, base))
            emit(Paragraph("<b>Notes</b>", st_head[3]))
            for n, text in notes.footnotes():
                emit(Paragraph(f'<a name="note-{n}"/><super>[{n}]</super> {markup_text(text, False)}', st_cell))
            for i, line in enumerate(notes.citation_lines(), 1):
                emit(Paragraph(f'<a name="cite-{i}"/>{markup_text(line, False)}', st_cell))
            if notes.note_numbers:
                warnings.append("Footnotes are written as numbered endnotes at the end of the document")

    # ---------------------------------------------------------------- header/footer pass
    class DocumasterCanvas(canvasmod.Canvas):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._saved: list[dict[str, Any]] = []

        def showPage(self) -> None:  # noqa: N802 - reportlab API
            self._saved.append(dict(self.__dict__))
            self._startPage()

        def save(self) -> None:
            total = len(self._saved)
            previous: str | None = None
            printed = 0
            printed_labels.clear()
            template_ids.clear()
            for index, saved in enumerate(self._saved):
                tid = saved.get("_dm_template", first_id)
                section = section_info.get(tid, section_info[first_id])["section"] or {}
                numbering = section.get("numbering") or {}
                if tid != previous and numbering.get("start") is not None:
                    printed = numbering["start"] - 1
                previous = tid
                template_ids.append(tid)
                if section.get("role") == "cover" or numbering.get("show") is False or not opts.page_numbers:
                    label = None
                else:
                    printed += 1
                    label = format_number(printed, numbering.get("format", "decimal"))
                printed_labels.append(label)
            for index, saved in enumerate(self._saved):
                tid = template_ids[index]
                self.__dict__.update(saved)
                info = section_info.get(tid, section_info[first_id])
                self._draw_edges(info, index + 1, total, printed_labels[index])
                super().showPage()
            super().save()

        def _draw_edges(self, info: dict[str, Any], physical: int, total: int, label: str | None) -> None:
            section = info["section"]
            s: PageSetup = info["setup"]
            pw, ph = info["page"]
            for edge in ("header", "footer"):
                template = (section or {}).get(edge)
                if template is None and edge == "footer" and opts.page_numbers and section is None:
                    template = {"center": "{page}"}
                if not template:
                    continue
                texts = []
                for pos in ("left", "center", "right"):
                    raw = template.get(pos, "")
                    raw = raw.replace("{{", "\x01").replace("}}", "\x02").replace("{page}", label or "").replace("{pages}", str(total)) \
                        .replace("{title}", title).replace("\x01", "{").replace("\x02", "}")
                    texts.append(raw)
                y = ph - (s.top * MM) / 2 - 3 if edge == "header" else (s.bottom * MM) / 2 - 3
                self.setFont(body_font, 8.5)
                self.setFillColor(colors.HexColor("#555555"))
                for pos, text in zip(("left", "center", "right"), texts):
                    if not text:
                        continue
                    draw = "".join(ch if (ord(ch) in cover_body or ch.isspace()) else "?" for ch in text)
                    if pos == "left":
                        self.drawString(s.left * MM, y, draw)
                    elif pos == "center":
                        self.drawCentredString(pw / 2, y, draw)
                    else:
                        self.drawRightString(pw - s.right * MM, y, draw)

    # ---------------------------------------------------------------- build
    buffer = io.BytesIO()
    authors = (doc.get("publication") or {}).get("authors") or ([opts.author] if opts.author else [])
    keywords = (doc.get("publication") or {}).get("keywords") or []

    def run_build() -> int:
        buffer.seek(0)
        buffer.truncate()
        kwargs: dict[str, Any] = dict(pagesize=section_info[first_id]["page"], title=clean(title)[:250],
                                      author=clean("; ".join(authors))[:250], subject=clean((doc.get("publication") or {}).get("subtitle", ""))[:250],
                                      keywords=", ".join(keywords)[:250], creator="SLAI Documaster", producer="SLAI Documaster (reportlab)")
        lang = doc.get("language")
        class LayoutDocTemplate(BaseDocTemplate):
            def afterFlowable(self, flowable: Any) -> None:  # noqa: N802
                block_id = getattr(flowable, "_sldc_block_id", None)
                if block_id:
                    measured_pages.setdefault(block_id, self.page)

        try:
            document = LayoutDocTemplate(buffer, lang=lang if lang and lang != "und" else None, **kwargs)
        except TypeError:
            document = LayoutDocTemplate(buffer, **kwargs)
        document.addPageTemplates(templates)
        build_story()
        document.build(list(story), canvasmaker=DocumasterCanvas)
        return document.page

    # Page references themselves can affect TOC line wrapping. A bounded
    # fixed-point loop always uses the actual ReportLab page placements.
    for attempt in range(5):
        measured_pages.clear()
        try:
            pages = run_build()
        except LayoutError as exc:
            if state["degraded"]:
                raise
            state["degraded"] = True
            warnings.append("A table cell was taller than a page, so tables were written as text rows instead: " + str(exc)[:120])
            measured_pages.clear()
            pages = run_build()
        updated = {block: printed_labels[page - 1] or "" for block, page in measured_pages.items()
                   if 1 <= page <= len(printed_labels)}
        if updated == reference_labels:
            break
        reference_labels = updated
    else:
        warnings.append("TOC pagination did not converge in five passes; inspect the printed page references")
    data = buffer.getvalue()
    dest.write_bytes(data)
    if missing:
        sample = "".join(sorted(missing))[:20]
        warnings.append(f"{len(missing)} character(s) are not in any available font and may appear as boxes: {sample}")
    lost = ["accessibility tags and image alt text", "native footnotes (written as endnotes)", "comments and revision history",
            "editability of the text layout"]
    return WriteResult(dest, "pdf", "reportlab" + (f" + {plan.body.label}" if plan.body else ""), warnings=warnings,
                       kept=["text with embedded fonts", "headings and a PDF outline", "lists", "tables with merged cells and "
                             "repeating header rows", "bold/italic/underline/strike/super/subscript/colour", "hyperlinks and "
                             "internal links", "images", "page size, margins, columns per section",
                             "headers/footers with page numbers", "page breaks", "document properties"],
                       lost=lost, pages=pages,
                       layout={"pages": pages, "blockPages": measured_pages.copy(), "printedLabels": printed_labels.copy(),
                               "sectionIds": template_ids.copy(), "blockLabels": reference_labels.copy()})
