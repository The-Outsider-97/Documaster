"""OpenDocument Text writer (hand-written ODF 1.2 package; SLAI has no ODT writer and no extra dependency is needed).

Keeps headings, paragraphs, nested bullet/numbered lists, tables with merged cells and header rows, quotes, code, inline
formatting, hyperlinks, internal cross-references, real footnotes, inline images with alt text, page size/margins,
headers/footers with page-number fields, per-section page layouts and page breaks.
"""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from ..ir import has_page_break, heading_level, span_segments
from . import WriteOptions, WriteResult
from .common import (Notes, PageSetup, asset_bytes, char_style_table, clean, group_blocks, page_setup,
                     resolve_header_footer, style_table, table_grid, PAGE_SIZES_MM)

NS = (' xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"'
      ' xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0"'
      ' xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"'
      ' xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"'
      ' xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"'
      ' xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0"'
      ' xmlns:xlink="http://www.w3.org/1999/xlink"'
      ' xmlns:dc="http://purl.org/dc/elements/1.1/"'
      ' xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0"'
      ' xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"')
_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif"}


def _q(value: str) -> str:
    return quoteattr(clean(value))


def _e(value: str) -> str:
    return escape(clean(value))


def _mm(value: float) -> str:
    return f"{value:.2f}mm"


def _image_size(data: bytes) -> tuple[int, int]:
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception:  # noqa: BLE001
        return (800, 600)


