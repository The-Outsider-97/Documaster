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
from .common import (Notes, PAGE_SIZES_MM, PageSetup, book_of, asset_bytes, char_style_table, clean, column_widths, format_number, group_blocks,
                     list_labels, page_setup, resolve_header_footer, even_page_template, style_table, table_grid, table_look, hex_rgb,
                     border_runs, rule_look)
from .band_shapes import page_items, place as place_band
from .vector_shapes import draw_vector
from .toc_layout import INLINE_GAP_EM, leader_gap_pt, leader_mark, place_entry, plan_entry, resolve_look

MM = 72.0 / 25.4
_registered: dict[str, str] = {}


SLDC_WRITER_SHAPES = True  # Native vector-path rendering (blurred shadows approximated).


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
        from reportlab.platypus.doctemplate import FrameBreak, Indenter, LayoutError
        from reportlab.platypus.flowables import BalancedColumns, KeepInFrame, KeepTogether
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
    # Writer's own look of a table of contents (leader kind, where the page number goes). Optional: a table without it keeps the native look.
    toc_looks = {block["extensions"]["slai.sldc.toc"]["id"]: block["extensions"]["slai.writer.toc_style"] for block in doc["blocks"]
                 if isinstance((block.get("extensions") or {}).get("slai.writer.toc_style"), dict)
                 and isinstance((block["extensions"].get("slai.sldc.toc") or {}).get("id"), str)}
    # Writer's column layouts per section (separator line, flow, continuous). Optional as well.
    column_layouts = (((doc.get("extensions") or {}).get("slai.writer") or {}).get("column_layouts") or {})

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
            if merged.get("font_size_pt"):
                xml = f'<font size="{float(merged["font_size_pt"]):g}">{xml}</font>'
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
        s = page_setup(section, None, book_of(doc, (section or {}).get("id")))
        if opts.page_size and not (section and "page" in section) and opts.page_size in PAGE_SIZES_MM:
            s.width_mm, s.height_mm = PAGE_SIZES_MM[opts.page_size]
        if opts.margins_mm is not None and not (section and "page" in section):
            s.top = s.right = s.bottom = s.left = opts.margins_mm
        s.top, s.bottom = max(s.top, 12.0), max(s.bottom, 12.0)
        return s

    section_info: dict[str, dict[str, Any]] = {}   # template id -> data for the final pass
    templates: list[PageTemplate] = []

    class BookTemplate(PageTemplate):
        """A page template whose frames sit one way on right-hand pages and the mirrored way on left-hand ones."""

        def __init__(self, *args: Any, even_shift: float = 0.0, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._odd_x = [frame._x1 for frame in self.frames]
            self._even_shift = even_shift

        def beforeDrawPage(self, canv: Any, doc: Any) -> None:  # noqa: N802 - reportlab API
            move = self._even_shift if doc.page % 2 == 0 else 0.0
            for frame, x in zip(self.frames, self._odd_x):
                frame._x1 = x + move
            canv._dm_even_shift = move
            super().beforeDrawPage(canv, doc)

    band_state: dict[str, Any] = {"last": None}

    def draw_band_shapes(canvas: Any, section: dict[str, Any] | None, physical: int, first: bool, setup: PageSetup, pw: float, ph: float, layer: str) -> None:
        """The shapes of the headers and footers on one page, behind the text or in front of it, as vector drawing (Writer's own shapes, the same painter as the body's)."""
        sid = (section or {}).get("id")
        for edge in ("header", "footer"):
            items, mirrored = page_items(doc, sid, edge, physical, first)
            for item in items:
                if item.get("layer", "front") != layer:
                    continue
                at = place_band(item, edge, pw / MM, ph / MM, setup.left_for(physical), setup.right_for(physical), mirrored)
                canvas.saveState()
                canvas.translate(at["x"] * MM, ph - (at["y"] + at["h"]) * MM)
                try:
                    draw_vector(canvas, {**item["shape"], "width_mm": at["w"], "height_mm": at["h"]}, at["w"] * MM, at["h"] * MM)
                finally:
                    canvas.restoreState()

    def make_template(tid: str, section: dict[str, Any] | None) -> PageSetup:
        s = setup_for(section)
        pw, ph = s.width_mm * MM, s.height_mm * MM
        usable_w = pw - (s.left + s.right) * MM
        gap = s.column_gap_mm * MM
        cols = max(1, s.columns)
        layout = column_layouts.get((section or {}).get("id")) or {}
        unequal = column_widths(layout.get("widths_mm"), s)
        widths = [w * MM for w in unequal] if unequal else [(usable_w - gap * (cols - 1)) / cols] * cols
        xs = [s.left * MM]
        for w in widths[:-1]:
            xs.append(xs[-1] + w + gap)
        frames = [Frame(xs[i], s.bottom * MM, widths[i], ph - (s.top + s.bottom) * MM,
                        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0, id=f"{tid}-c{i}") for i in range(cols)]
        rule = layout.get("rule") if cols > 1 else None
        rule_x = [xs[i] - gap / 2 for i in range(1, cols)]

        def on_page(canvas: Any, document: Any, _tid: str = tid, _rule: Any = rule, _x: Any = rule_x, _b: float = s.bottom * MM,
                    _h: float = ph - (s.top + s.bottom) * MM) -> None:
            canvas._dm_template = _tid
            if document.page <= 1:
                band_state["last"] = None
            draw_band_shapes(canvas, section, document.page, band_state["last"] != _tid, s, pw, ph, "behind")      # behind the text: drawn before it
            band_state["last"] = _tid
            if isinstance(_rule, dict):                          # the line between the columns of a page-wide column layout
                canvas.saveState()
                canvas.setStrokeColor(colors.HexColor(_rule.get("color", "#000000")))
                canvas.setLineWidth(float(_rule.get("width_pt", 0.75)))
                for x in _x:
                    shifted = x + getattr(canvas, "_dm_even_shift", 0.0)
                    canvas.line(shifted, _b, shifted, _b + _h)
                canvas.restoreState()

        # Mirrored margins: a left-hand page (every even one, by its position in the document) has the margins the other way round. Its frames move by the
        # difference; the text width is the same on both sides, so nothing reflows when a page changes side.
        shift = (s.right - s.left) * MM if s.mirror else 0.0
        templates.append(BookTemplate(id=tid, frames=frames, onPage=on_page, pagesize=(pw, ph), even_shift=shift))
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
    state = {"emitted": False, "outline_level": 0, "degraded": False, "regions_off": False}
    current_section = first_id

    def emit(flowable: Any) -> None:
        story.append(flowable)
        state["emitted"] = True

    def page_break() -> None:
        if state["emitted"]:
            story.append(PageBreak())
            state["emitted"] = False

    class RuleLine(Flowable):
        """A horizontal rule: one drawn line (two for the double style) from the left to the right edge of the frame or column it sits in."""

        def __init__(self, look: dict[str, Any], block_id: str) -> None:
            super().__init__()
            self.look = look
            self._sldc_block_id = block_id
            self.spaceBefore = look["space_before_pt"]
            self.spaceAfter = look["space_after_pt"]

        def wrap(self, availWidth: float, availHeight: float) -> tuple[float, float]:  # noqa: N802
            self.width = availWidth
            self.height = self.look["width_pt"] * (3 if self.look["style"] == "double" else 1)
            return availWidth, self.height

        def draw(self) -> None:
            c, look = self.canv, self.look
            w = look["width_pt"]
            r, g, b = hex_rgb(look["color"]) or (0.25, 0.25, 0.25)
            c.saveState()
            c.setStrokeColorRGB(r, g, b)
            c.setLineWidth(w)
            c.setLineCap(1 if look["style"] == "dotted" else 0)
            if look["style"] == "dashed":
                c.setDash(w * 4, w * 3)
            elif look["style"] == "dotted":
                c.setDash(0, w * 2)
            ys = (w * 0.5, w * 2.5) if look["style"] == "double" else (w * 0.5,)
            for y in ys:
                c.line(0, y, self.width, y)
            c.restoreState()

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

    class LeaderRule(Flowable):
        """A thin rule on the baseline: the "solid" leader of a table of contents."""

        def __init__(self, width: float, height: float, size: float) -> None:
            super().__init__()
            self.width, self.height, self.size = max(2.0, width), height, size

        def wrap(self, *_: Any) -> tuple[float, float]:
            return self.width, self.height

        def draw(self) -> None:
            self.canv.setLineWidth(0.5)
            self.canv.setStrokeColor(colors.HexColor("#666666"))
            self.canv.line(0, self.size * 0.22, self.width, self.size * 0.22)

    class TocEntry(Flowable):
        """One entry of a table of contents: its text (which may wrap), the leader after it and the page number, placed by ``toc_layout`` from the real widths
        of the text, the number and the leader's marks. The number sits on the last line of the text (on the first when it comes before it)."""

        def __init__(self, xml: str, number_xml: str, number: str, style: Any, look: dict[str, Any], column_w: float, block_id: str) -> None:
            super().__init__()
            self.xml, self.number_xml, self.number, self.style, self.look, self.column_w = xml, number_xml, number, style, look, column_w
            self._sldc_block_id = block_id
            self.spaceBefore, self.spaceAfter = style.spaceBefore, style.spaceAfter
            self.keepWithNext = getattr(style, "keepWithNext", 0)
            self.hAlign = "LEFT"
            self._ready: tuple[float, float] | None = None

        def wrap(self, availWidth: float, availHeight: float) -> tuple[float, float]:  # noqa: N802
            st, look = self.style, self.look
            plain = ParagraphStyle("toc-measure", parent=st, leftIndent=0, rightIndent=0, firstLineIndent=0, alignment=TA_LEFT, spaceBefore=0, spaceAfter=0)
            self.number_w = text_width(self.number_xml, plain)
            self.mark = leader_mark(look)
            self.mark_w = text_width(markup_text(self.mark, False), plain) if self.mark else 0.0
            self.gap = leader_gap_pt(look, st.fontSize)
            inline_gap = INLINE_GAP_EM * st.fontSize
            self.plan = plan_entry(look, availWidth, st.leftIndent, st.rightIndent, max(self.column_w, self.number_w), self.number_w, inline_gap)
            x0, x1 = self.plan["text"]
            self.para = Paragraph(self.xml, ParagraphStyle("toc-text", parent=st, leftIndent=0, rightIndent=0, firstLineIndent=0, spaceBefore=0, spaceAfter=0))
            _, height = self.para.wrap(x1 - x0, availHeight)
            widths = self.para.getActualLineWidths0() or [0.0]
            room = x1 - x0
            align = st.alignment

            def extent(index: int) -> tuple[float, float]:
                width = widths[index]
                if align == TA_JUSTIFY and index < len(widths) - 1:
                    return x0, x1
                shift = (room - width) / 2 if align == TA_CENTER else room - width if align == TA_RIGHT else 0.0
                return x0 + shift, x0 + shift + width
            self.lines = len(widths)
            self.placed = place_entry(look, self.plan, self.number_w, extent(0), extent(len(widths) - 1), self.mark_w, self.gap, inline_gap)
            self.width, self.height = availWidth, height
            return availWidth, height

        def split(self, availWidth: float, availHeight: float) -> list[Any]:  # noqa: N802
            return []

        def draw(self) -> None:
            canv, st, placed = self.canv, self.style, self.placed
            self.para.drawOn(canv, self.plan["text"][0], 0)
            if placed["number_x"] is None:
                return
            leading = st.leading
            first_line = self.look["position"] == "left"
            baseline = self.height - first_baseline(self.para) - (0 if first_line else (self.lines - 1) * leading)
            number = Paragraph(self.number_xml, ParagraphStyle("toc-number", parent=st, leftIndent=0, rightIndent=0, firstLineIndent=0, alignment=TA_LEFT,
                                                               spaceBefore=0, spaceAfter=0))
            number.wrap(self.number_w + 4, leading * 2)
            number.drawOn(canv, placed["number_x"], baseline + first_baseline(number) - number.height)
            leader = placed["leader"]
            if not leader:
                return
            canv.saveState()
            canv.setFillColor(st.textColor)
            canv.setStrokeColor(st.textColor)
            if leader["kind"] == "solid":
                canv.setLineWidth(max(0.4, st.fontSize * 0.045))
                canv.line(leader["x0"], baseline - st.fontSize * 0.1, leader["x1"], baseline - st.fontSize * 0.1)
            else:
                canv.setFont(body_font, st.fontSize)
                for i in range(leader["count"]):
                    canv.drawString(leader["x0"] + i * leader["pitch"], baseline, self.mark)
            canv.restoreState()

    def first_baseline(paragraph: Any) -> float:
        """How far the first baseline of a wrapped paragraph lies below its top (ReportLab's own rule, which depends on its configuration)."""
        from reportlab import rl_config
        lines = paragraph.blPara
        return float(lines.fontSize) if getattr(rl_config, "paraFontSizeHeightOffset", 0) else float(getattr(lines, "ascent", lines.fontSize))

    def text_width(xml: str, style: Any) -> float:
        probe = Paragraph(xml, style)
        probe.wrap(100000, 100000)
        return float((probe.getActualLineWidths0() or [0.0])[0])

    def flagged(xml: str, flags: dict[str, Any]) -> str:
        for flag, tag in (("bold", "b"), ("italic", "i"), ("underline", "u"), ("strike", "strike")):
            if flags.get(flag):
                xml = f"<{tag}>{xml}</{tag}>"
        if flags.get("color"):
            xml = f'<font color="{flags["color"]}">{xml}</font>'
        return xml

    column_cache: dict[tuple[str, int], float] = {}

    def column_for(toc_id: str, look: dict[str, Any]) -> float:
        """The width of the page-number column of a table of contents: what the person set, or the widest page number of its entries (so the numbers line up)."""
        if look["number_column_pt"]:
            return float(look["number_column_pt"])
        key = (toc_id, id(reference_labels))
        if key not in column_cache:
            widest = 0.0
            for other in doc["blocks"]:
                meta = (other.get("extensions") or {}).get("slai.sldc.toc") or {}
                if meta.get("role") != "entry" or meta.get("id") != toc_id or not reference_labels.get(meta.get("heading", "")):
                    continue
                style, flags = style_for(other, st_body)
                if other.get("style") is None and int(meta.get("level", 1)) == 1:
                    flags["bold"] = True
                plain = ParagraphStyle("toc-measure", parent=style, leftIndent=0, rightIndent=0, firstLineIndent=0, alignment=TA_LEFT, spaceBefore=0, spaceAfter=0)
                widest = max(widest, text_width(flagged(markup_text(reference_labels[meta["heading"]], False), flags), plain))
            column_cache[key] = widest
        return column_cache[key]

    class TrackedParagraph(Paragraph):
        """Keep the originating SLDC block ID even when ReportLab splits a paragraph."""

        def __init__(self, xml: str, style: Any, block_id: str = "", **kwargs: Any) -> None:
            super().__init__(xml, style, **kwargs)
            self._sldc_block_id = block_id

        def split(self, availWidth: float, availHeight: float) -> list[Any]:  # noqa: N802
            pieces = super().split(availWidth, availHeight)
            for piece in pieces:
                piece._sldc_block_id = self._sldc_block_id
            return pieces

    def warn_once(text: str) -> None:
        if text not in warnings:
            warnings.append(text)

    def flush_region() -> None:
        """Close a run of blocks that sits in its own columns in the middle of a page: ReportLab balances it across the columns."""
        sid = state.get("region")
        if sid is None:
            return
        state["region"] = None
        start = state["region_start"]
        items = story[start:]
        del story[start:]
        if not items:
            return
        s = section_info[sid]["setup"]
        gap = s.column_gap_mm * MM
        rule = (column_layouts.get(sid) or {}).get("rule")
        extra: dict[str, Any] = {}
        if isinstance(rule, dict):
            extra = {"vLinesStrokeColor": colors.HexColor(rule.get("color", "#000000")), "vLinesStrokeWidth": float(rule.get("width_pt", 0.75))}
        # BalancedColumns pads the outer columns by half a gap; widening the frame by that much keeps the text flush with the margins.
        story.append(Indenter(-gap / 2, -gap / 2))
        story.append(BalancedColumns(items, nCols=s.columns, needed=2 * s.columns * base, innerPadding=gap, leftPadding=gap / 2, rightPadding=gap / 2,
                                     topPadding=0, bottomPadding=0, **extra))
        story.append(Indenter(gap / 2, gap / 2))
        state["emitted"] = True

    def same_sheet(a: PageSetup, b: PageSetup) -> bool:
        return (a.width_mm, a.height_mm, a.top, a.right, a.bottom, a.left, a.mirror, a.gutter, a.gutter_position) == \
            (b.width_mm, b.height_mm, b.top, b.right, b.bottom, b.left, b.mirror, b.gutter, b.gutter_position)

    def start_block(block: dict[str, Any]) -> None:
        nonlocal current_section
        sid = block.get("section")
        if sid and sid != current_section:
            flush_region()
            if sid not in made:
                make_template(sid, sections.get(sid))
                made.add(sid)
            kind = None
            if (column_layouts.get(sid) or {}).get("continuous") is True and not state["regions_off"]:
                here, there = section_info[state["base"]]["setup"], section_info[sid]["setup"]
                if same_sheet(here, there):
                    kind = "same" if there.columns == here.columns else "region" if here.columns == 1 and there.columns > 1 else None
                if kind is None and state["emitted"]:
                    warn_once("Columns that change in the middle of a page only keep the page when the page itself has one column; "
                              "the PDF starts a new page for them")
            if kind == "region":
                if column_widths((column_layouts.get(sid) or {}).get("widths_mm"), section_info[sid]["setup"]):
                    warn_once("Unequal column widths of a selection that shares a page are written as equal columns in the PDF")
                state.update(region=sid, region_start=len(story))
                current_section = sid
            elif kind == "same":
                story.append(NextPageTemplate(sid))
                state["base"] = sid
                current_section = sid
            else:
                story.append(NextPageTemplate(sid))
                page_break()
                current_section = sid
                state["emitted"] = False
                state["base"] = sid
        elif has_page_break(block) and state.get("region") is None:
            page_break()
        if (block.get("extensions") or {}).get("slai.writer.column_break"):
            if state.get("region") is not None:
                warn_once("A column break inside columns that share a page with other text is not written to the PDF (the columns are balanced instead)")
            elif section_info[current_section]["setup"].columns > 1:
                if state["emitted"]:
                    story.append(FrameBreak())
                    state["emitted"] = False
            else:
                page_break()
        if block["id"] in xref_ids:
            story.append(Anchor(block["id"]))

    def frame_width(section_id: str) -> float:
        s = section_info[section_id]["setup"]
        usable = (s.width_mm - s.left - s.right) * MM
        cols = max(1, s.columns)
        unequal = column_widths((column_layouts.get((section_info[section_id]["section"] or {}).get("id")) or {}).get("widths_mm"), s)
        if unequal and state.get("region") != section_id:           # a balanced region (ReportLab) has equal columns
            return min(unequal) * MM
        width = (usable - s.column_gap_mm * MM * (cols - 1)) / cols
        # ReportLab refuses a table or picture that is even a rounding error wider than a column of a balanced region
        return width - 0.5 if state.get("region") == section_id else width

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
        look = table_look(block, grid)
        cells_look = (look or {}).get("cells", {})
        pad_default = (float(look["cell_padding_mm"]) * MM if look and look.get("cell_padding_mm") is not None else None)
        data: list[list[Any]] = []
        weights = [6.0] * grid.columns
        for r, row in enumerate(grid.rows):
            line = []
            for c, cell in enumerate(row):
                if cell is None:
                    line.append("")
                    continue
                text = cell["text"]
                mine = cells_look.get(f"{r},{c}", {})
                style = st_cell
                ink = hex_rgb(mine.get("color"))
                if cell.get("align") or ink:
                    style = ParagraphStyle("cellx", parent=st_cell, **({"alignment": ALIGN[cell["align"]]} if cell.get("align") else {}),
                                           **({"textColor": colors.Color(*ink)} if ink else {}))
                xml = inline({"text": text, "spans": cell.get("spans", [])})
                if (r < grid.header_rows if look is None else mine.get("bold")) or cell.get("header") and look is None:
                    xml = f"<b>{xml}</b>"
                line.append(Paragraph(xml, style))
                if cell.get("colspan", 1) == 1:
                    longest = max([len(w) for w in text.split()] + [1])
                    weights[c] = max(weights[c], min(max(len(text) ** 0.7, longest), 40))
            data.append(line)
        # ---- column widths: the ones the person set; otherwise as the editor lays a table out (equal columns), or by content when "fit to content"
        layout_widths = (look or {}).get("column_widths_mm")
        fit = (look or {}).get("fit", "auto")
        if isinstance(layout_widths, list) and len(layout_widths) == grid.columns:
            widths = [float(w) * MM for w in layout_widths]
            goal = width if fit == "window" else min(sum(widths), width)
        elif look is not None and fit != "content":
            goal = float(look["width_mm"]) * MM if look.get("width_mm") and fit != "window" else width
            goal = min(goal, width)
            widths = [goal / grid.columns] * grid.columns
        else:
            total = sum(weights)
            widths = [max(width * w / total, 14.0) for w in weights]
            goal = width
        widths = [w * goal / sum(widths) for w in widths]
        indent = float((look or {}).get("indent_mm") or 0) * MM
        align = (look or {}).get("align")
        cmds: list[tuple] = [("VALIGN", (0, 0), (-1, -1), "TOP")]
        pad = pad_default if pad_default is not None else None
        cmds += [("LEFTPADDING", (0, 0), (-1, -1), pad if pad is not None else 4), ("RIGHTPADDING", (0, 0), (-1, -1), pad if pad is not None else 4),
                 ("TOPPADDING", (0, 0), (-1, -1), pad if pad is not None else 3), ("BOTTOMPADDING", (0, 0), (-1, -1), pad if pad is not None else 3)]
        if look is None:                                        # no resolved look (a caller that is not Writer): the plain grid of before
            cmds.insert(0, ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#9aa0a6")))
            if grid.header_rows:
                cmds.append(("BACKGROUND", (0, 0), (-1, grid.header_rows - 1), colors.HexColor("#eceff3")))
        else:
            for r, row in enumerate(grid.rows):
                for c, cell in enumerate(row):
                    if cell is None:
                        continue
                    mine = cells_look.get(f"{r},{c}", {})
                    end = (c + cell.get("colspan", 1) - 1, r + cell.get("rowspan", 1) - 1)
                    fill = hex_rgb(mine.get("fill"))
                    if fill:
                        cmds.append(("BACKGROUND", (c, r), end, colors.Color(*fill)))
                    if mine.get("valign"):
                        cmds.append(("VALIGN", (c, r), end, {"top": "TOP", "middle": "MIDDLE", "bottom": "BOTTOM"}[mine["valign"]]))
                    if mine.get("padding_mm") is not None:
                        p_ = float(mine["padding_mm"]) * MM
                        cmds += [("LEFTPADDING", (c, r), end, p_), ("RIGHTPADDING", (c, r), end, p_), ("TOPPADDING", (c, r), end, p_), ("BOTTOMPADDING", (c, r), end, p_)]
            for axis, line, first, last, spec in border_runs(look, len(grid.rows), grid.columns):
                rgb = hex_rgb(spec.get("color")) or (0, 0, 0)
                w = float(spec["width_pt"])
                style = spec.get("style", "solid")
                dash = None if style in ("solid", "double") else ((w * 4, w * 2.5) if style == "dashed" else (w, w * 2.2))
                count, weight, space = (2, max(w / 3, 0.2), w / 3) if style == "double" and w >= 3 else (1, w, 0)
                common = (weight, colors.Color(*rgb), 1 if style == "dotted" else 0, dash, 0, count, space)
                if axis == "h":
                    if line < len(grid.rows):
                        cmds.append(("LINEABOVE", (first, line), (last, line), *common))
                    else:
                        cmds.append(("LINEBELOW", (first, line - 1), (last, line - 1), *common))
                elif line < grid.columns:
                    cmds.append(("LINEBEFORE", (line, first), (line, last), *common))
                else:
                    cmds.append(("LINEAFTER", (line - 1, first), (line - 1, last), *common))
        for r, row in enumerate(grid.rows):
            for c, cell in enumerate(row):
                if cell is not None and (cell.get("colspan", 1) > 1 or cell.get("rowspan", 1) > 1):
                    cmds.append(("SPAN", (c, r), (c + cell.get("colspan", 1) - 1, r + cell.get("rowspan", 1) - 1)))
        heights = (look or {}).get("row_heights_mm")
        hAlign = {"center": "CENTER", "right": "RIGHT"}.get(align, "LEFT")
        def build(row_heights: Any) -> Any:
            built = Table(data, colWidths=widths, rowHeights=row_heights, repeatRows=grid.header_rows, splitByRow=1, hAlign=hAlign)
            built.setStyle(TableStyle(cmds))
            return built
        table = build(None)
        if isinstance(heights, list) and len(heights) == len(grid.rows):
            table.wrap(goal, 1_000_000)                    # the height every row needs for its text, then the one the person asked for (at least, or exactly)
            exact = look.get("row_height_rule") == "exact"
            table = build([float(h_) * MM if exact else max(float(h_) * MM, natural) for h_, natural in zip(heights, table._rowHeights)])  # noqa: SLF001
        if indent and align not in ("center", "right"):
            table = [Indenter(left=indent), table, Indenter(left=-indent)]
        elif look is not None and look.get("keep_rows_together"):
            table = KeepTogether([table])
        return table

    from .vector_shapes import supported as is_vector_shape, make_flowable

    def build_story() -> None:
        nonlocal current_section
        story.clear()
        state.update(emitted=False, outline_level=0, region=None, region_start=0, base=first_id)
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
            if is_vector_shape(block):
                shape = block["extensions"]["slai.writer.shape"]
                if any(float(s.get("blur_mm",0))>0 for s in shape.get("shadows",[])):
                    note = "Blurred shape shadows are approximated as layered translucent vector paths; visual comparison is recommended."
                    if note not in warnings:
                        warnings.append(note)
                if (shape.get("layout") or {}).get("wrap","none")!="none":
                    note = "Vector shape wrapping around text is not yet supported in PDF; the shape uses its declared position."
                    if note not in warnings:
                        warnings.append(note)
                flow = make_flowable(block,fw,section_info[current_section]["setup"])
                emit(flow)
            elif t == "heading":
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
                number = reference_labels.get(toc.get("heading", ""), "")
                look = resolve_look(toc_looks.get(toc["id"]), show_page_numbers=bool(title_info.get("show_page_numbers", True)),
                                    leader_dots=bool(title_info.get("leader_dots", True)))
                if not number or look["position"] == "hidden":
                    emit(TrackedParagraph(inline(block, flags), style, block["id"]))
                    continue
                emit(TocEntry(inline(block, flags), flagged(markup_text(number, False), flags), number, style, look, column_for(toc["id"], look), block["id"]))
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
            elif rule_look(block) is not None:
                emit(RuleLine(rule_look(block) or {}, block["id"]))
            else:
                if not block["text"].strip():
                    if has_page_break(block):
                        emit(Spacer(1, 1))     # keep an empty page (for example a scanned page without text)
                    continue
                style, flags = style_for(block, st_body)
                emit(TrackedParagraph(inline(block, flags), style, block["id"]))
        flush_region()
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
                draw_band_shapes(self, info["section"], index + 1, index == 0 or template_ids[index - 1] != tid, info["setup"], info["page"][0], info["page"][1], "front")
                super().showPage()
            super().save()

        def _draw_edges(self, info: dict[str, Any], physical: int, total: int, label: str | None) -> None:
            section = info["section"]
            s: PageSetup = info["setup"]
            pw, ph = info["page"]
            left_margin, right_margin = s.left_for(physical), s.right_for(physical)
            left_hand = physical % 2 == 0
            for edge in ("header", "footer"):
                template = (section or {}).get(edge)
                if template is None and edge == "footer" and opts.page_numbers and section is None:
                    template = {"center": "{page}"}
                if left_hand and (s.odd_even or (s.mirror and s.mirror_edges)):
                    template = even_page_template(edge, template, s)          # its own even-page text, or the ordinary one with the edges swapped in a book
                if not template:
                    continue
                def replace_fields(raw: str) -> str:
                    return raw.replace("{{", "\x01").replace("}}", "\x02").replace("{page}", label or "").replace("{pages}", str(total)) \
                        .replace("{title}", title).replace("\x01", "{").replace("\x02", "}")
                y = ph - (s.top * MM) / 2 - 3 if edge == "header" else (s.bottom * MM) / 2 - 3
                for pos in ("left", "center", "right"):
                    from_pos = pos                          # a left-hand page's template already has its edges in place (even_page_template)
                    raw = template.get(from_pos, "")
                    if not raw:
                        continue
                    fallback = (template.get("formats") or {}).get(from_pos, {})
                    rich = (template.get("runs") or {}).get(from_pos) or []
                    fragments = rich if rich and "".join(r["text"] for r in rich) == raw else [{"text": raw}]
                    drawn: list[tuple[str, str, float, str, float]] = []
                    for fragment in fragments:
                        props = {**fallback, **fragment.get("format", {})}
                        weight, italic = bool(props.get("bold")), bool(props.get("italic"))
                        face = (("DMBody-" + ("BI" if weight and italic else "B" if weight else "I" if italic else "R"))
                                if body_font.startswith("DMBody-") else ("Helvetica-BoldOblique" if weight and italic else "Helvetica-Bold" if weight else
                                                   "Helvetica-Oblique" if italic else "Helvetica"))
                        size = max(4.0, min(400.0, float(props.get("font_size_pt") or 8.5)))
                        text = replace_fields(fragment["text"])
                        printable = "".join(ch if (ord(ch) in cover_body or ch.isspace()) else "?" for ch in text)
                        width = pdfmetrics.stringWidth(printable, face, size)
                        drawn.append((printable, face, size, props.get("color", "#555555"), width))
                    total_width = sum(fragment[4] for fragment in drawn)
                    x = left_margin * MM if pos == "left" else pw / 2 - total_width / 2 if pos == "center" else pw - right_margin * MM - total_width
                    for printable, face, size, color, width in drawn:
                        self.setFont(face, size)
                        self.setFillColor(colors.HexColor(color))
                        self.drawString(x, y, printable)
                        x += width

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
                if isinstance(flowable, KeepInFrame):      # one column of a balanced region: the blocks in it are on this page
                    for inner in getattr(flowable, "_content", []) or []:
                        inner_id = getattr(inner, "_sldc_block_id", None)
                        if inner_id:
                            measured_pages.setdefault(inner_id, self.page)

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
            try:
                pages = run_build()
            except ValueError as exc:
                if "BalancedColumns" not in str(exc) or state["regions_off"]:
                    raise
                # a block that cannot be balanced across columns (ReportLab cannot split it): the columns start on a page of their own instead
                state["regions_off"] = True
                warnings.append("Columns that share a page with other text could not be balanced for this content, so each starts a new page in the PDF")
                measured_pages.clear()
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
    lost = ["dashed, dotted or double styles of the line between columns (drawn solid)", "unequal widths and column breaks inside a selection that shares a page with other text",
            "accessibility tags and image alt text", "native footnotes (written as endnotes)", "comments and revision history",
            "editability of the text layout"]
    return WriteResult(dest, "pdf", "reportlab" + (f" + {plan.body.label}" if plan.body else ""), warnings=warnings,
                       kept=["text with embedded fonts", "headings and a PDF outline", "lists", "tables with merged cells and "
                             "repeating header rows", "bold/italic/underline/strike/super/subscript/colour", "hyperlinks and "
                             "internal links", "images", "editable vector path appearance with searchable shape text", "page size, margins, columns (whole-page columns may be unequal; a selection in the middle of a page is balanced across equal columns)", "the line between columns", "column breaks", "table of contents leaders (dots, dashes, rule) and page-number placement",
                             "headers/footers with page numbers", "page breaks", "document properties"],
                       lost=lost, pages=pages,
                       layout={"pages": pages, "blockPages": measured_pages.copy(), "printedLabels": printed_labels.copy(),
                               "sectionIds": template_ids.copy(), "blockLabels": reference_labels.copy()})