def write_odt(package: Any, dest: Path, opts: WriteOptions) -> WriteResult:
    doc = package.document
    notes = Notes.of(doc)
    styles_para = style_table(doc)
    styles_char = char_style_table(doc)
    sections = {s["id"]: s for s in doc.get("sections", [])}
    section_order = [s["id"] for s in doc.get("sections", [])]
    title = opts.title or doc["title"]
    warnings: list[str] = []

    # ---- automatic style registries
    auto_para: dict[tuple, str] = {}
    auto_text: dict[tuple, str] = {}
    page_layouts: list[tuple[str, str, PageSetup, dict[str, Any] | None]] = []   # (layout name, master name, setup, section)
    pictures: dict[str, tuple[str, bytes, str]] = {}

    def para_style(parent: str, break_before: bool = False, align: str | None = None, master: str | None = None,
                   extra: tuple = ()) -> str:
        key = (parent, break_before, align, master, extra)
        if key not in auto_para:
            auto_para[key] = f"P{len(auto_para) + 1}"
        return auto_para[key]

    def text_style(flags: dict[str, Any]) -> str | None:
        key = tuple(sorted((k, str(v)) for k, v in flags.items()
                           if k in ("bold", "italic", "underline", "strike", "superscript", "subscript", "code", "color",
                                    "background", "small_caps", "font_family", "font_size_pt") and v))
        if not key:
            return None
        if key not in auto_text:
            auto_text[key] = f"T{len(auto_text) + 1}"
        return auto_text[key]

    def setup_for(section: dict[str, Any] | None) -> PageSetup:
        s = page_setup(section)
        if opts.page_size and not (section and "page" in section) and opts.page_size in PAGE_SIZES_MM:
            s.width_mm, s.height_mm = PAGE_SIZES_MM[opts.page_size]
        if opts.margins_mm is not None and not (section and "page" in section):
            s.top = s.right = s.bottom = s.left = opts.margins_mm
        return s

    # first (default) page layout
    first_model = sections.get(section_order[0]) if section_order else None
    page_layouts.append(("PL1", "Standard", setup_for(first_model), first_model))
    master_for_section: dict[str, str] = {}
    if section_order:
        master_for_section[section_order[0]] = "Standard"

    xref_targets = {span["target"] for b in doc["blocks"] for span in b.get("spans", []) if span["type"] == "xref"}

    def inline(block: dict[str, Any]) -> str:
        parts = []
        for seg, attrs in span_segments(block["text"], block.get("spans")):
            props = dict(attrs)
            if props.get("style") in styles_char:
                props = {**styles_char[props["style"]], **props}
            xml = _text_xml(seg)
            name = text_style(props)
            if name:
                xml = f'<text:span text:style-name="{name}">{xml}</text:span>'
            if props.get("href"):
                xml = f'<text:a xlink:type="simple" xlink:href={_q(props["href"])}>{xml}</text:a>'
            if props.get("xref") and props["xref"] in {b["id"] for b in doc["blocks"]}:
                xml = f'<text:a xlink:type="simple" xlink:href="#{_e(props["xref"])}">{xml}</text:a>'
            if props.get("ref"):
                kind, number = notes.use(props["ref"])
                ref = notes.references.get(props["ref"])
                if kind == "footnote" and ref:
                    xml += (f'<text:note text:id="ftn{number}" text:note-class="footnote"><text:note-citation>{number}'
                            f'</text:note-citation><text:note-body><text:p text:style-name="Footnote">{_e(ref["text"])}'
                            f'</text:p></text:note-body></text:note>')
                elif kind == "citation":
                    xml += f"[{number}]"
            parts.append(xml)
        return "".join(parts)

    def bookmark(block: dict[str, Any]) -> str:
        return f'<text:bookmark text:name={_q(block["id"])}/>' if block["id"] in xref_targets else ""

    body: list[str] = []
    extra_auto: list[str] = []
    current_section = section_order[0] if section_order else None

    def master_break(block: dict[str, Any]) -> tuple[str | None, bool]:
        """Return (master page name, True) when this block starts a new section."""
        nonlocal current_section
        sid = block.get("section")
        if sid and sid != current_section:
            current_section = sid
            model = sections.get(sid)
            name = f"MP{len(page_layouts) + 1}"
            page_layouts.append((f"PL{len(page_layouts) + 1}", name, setup_for(model), model))
            master_for_section[sid] = name
            return name, True
        return None, False

    for kind, item in group_blocks(doc["blocks"]):
        if kind == "list":
            first = item[0]
            master, _ = master_break(first)
            body.append(_odt_list(item, inline, bookmark, para_style, has_page_break, master))
            continue
        block = item
        master, _ = master_break(block)
        t = block["type"]
        attrs = block.get("attributes") or {}
        align = attrs.get("align") or styles_para.get(block.get("style", ""), {}).get("align")
        brk = has_page_break(block)
        if t == "heading":
            lv = heading_level(block)
            style = para_style(f"Heading_20_{lv}", brk, None, master)
            body.append(f'<text:h text:style-name="{style}" text:outline-level="{lv}">{bookmark(block)}{inline(block)}</text:h>')
        elif t == "quote":
            body.append(f'<text:p text:style-name="{para_style("Quotations", brk, None, master)}">{bookmark(block)}{inline(block)}</text:p>')
        elif t == "code":
            lines = clean(block["text"]).split("\n")
            xml = "<text:line-break/>".join(_text_xml(line) for line in lines)
            body.append(f'<text:p text:style-name="{para_style("Preformatted_20_Text", brk, None, master)}">{bookmark(block)}{xml}</text:p>')
        elif t == "table":
            if block["text"]:
                body.append(f'<text:p text:style-name="{para_style("Caption", brk, None, master)}">{_e(block["text"])}</text:p>')
                brk, master = False, None
            body.append(_odt_table(block, setup_for(sections.get(current_section)), para_style(
                "Table_20_Contents", False), para_style("Table_20_Heading", False), brk, master, bookmark(block), _e,
                extra_auto))
        elif t == "figure":
            body.append(_odt_figure(package, block, pictures, setup_for(sections.get(current_section)), para_style, brk, master, warnings))
        else:
            if not block["text"].strip():
                if brk:
                    body.append(f'<text:p text:style-name="{para_style("Standard", True, None, master)}"/>')
                continue
            body.append(f'<text:p text:style-name="{para_style("Standard", brk, align, master)}">{bookmark(block)}{inline(block)}</text:p>')

    if opts.include_notes and notes.cited:
        body.append(f'<text:h text:style-name="Heading_20_2" text:outline-level="2">References</text:h>')
        for line in notes.citation_lines():
            body.append(f'<text:p text:style-name="Standard">{_e(line)}</text:p>')

    # ---- assemble parts
    auto_styles = []
    for (parent, brk, align, master, extra), name in auto_para.items():
        props = ""
        attrs = ""
        if master:
            attrs += f' style:master-page-name="{master}"'
        pp = []
        if brk and not master:
            pp.append('fo:break-before="page"')
        if align:
            pp.append(f'fo:text-align="{ {"left": "start", "right": "end"}.get(align, align) }"')
        if pp:
            props = f"<style:paragraph-properties {' '.join(pp)}/>"
        auto_styles.append(f'<style:style style:name="{name}" style:family="paragraph" style:parent-style-name="{parent}"{attrs}>{props}</style:style>')
    for key, name in auto_text.items():
        flags = dict(key)
        tp = []
        if "bold" in flags:
            tp.append('fo:font-weight="bold"')
        if "italic" in flags:
            tp.append('fo:font-style="italic"')
        if "underline" in flags:
            tp.append('style:text-underline-style="solid" style:text-underline-width="auto" style:text-underline-color="font-color"')
        if "strike" in flags:
            tp.append('style:text-line-through-style="solid"')
        if "superscript" in flags:
            tp.append('style:text-position="super 58%"')
        if "subscript" in flags:
            tp.append('style:text-position="sub 58%"')
        if "small_caps" in flags:
            tp.append('fo:font-variant="small-caps"')
        if "code" in flags:
            tp.append('style:font-name="Liberation Mono" fo:font-family="\'Liberation Mono\', \'Courier New\', monospace"')
        if "font_family" in flags and "code" not in flags:
            tp.append(f'fo:font-family={_q(flags["font_family"])}')
        if "font_size_pt" in flags:
            tp.append(f'fo:font-size="{flags["font_size_pt"]}pt"')
        if "color" in flags:
            tp.append(f'fo:color="{flags["color"]}"')
        if "background" in flags:
            tp.append(f'fo:background-color="{flags["background"]}"')
        auto_styles.append(f'<style:style style:name="{name}" style:family="text"><style:text-properties {" ".join(tp)}/></style:style>')
    auto_styles.extend(extra_auto)
    auto_styles.append('<style:style style:name="Tbl" style:family="table"><style:table-properties style:width="100%" table:align="margins"/></style:style>')
    auto_styles.append('<style:style style:name="TC" style:family="table-cell"><style:table-cell-properties fo:padding="1.2mm" fo:border="0.5pt solid #808080"/></style:style>')
    auto_styles.append('<text:list-style style:name="LB">' + "".join(
        f'<text:list-level-style-bullet text:level="{i}" text:bullet-char="{"•" if i % 2 else "–"}">'
        f'<style:list-level-properties text:list-level-position-and-space-mode="label-alignment">'
        f'<style:list-level-label-alignment text:label-followed-by="listtab" text:list-tab-stop-position="{6.35 * i:.2f}mm" '
        f'fo:text-indent="-5mm" fo:margin-left="{6.35 * i:.2f}mm"/></style:list-level-properties></text:list-level-style-bullet>'
        for i in range(1, 11)) + "</text:list-style>")
    auto_styles.append('<text:list-style style:name="LN">' + "".join(
        f'<text:list-level-style-number text:level="{i}" style:num-suffix="." style:num-format="{"1" if i % 2 else "a"}">'
        f'<style:list-level-properties text:list-level-position-and-space-mode="label-alignment">'
        f'<style:list-level-label-alignment text:label-followed-by="listtab" text:list-tab-stop-position="{6.35 * i:.2f}mm" '
        f'fo:text-indent="-6mm" fo:margin-left="{6.35 * i:.2f}mm"/></style:list-level-properties></text:list-level-style-number>'
        for i in range(1, 11)) + "</text:list-style>")
    auto_styles.append('<style:style style:name="Fr" style:family="graphic"><style:graphic-properties style:wrap="none" '
                       'style:vertical-pos="top" style:vertical-rel="baseline"/></style:style>')

    content = (f'<?xml version="1.0" encoding="UTF-8"?>\n<office:document-content{NS} office:version="1.2">'
               f'<office:automatic-styles>{"".join(auto_styles)}</office:automatic-styles>'
               f'<office:body><office:text>{"".join(body)}</office:text></office:body></office:document-content>')

    styles_xml = _styles_xml(page_layouts, title, doc, opts)
    lang = doc.get("language", "und")
    meta = (f'<?xml version="1.0" encoding="UTF-8"?>\n<office:document-meta{NS} office:version="1.2"><office:meta>'
            f'<dc:title>{_e(title)}</dc:title>'
            + (f'<dc:creator>{_e("; ".join((doc.get("publication") or {}).get("authors", [])))}</dc:creator>'
               if (doc.get("publication") or {}).get("authors") else "")
            + (f'<dc:language>{_e(lang)}</dc:language>' if lang != "und" else "")
            + '<meta:generator>SLAI Documaster</meta:generator></office:meta></office:document-meta>')
    manifest_entries = ['<manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.text"/>']
    for name in ("content.xml", "styles.xml", "meta.xml"):
        manifest_entries.append(f'<manifest:file-entry manifest:full-path="{name}" manifest:media-type="text/xml"/>')
    for path, (name, data, media) in pictures.items():
        manifest_entries.append(f'<manifest:file-entry manifest:full-path="{path}" manifest:media-type="{media}"/>')
    manifest = ('<?xml version="1.0" encoding="UTF-8"?>\n<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:'
                'xmlns:manifest:1.0" manifest:version="1.2">' + "".join(manifest_entries) + "</manifest:manifest>")
    with zipfile.ZipFile(dest, "w") as z:
        info = zipfile.ZipInfo("mimetype", date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_STORED
        z.writestr(info, "application/vnd.oasis.opendocument.text")
        for name, text in (("content.xml", content), ("styles.xml", styles_xml), ("meta.xml", meta),
                           ("META-INF/manifest.xml", manifest)):
            zi = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, text.encode("utf-8"))
        for path, (name, data, media) in pictures.items():
            zi = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_STORED
            z.writestr(zi, data)
    return WriteResult(dest, "odt", "Documaster ODF writer", warnings=warnings,
                       kept=["headings", "nested lists", "tables with merged cells and header rows", "quotes", "code",
                             "bold/italic/underline/strike/super/subscript/colour", "hyperlinks and cross-references",
                             "native footnotes", "images with alt text", "page size, margins, headers/footers with page numbers",
                             "page breaks", "document title, author, language"],
                       lost=["columns (written as a single column)", "comments and revision history", "signatures"])


_TOKENS = re.compile(r"(\t)|( +)|([^\t ]+)")


def _text_xml(text: str) -> str:
    """Escape text for ODF: tabs, line breaks and runs of spaces must be explicit elements."""
    out: list[str] = []
    for i, line in enumerate(clean(text).split("\n")):
        if i:
            out.append("<text:line-break/>")
        for m in _TOKENS.finditer(line):
            if m.group(1):
                out.append("<text:tab/>")
            elif m.group(2):
                n = len(m.group(2))
                if m.start() == 0:
                    out.append(f'<text:s text:c="{n}"/>')
                elif n == 1:
                    out.append(" ")
                else:
                    out.append(f' <text:s text:c="{n - 1}"/>')
            else:
                out.append(escape(m.group(3)))
    return "".join(out)


def _odt_list(items: list[dict[str, Any]], inline: Any, bookmark: Any, para_style: Any, has_break: Any,
              master: str | None) -> str:
    out: list[str] = []
    stack: list[str] = []
    first = True
    for block in items:
        attrs = block.get("attributes") or {}
        want = min(int(attrs.get("depth", 0)), len(stack))
        ordered = bool(attrs.get("ordered"))
        style = "LN" if ordered else "LB"
        while len(stack) > want + 1:
            out.append("</text:list-item></text:list>")
            stack.pop()
        if len(stack) == want + 1:
            if stack[-1] != style:
                out.append("</text:list-item></text:list>")
                stack.pop()
                out.append(f'<text:list text:style-name="{style}">')
                stack.append(style)
            else:
                out.append("</text:list-item>")
        else:
            out.append(f'<text:list text:style-name="{style}">')
            stack.append(style)
        pstyle = para_style("List_20_Paragraph" if False else "Standard", has_break(block) or False, None,
                            master if first else None)
        first = False
        out.append(f'<text:list-item><text:p text:style-name="{pstyle}">{bookmark(block)}{inline(block)}</text:p>')
    while stack:
        out.append("</text:list-item></text:list>")
        stack.pop()
    return "".join(out)


def _odt_table(block: dict[str, Any], setup: PageSetup, cell_style: str, head_style: str, brk: bool,
               master: str | None, bm: str, e: Any, extra_auto: list[str]) -> str:
    grid = table_grid(block["table"])
    width = max(20.0, setup.width_mm - setup.left - setup.right)
    col_w = width / grid.columns
    name = "Table_" + "".join(ch for ch in block["id"] if ch.isalnum())[:40]
    table_style = "Tbl"
    if brk or master:
        table_style = f"Tbl_{len(extra_auto) + 1}"
        master_attr = f' style:master-page-name="{master}"' if master else ""
        break_attr = "" if master else ' fo:break-before="page"'
        extra_auto.append(f'<style:style style:name="{table_style}" style:family="table"{master_attr}><style:table-properties '
                          f'style:width="100%" table:align="margins"{break_attr}/></style:style>')
    cols = f'<table:table-column table:number-columns-repeated="{grid.columns}"/>'
    rows_xml = []
    for r, row in enumerate(grid.rows):
        cells = []
        for cell in row:
            if cell is None:
                cells.append("<table:covered-table-cell/>")
                continue
            attrs = ' table:style-name="TC" office:value-type="string"'
            if cell.get("colspan", 1) > 1:
                attrs += f' table:number-columns-spanned="{cell["colspan"]}"'
            if cell.get("rowspan", 1) > 1:
                attrs += f' table:number-rows-spanned="{cell["rowspan"]}"'
            style = head_style if r < grid.header_rows else cell_style
            paragraphs = "".join(f'<text:p text:style-name="{style}">{_text_xml(line)}</text:p>'
                                 for line in (cell["text"].split("\n") if cell["text"] else [""]))
            cells.append(f"<table:table-cell{attrs}>{paragraphs}</table:table-cell>")
        rows_xml.append(f"<table:table-row>{''.join(cells)}</table:table-row>")
    head = "".join(rows_xml[:grid.header_rows])
    tail = "".join(rows_xml[grid.header_rows:])
    return (f'<table:table table:name="{name}" table:style-name="{table_style}"><table:table-column table:number-columns-repeated="{grid.columns}"/>'
            + (f"<table:table-header-rows>{head}</table:table-header-rows>" if head else "") + tail + "</table:table>")


def _odt_figure(package: Any, block: dict[str, Any], pictures: dict[str, tuple[str, bytes, str]], setup: PageSetup,
                para_style: Any, brk: bool, master: str | None, warnings: list[str]) -> str:
    fig = block["figure"]
    got = asset_bytes(package, fig["asset"])
    if not got:
        warnings.append(f"Image {fig['asset']} is missing from the package and was skipped")
        return ""
    data, media = got
    ext = _EXT.get(media)
    if not ext:
        warnings.append(f"Image {fig['asset']} ({media}) is not supported inside ODT and was skipped")
        return ""
    path = f"Pictures/{fig['asset']}.{ext}"
    pictures[path] = (fig["asset"], data, media)
    px_w, px_h = _image_size(data)
    max_w = max(20.0, setup.width_mm - setup.left - setup.right)
    width = min(float(fig.get("width_mm", max_w)), max_w)
    height = width * px_h / max(px_w, 1)
    align = fig.get("align", "center")
    desc = f"<svg:desc>{_e(fig['alt'])}</svg:desc>" if fig.get("alt") else ""
    frame = (f'<draw:frame draw:style-name="Fr" draw:name={_q(block["id"])} text:anchor-type="as-char" svg:width="{_mm(width)}" '
             f'svg:height="{_mm(height)}"><draw:image xlink:href="{path}" xlink:type="simple" xlink:show="embed" '
             f'xlink:actuate="onLoad"/>{desc}</draw:frame>')
    out = f'<text:p text:style-name="{para_style("Standard", brk, align, master)}">{frame}</text:p>'
    if block["text"]:
        out += f'<text:p text:style-name="{para_style("Caption", False, "center")}">{_e(block["text"])}</text:p>'
    return out


def _styles_xml(layouts: list[tuple[str, str, PageSetup, dict[str, Any] | None]], title: str, doc: dict[str, Any],
                opts: WriteOptions) -> str:
    base_pt = opts.font_size_pt
    font = opts.font_family or "Liberation Sans"
    styles = [
        f'<style:style style:name="Standard" style:family="paragraph" style:class="text"><style:paragraph-properties fo:margin-bottom="2.5mm" fo:line-height="120%"/><style:text-properties fo:font-size="{base_pt}pt" fo:font-family={_q(font)}/></style:style>',
        '<style:style style:name="Table_20_Contents" style:display-name="Table Contents" style:family="paragraph" style:parent-style-name="Standard"><style:paragraph-properties fo:margin-bottom="0mm"/></style:style>',
        '<style:style style:name="Table_20_Heading" style:display-name="Table Heading" style:family="paragraph" style:parent-style-name="Table_20_Contents"><style:text-properties fo:font-weight="bold"/></style:style>',
        '<style:style style:name="Quotations" style:family="paragraph" style:parent-style-name="Standard"><style:paragraph-properties fo:margin-left="10mm" fo:margin-right="10mm" fo:border-left="1.5pt solid #808080" fo:padding-left="3mm"/><style:text-properties fo:font-style="italic"/></style:style>',
        '<style:style style:name="Preformatted_20_Text" style:display-name="Preformatted Text" style:family="paragraph" style:parent-style-name="Standard"><style:paragraph-properties fo:margin-bottom="3mm" fo:background-color="#f2f2f2" fo:padding="2mm" fo:line-height="100%"/><style:text-properties fo:font-family="\'Liberation Mono\', \'Courier New\', monospace" fo:font-size="9.5pt"/></style:style>',
        '<style:style style:name="Caption" style:family="paragraph" style:parent-style-name="Standard"><style:text-properties fo:font-style="italic" fo:font-size="9.5pt"/></style:style>',
        '<style:style style:name="Footnote" style:family="paragraph" style:parent-style-name="Standard"><style:text-properties fo:font-size="9pt"/></style:style>',
        '<style:style style:name="Header" style:family="paragraph" style:parent-style-name="Standard"><style:text-properties fo:font-size="9pt"/></style:style>',
    ]
    sizes = {1: 20, 2: 16, 3: 14, 4: 12.5, 5: 11.5, 6: 11}
    for level in range(1, 7):
        styles.append(f'<style:style style:name="Heading_20_{level}" style:display-name="Heading {level}" style:family="paragraph" '
                      f'style:parent-style-name="Standard" style:default-outline-level="{level}"><style:paragraph-properties '
                      f'fo:margin-top="{5 if level < 3 else 3}mm" fo:margin-bottom="2mm" fo:keep-with-next="always"/>'
                      f'<style:text-properties fo:font-size="{sizes[level]}pt" fo:font-weight="bold"/></style:style>')
    layout_xml = []
    master_xml = []
    for layout_name, master_name, setup, section in layouts:
        layout_xml.append(
            f'<style:page-layout style:name="{layout_name}"><style:page-layout-properties fo:page-width="{_mm(setup.width_mm)}" '
            f'fo:page-height="{_mm(setup.height_mm)}" style:print-orientation="{"landscape" if setup.width_mm > setup.height_mm else "portrait"}" '
            f'fo:margin-top="{_mm(setup.top)}" fo:margin-bottom="{_mm(setup.bottom)}" fo:margin-left="{_mm(setup.left)}" '
            f'fo:margin-right="{_mm(setup.right)}"/><style:header-style/><style:footer-style/></style:page-layout>')
        content_width = setup.width_mm - setup.left - setup.right
        parts = []
        header_style = f"H_{layout_name}"
        styles.append(f'<style:style style:name="{header_style}" style:family="paragraph" style:parent-style-name="Header">'
                      f'<style:paragraph-properties><style:tab-stops><style:tab-stop style:position="{_mm(content_width / 2)}" style:type="center"/>'
                      f'<style:tab-stop style:position="{_mm(content_width)}" style:type="right"/></style:tab-stops></style:paragraph-properties></style:style>')
        for edge in ("header", "footer"):
            template = (section or {}).get(edge)
            if template is None and edge == "footer" and opts.page_numbers and section is None:
                template = {"center": "{page}"}
            if not template:
                continue
            parts.append(f"<style:{edge}>" + _hf_paragraph(template, header_style, title) + f"</style:{edge}>")
        master_xml.append(f'<style:master-page style:name="{master_name}" style:page-layout-name="{layout_name}">{"".join(parts)}</style:master-page>')
        if master_name != "Standard":
            pass
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<office:document-styles{NS} office:version="1.2">'
            f'<office:font-face-decls/><office:styles>{"".join(styles)}</office:styles>'
            f'<office:automatic-styles>{"".join(layout_xml)}</office:automatic-styles>'
            f'<office:master-styles>{"".join(master_xml)}</office:master-styles></office:document-styles>')


def _hf_paragraph(template: dict[str, Any], style_name: str, title: str) -> str:
    def fill(text: str) -> str:
        text = text.replace("{{", "\x01").replace("}}", "\x02")
        out = []
        import re
        for token in re.split(r"(\{page\}|\{pages\}|\{title\})", text):
            if token == "{page}":
                out.append("<text:page-number text:select-page=\"current\">1</text:page-number>")
            elif token == "{pages}":
                out.append("<text:page-count>1</text:page-count>")
            elif token == "{title}":
                out.append(_e(title))
            elif token:
                out.append(_e(token.replace("\x01", "{").replace("\x02", "}")))
        return "".join(out)
    return (f'<text:p text:style-name="{style_name}">{fill(template.get("left", ""))}<text:tab/>{fill(template.get("center", ""))}'
            f'<text:tab/>{fill(template.get("right", ""))}</text:p>')
