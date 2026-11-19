"""RTF writer (hand-written, no dependency). Keeps headings, bullet/numbered lists (real list table, numbering restarts),
tables with merged cells and repeating header rows, inline formatting, hyperlinks, real footnotes, PNG/JPEG images with alt
text, page size/margins, per-section headers and footers with page-number fields, page breaks and document info."""
from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any

from ..ir import has_page_break, heading_level, span_segments
from . import WriteOptions, WriteResult
from .common import (Notes, PageSetup, book_of, even_page_template, uses_facing_pages, PAGE_SIZES_MM, asset_bytes, char_style_table, clean, column_widths, group_blocks, page_setup,
                     style_table, table_grid, table_look, rule_look)
from .toc_layout import MM_PT, resolve_look, tab_plan, word_leader

TWIP_PER_MM = 56.6929


def tw(mm: float) -> int:
    return int(round(mm * TWIP_PER_MM))


def rtf_text(text: str) -> str:
    out: list[str] = []
    for ch in clean(text):
        code = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == "{":
            out.append("\\{")
        elif ch == "}":
            out.append("\\}")
        elif ch == "\n":
            out.append("\\line ")
        elif ch == "\t":
            out.append("\\tab ")
        elif code < 32:
            continue
        elif code < 128:
            out.append(ch)
        elif code <= 0xFFFF:
            out.append(f"\\u{code - 65536 if code > 32767 else code}?")
        else:
            code -= 0x10000
            hi, lo = 0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)
            out.append(f"\\u{hi - 65536}?\\u{lo - 65536}?")
    return "".join(out)


def write_rtf(package: Any, dest: Path, opts: WriteOptions) -> WriteResult:
    doc = package.document
    notes = Notes.of(doc)
    styles_para = style_table(doc)
    styles_char = char_style_table(doc)
    sections = {s["id"]: s for s in doc.get("sections", [])}
    order = [s["id"] for s in doc.get("sections", [])]
    mirrored_document = any((book_of(doc, sid) or {}).get("mirror") is True for sid in order)
    paired_document = uses_facing_pages(doc, order)                          # facing pages (separate right-hand and left-hand running heads) are document-wide in RTF      # mirrored margins and facing pages are document-wide in RTF
    # Writer's column layouts (separator, unequal widths, flow, continuous). Optional: without them columns are equal and every section starts a page.
    column_layouts = (((doc.get("extensions") or {}).get("slai.writer") or {}).get("column_layouts") or {})
    # The contents: a page-reference field in each entry needs a bookmark on the heading; the cached number is what Writer measured.
    toc_labels = ((doc.get("extensions") or {}).get("slai.writer") or {}).get("toc_page_labels") or {}
    toc_targets = {meta["heading"] for b in doc["blocks"]
                   if (meta := (b.get("extensions") or {}).get("slai.sldc.toc")) and meta.get("role") == "entry" and isinstance(meta.get("heading"), str)
                   } & {b["id"] for b in doc["blocks"] if b["type"] == "heading"}
    toc_title_meta = {meta["id"]: meta for b in doc["blocks"]
                      if (meta := (b.get("extensions") or {}).get("slai.sldc.toc")) and meta.get("role") == "title" and isinstance(meta.get("id"), str)}
    toc_looks = {b["extensions"]["slai.sldc.toc"]["id"]: b["extensions"]["slai.writer.toc_style"] for b in doc["blocks"]
                 if isinstance((b.get("extensions") or {}).get("slai.writer.toc_style"), dict)
                 and isinstance(((b["extensions"].get("slai.sldc.toc")) or {}).get("id"), str)}

    def toc_bookmark(block_id: str) -> str:
        return "_Toc" + re.sub(r"[^A-Za-z0-9]", "_", block_id)[:30]

    title = opts.title or doc["title"]
    warnings: list[str] = []
    colors: list[str] = ["#000000", "#0563C1", "#F2F2F2"]

    def color_index(value: str) -> int:
        value = value.upper()
        if value not in colors:
            colors.append(value)
        return colors.index(value) + 1

    def setup_for(section: dict[str, Any] | None) -> PageSetup:
        s = page_setup(section, None, book_of(doc, (section or {}).get("id")))
        if opts.page_size and not (section and "page" in section) and opts.page_size in PAGE_SIZES_MM:
            s.width_mm, s.height_mm = PAGE_SIZES_MM[opts.page_size]
        if opts.margins_mm is not None and not (section and "page" in section):
            s.top = s.right = s.bottom = s.left = opts.margins_mm
        return s

    def section_props(section: dict[str, Any] | None, previous: PageSetup | None = None) -> tuple[str, PageSetup]:
        s = setup_for(section)
        layout = column_layouts.get((section or {}).get("id")) or {}
        landscape = "\\lndscpsxn" if s.width_mm > s.height_mm else ""
        cols = ""
        if s.columns > 1:
            widths = column_widths(layout.get("widths_mm"), s)
            if widths:
                cols = f"\\cols{s.columns}" + "".join(
                    f"\\colno{i + 1}\\colw{tw(w)}" + (f"\\colsr{tw(s.column_gap_mm)}" if i < s.columns - 1 else "") for i, w in enumerate(widths))
            else:
                cols = f"\\cols{s.columns}\\colsx{tw(s.column_gap_mm)}"
            if layout.get("rule"):
                cols += "\\linebetcol"                           # a thin line; its colour and thickness are not part of RTF
            if layout.get("flow") == "rtl":
                cols += "\\rtlsect"
        same_sheet = previous is not None and (previous.width_mm, previous.height_mm, previous.top, previous.right, previous.bottom, previous.left, previous.mirror) == \
            (s.width_mm, s.height_mm, s.top, s.right, s.bottom, s.left, s.mirror)
        start = "\\sbknone" if layout.get("continuous") is True and same_sheet else "\\sbkpage"
        # Mirrored margins: the left margin is the inside one of a right-hand page and the gutter is part of it (readers other than Word ignore a separate
        # gutter field, and the page must look the same in all of them). \\margmirror is set for the document.
        left_twips, gutter = tw(s.left), ""
        text = (f"\\sectd{start}\\pgwsxn{tw(s.width_mm)}\\pghsxn{tw(s.height_mm)}{landscape}\\marglsxn{left_twips}"
                f"\\margrsxn{tw(s.right)}\\margtsxn{tw(s.top)}\\margbsxn{tw(s.bottom)}{gutter}{cols}")
        return text, s

    def hf_group(kind: str, template: dict[str, Any], width_mm: float) -> str:
        def fill(text: str) -> str:
            text = text.replace("{{", "\x01").replace("}}", "\x02")
            out = []
            for token in re.split(r"(\{page\}|\{pages\}|\{title\})", text):
                if token == "{page}":
                    out.append("{\\field{\\*\\fldinst PAGE}{\\fldrslt 1}}")
                elif token == "{pages}":
                    out.append("{\\field{\\*\\fldinst NUMPAGES}{\\fldrslt 1}}")
                elif token == "{title}":
                    out.append(rtf_text(title))
                elif token:
                    out.append(rtf_text(token.replace("\x01", "{").replace("\x02", "}")))
            return "".join(out)
        return (f"{{\\{kind}\\pard\\plain\\f0\\fs18\\tqc\\tx{tw(width_mm / 2)}\\tqr\\tx{tw(width_mm)} "
                f"{fill(template.get('left', ''))}\\tab {fill(template.get('center', ''))}\\tab {fill(template.get('right', ''))}\\par}}")

    def hf_for(section: dict[str, Any] | None, s: PageSetup) -> str:
        out = ""
        width = s.width_mm - s.left - s.right
        for edge in ("header", "footer"):
            template = (section or {}).get(edge)
            if template is None and edge == "footer" and opts.page_numbers and section is None:
                template = {"center": "{page}"}
            if paired_document:                                       # facing pages: right-hand and left-hand pages each have their own
                even = even_page_template(edge, template, s)
                if template:
                    out += hf_group(edge + "r", template, width)
                if even:
                    out += hf_group(edge + "l", even, width)
            elif template:
                out += hf_group(edge, template, width)
        return out

    # ---- lists
    numbered_runs: list[int] = []      # one list definition per numbered run: Word and LibreOffice then restart at 1

    def list_table(runs: list[int]) -> str:
        def level(i: int, ordered: bool) -> str:
            indent = 720 * (i + 1)
            if ordered:
                nfc = 0 if i % 2 == 0 else 4
                return (f"{{\\listlevel\\levelnfc{nfc}\\levelnfcn{nfc}\\leveljc0\\leveljcn0\\levelfollow0\\levelstartat1\\levelspace0"
                        f"\\levelindent0{{\\leveltext\\leveltemplateid{100 + i}\\'02\\'00.;}}{{\\levelnumbers\\'01;}}\\fi-360\\li{indent}\\lin{indent}}}")
            ch = "\\u8226?" if i % 2 == 0 else "\\u8211?"
            return (f"{{\\listlevel\\levelnfc23\\levelnfcn23\\leveljc0\\leveljcn0\\levelfollow0\\levelstartat1\\levelspace0\\levelindent0"
                    f"{{\\leveltext\\leveltemplateid{200 + i}\\'01{ch};}}{{\\levelnumbers;}}\\fi-360\\li{indent}\\lin{indent}}}")
        bullets = "{\\list\\listtemplateid1\\listhybrid" + "".join(level(i, False) for i in range(9)) + "\\listid1}"
        numbers = "".join("{\\list\\listtemplateid" + str(1000 + k) + "\\listhybrid" + "".join(level(i, True) for i in range(9))
                          + "\\listid" + str(1000 + k) + "}" for k in runs)
        return "{\\*\\listtable" + bullets + numbers + "}"

    # ---- inline
    def run_format(attrs: dict[str, Any]) -> tuple[str, str]:
        on, off = "", ""
        if attrs.get("bold"):
            on, off = on + "\\b", "\\b0" + off
        if attrs.get("italic"):
            on, off = on + "\\i", "\\i0" + off
        if attrs.get("underline"):
            on, off = on + "\\ul", "\\ulnone" + off
        if attrs.get("strike"):
            on, off = on + "\\strike", "\\strike0" + off
        if attrs.get("superscript"):
            on, off = on + "\\super", "\\nosupersub" + off
        if attrs.get("subscript"):
            on, off = on + "\\sub", "\\nosupersub" + off
        if attrs.get("small_caps"):
            on, off = on + "\\scaps", "\\scaps0" + off
        if attrs.get("code"):
            on, off = on + "\\f1", "\\f0" + off
        if attrs.get("color"):
            on, off = on + f"\\cf{color_index(attrs['color'])}", "\\cf0" + off
        if attrs.get("background"):
            on, off = on + f"\\chcbpat{color_index(attrs['background'])}", "\\chcbpat0" + off
        if attrs.get("font_size_pt"):
            on, off = on + f"\\fs{int(attrs['font_size_pt'] * 2)}", off
        return on, off

    def inline(block: dict[str, Any], base: dict[str, Any] | None = None) -> str:
        parts = []
        for seg, attrs in span_segments(block["text"], block.get("spans")):
            props = dict(attrs)
            if props.get("style") in styles_char:
                props = {**styles_char[props["style"]], **props}
            merged = {**(base or {}), **props}
            on, off = run_format(merged)
            body = rtf_text(seg)
            piece = f"{{{on} {body}}}" if on else body
            if props.get("href"):
                url = props["href"].replace("\\", "").replace('"', "%22")
                piece = (f'{{\\field{{\\*\\fldinst{{HYPERLINK "{url}"}}}}{{\\fldrslt{{\\ul\\cf2 {body}}}}}}}')
            if props.get("ref"):
                kind, number = notes.use(props["ref"])
                ref = notes.references.get(props["ref"])
                if kind == "footnote" and ref:
                    piece += ("{\\super\\chftn}{\\footnote\\pard\\plain\\f0\\fs18{\\super\\chftn} " + rtf_text(ref["text"]) + "\\par}")
                elif kind == "citation":
                    piece += rtf_text(f"[{number}]")
            parts.append(piece)
        return "".join(parts)

    def para_base(block: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        props = styles_para.get(block.get("style", ""), {})
        ctrl = ""
        align = (block.get("attributes") or {}).get("align") or props.get("align")
        if align:
            ctrl += {"left": "\\ql", "center": "\\qc", "right": "\\qr", "justify": "\\qj"}[align]
        if "space_before_pt" in props:
            ctrl += f"\\sb{int(props['space_before_pt'] * 20)}"
        if "space_after_pt" in props:
            ctrl += f"\\sa{int(props['space_after_pt'] * 20)}"
        if "indent_mm" in props:
            ctrl += f"\\li{tw(props['indent_mm'])}"
        if "first_line_indent_mm" in props:
            ctrl += f"\\fi{tw(props['first_line_indent_mm'])}"
        if "line_height" in props:
            ctrl += f"\\sl{int(props['line_height'] * 240)}\\slmult1"
        if props.get("keep_with_next"):
            ctrl += "\\keepn"
        base = {k: props[k] for k in ("bold", "italic", "underline", "strike", "small_caps", "font_size_pt", "color", "background") if k in props}
        if has_page_break(block):
            ctrl += "\\pagebb"
        return ctrl, base

    out: list[str] = []
    first_model = sections.get(order[0]) if order else None
    first_props, first_setup = section_props(first_model)
    current = order[0] if order else None
    out.append(first_props)
    out.append(hf_for(first_model, first_setup))
    current_setup = [first_setup]
    waiting: list[Any] = []                        # [block, index into out] of the block whose column break is not yet written

    def settle() -> None:
        """A column break goes at the start of the block's own paragraph (``\\column`` is a control word of the text), not on a line of its own."""
        if not waiting:
            return
        block, at = waiting.pop()
        if at >= len(out) or not (block.get("extensions") or {}).get("slai.writer.column_break"):
            return
        chunk = out[at]
        space = chunk.find(" ") if chunk.startswith("\\pard") else -1
        if space > 0:
            out[at] = chunk[:space + 1] + "\\column " + chunk[space + 1:]
        else:
            out.insert(at, "\\pard\\plain\\column\\par\n")

    def new_section(block: dict[str, Any]) -> None:
        nonlocal current
        settle()
        waiting.append([block, len(out)])
        sid = block.get("section")
        if sid and sid != current:
            current = sid
            model = sections.get(sid)
            props, s = section_props(model, current_setup[0])
            current_setup[0] = s
            out.append("\\sect" + props + hf_for(model, s))
            waiting[-1][1] = len(out)

    for kind, item in group_blocks(doc["blocks"]):
        if kind == "list":
            new_section(item[0])
            ordered_run_ls: dict[int, int] = {}
            for block in item:
                if block is not item[0]:
                    settle()
                    waiting.append([block, len(out)])
                attrs = block.get("attributes") or {}
                depth = min(int(attrs.get("depth", 0)), 8)
                ordered = bool(attrs.get("ordered"))
                for d in [d for d in ordered_run_ls if d > depth]:
                    del ordered_run_ls[d]
                if ordered:
                    if depth not in ordered_run_ls:
                        numbered_runs.append(len(numbered_runs) + 1)
                        ordered_run_ls[depth] = 1000 + numbered_runs[-1]
                    ls = ordered_run_ls[depth]
                else:
                    ordered_run_ls.pop(depth, None)
                    ls = 1
                ctrl, base = para_base(block)
                indent = 720 * (depth + 1)
                out.append(f"\\pard\\plain\\f0\\fs{int(opts.font_size_pt * 2)}\\ls{ls}\\ilvl{depth}\\fi-360\\li{indent}\\sa80{ctrl} "
                           f"{inline(block, base)}\\par\n")
            continue
        block = item
        new_section(block)
        t = block["type"]
        fs = int(opts.font_size_pt * 2)
        ctrl, base = para_base(block)
        if t == "heading":
            level = heading_level(block)
            size = {1: 40, 2: 32, 3: 28, 4: 25, 5: 23, 6: 22}[level]
            mark_in, mark_out = (("{\\*\\bkmkstart " + toc_bookmark(block["id"]) + "}", "{\\*\\bkmkend " + toc_bookmark(block["id"]) + "}")
                                 if block["id"] in toc_targets else ("", ""))
            out.append(f"\\pard\\plain\\f0\\keepn\\sb240\\sa80\\outlinelevel{level - 1}\\b\\fs{size}{ctrl} {mark_in}{inline(block, base)}{mark_out}\\par\n")
        elif ((block.get("extensions") or {}).get("slai.sldc.toc") or {}).get("role") == "entry":
            meta = block["extensions"]["slai.sldc.toc"]
            title_meta = toc_title_meta.get(meta.get("id"), {})
            look = resolve_look(toc_looks.get(meta.get("id")), show_page_numbers=bool(title_meta.get("show_page_numbers", True)),
                                leader_dots=bool(title_meta.get("leader_dots", True)))
            label = toc_labels.get(meta.get("heading", "")) if look["position"] != "hidden" else None
            target = toc_bookmark(meta["heading"]) if label and meta.get("heading") in toc_targets else None
            field = ("{\\field{\\*\\fldinst PAGEREF " + target + " \\\\h}{\\fldrslt " + rtf_text(label) + "}}") if target else ""
            text = inline(block, base)
            if not target:
                out.append(f"\\pard\\plain\\f0\\fs{fs}\\sa60{ctrl} {text}\\par\n")
                continue
            props = {**(styles_para.get(block.get("style", ""), {})), **(block.get("attributes") or {})}
            sizes = setup_for(sections.get(current))
            widest = max((len(v) for v in toc_labels.values()), default=1) * 0.56 * (fs / 2)
            plan = tab_plan(look, (sizes.width_mm - sizes.left - sizes.right) * MM_PT, float(props.get("indent_mm", 0)) * MM_PT, 0.0,
                            float(look["number_column_pt"] or widest), props.get("align", "left"))
            leader = {"dot": "\\tldot", "hyphen": "\\tlhyph", "underscore": "\\tlul", "middleDot": "\\tlmdot", None: ""}[word_leader(look)]
            kinds = {"left": "", "center": "\\tqc", "right": "\\tqr"}
            stops = "".join(f"{kinds[stop['align']]}{leader if stop['leader'] else ''}\\tx{round(stop['x'] * 20)}" for stop in plan["stops"])
            body = "".join(text if part == "text" else "\\tab " if part == "tab" else "\\~\\~\\~" if part == "gap" else field for part in plan["sequence"])
            out.append(f"\\pard\\plain\\f0\\fs{fs}\\sa60{stops}{ctrl} {body}\\par\n")
        elif t == "quote":
            out.append(f"\\pard\\plain\\f0\\fs{fs}\\li720\\ri720\\sa120\\i{ctrl} {inline(block, base)}\\par\n")
        elif t == "code":
            out.append(f"\\pard\\plain\\f1\\fs{max(16, fs - 3)}\\cbpat3\\sa120{ctrl} {rtf_text(block['text'])}\\par\n")
        elif t == "table":
            if block["text"]:
                out.append(f"\\pard\\plain\\f0\\fs{fs}\\i\\keepn\\sa60{ctrl} {rtf_text(block['text'])}\\par\n")
                ctrl = ""
            out.append(_rtf_table(block, setup_for(sections.get(current)), tw, inline, fs, ctrl, color_index))
            out.append(f"\\pard\\plain\\f0\\fs{fs}\\par\n")
        elif t == "figure":
            out.append(_rtf_figure(package, block, setup_for(sections.get(current)), tw, warnings, ctrl))
        elif rule_look(block) is not None:
            look = rule_look(block) or {}
            width = round(look["width_pt"] * 20)
            if width > 75:                                    # RTF borders are at most 75 twips (3.75 pt) thick
                width = 75
                if "A rule thicker than 3.75 points is drawn 3.75 points thick in RTF" not in warnings:
                    warnings.append("A rule thicker than 3.75 points is drawn 3.75 points thick in RTF")
            kind = {"solid": "\\brdrs", "dashed": "\\brdrdash", "dotted": "\\brdrdot", "double": "\\brdrdb"}[look["style"]]
            page = "\\pagebb" if has_page_break(block) else ""
            before, after = round(look["space_before_pt"] * 20), round(look["space_after_pt"] * 20)
            out.append(f"\\pard\\plain\\f0\\fs2\\li0\\ri0\\sl-20\\slmult0\\sb{before}\\sa{after}{page}"
                       f"\\brdrb{kind}\\brdrw{max(2, width)}\\brsp0\\brdrcf{color_index(look['color'])}\\par\n")
        else:
            if not block["text"].strip():
                if has_page_break(block):
                    out.append(f"\\pard\\plain\\f0\\fs{fs}\\pagebb\\par\n")
                continue
            out.append(f"\\pard\\plain\\f0\\fs{fs}\\sa120{ctrl} {inline(block, base)}\\par\n")
    settle()
    if opts.include_notes and notes.cited:
        out.append(f"\\pard\\plain\\f0\\b\\fs32\\sb240\\sa80 References\\par\n")
        for line in notes.citation_lines():
            out.append(f"\\pard\\plain\\f0\\fs{int(opts.font_size_pt * 2)}\\sa80 {rtf_text(line)}\\par\n")

    font_name = opts.font_family or "Arial"
    color_table = "{\\colortbl;" + "".join(
        f"\\red{int(c[1:3], 16)}\\green{int(c[3:5], 16)}\\blue{int(c[5:7], 16)};" for c in colors) + "}"
    author = "; ".join((doc.get("publication") or {}).get("authors", []))
    header = ("{\\rtf1\\ansi\\ansicpg1252\\uc1\\deff0\\deflang1033\n"
              f"{{\\fonttbl{{\\f0\\fswiss\\fcharset0 {rtf_text(font_name)};}}{{\\f1\\fmodern\\fcharset0 Courier New;}}}}\n"
              f"{color_table}\n"
              + list_table_with_overrides(list_table(numbered_runs), numbered_runs) + "\n"
              f"{{\\info{{\\title {rtf_text(title)}}}" + (f"{{\\author {rtf_text(author)}}}" if author else "")
              + "{\\*\\generator SLAI Documaster;}}\n" + ("\\margmirror" if mirrored_document else "") + ("\\facingp" if paired_document else "") + "\\viewkind1\\fet0\n")
    dest.write_bytes((header + "".join(out) + "}").encode("ascii"))
    return WriteResult(dest, "rtf", "Documaster RTF writer", warnings=warnings,
                       kept=["headings", "bullet and numbered lists", "tables with merged cells and header rows", "quotes", "code",
                             "bold/italic/underline/strike/super/subscript/colour", "hyperlinks", "native footnotes",
                             "PNG/JPEG images with alt text", "page size, margins, columns (continuous sections for a selection, unequal widths, column breaks)", "contents entries with a leader tab and page-reference fields", "page size, margins, headers/footers with page numbers",
                             "page breaks", "title and author"],
                       lost=["GIF/WebP images (RTF cannot hold them)", "colour, thickness and style of the line between columns", "comments and revision history"])


def list_table_with_overrides(table: str, runs: list[int]) -> str:
    base = "{\\listoverride\\listid1\\listoverridecount0\\ls1}"
    return table + "{\\*\\listoverridetable" + base + "".join(
        f"{{\\listoverride\\listid{1000 + k}\\listoverridecount0\\ls{1000 + k}}}" for k in runs) + "}"


_RTF_LINE = {"solid": "\\brdrs", "dashed": "\\brdrdash", "dotted": "\\brdrdot", "double": "\\brdrdb"}


def _rtf_table(block: dict[str, Any], setup: PageSetup, tw: Any, inline: Any, fs: int, ctrl: str, color_index: Any = None) -> str:
    grid = table_grid(block["table"])
    look = table_look(block, grid) if color_index is not None else None
    text_width = setup.width_mm - setup.left - setup.right
    edges: list[int]
    if look is None:
        col_w = int(tw(text_width) / grid.columns)
        edges = [col_w * (c + 1) for c in range(grid.columns)]
        row_extra = "\\trleft0"
    else:
        widths_mm = look.get("column_widths_mm")
        fit = look.get("fit", "auto")
        if isinstance(widths_mm, list) and len(widths_mm) == grid.columns:
            widths = [float(w) for w in widths_mm]
            if fit == "window":
                widths = [w * text_width / sum(widths) for w in widths]
        else:
            total = min(float(look["width_mm"]), text_width) if look.get("width_mm") and fit != "window" else text_width
            widths = [total / grid.columns] * grid.columns
        if sum(widths) > text_width:
            widths = [w * text_width / sum(widths) for w in widths]
        edges, run = [], 0.0
        start_mm = float(look["indent_mm"]) if look.get("indent_mm") and look.get("align") not in ("center", "right") else 0.0
        if look.get("align") == "center":
            start_mm = (text_width - sum(widths)) / 2
        elif look.get("align") == "right":
            start_mm = text_width - sum(widths)
        for w in widths:
            run += w
            edges.append(tw(start_mm + run))
        row_extra = f"\\trleft{tw(start_mm)}"
    origin: dict[tuple[int, int], tuple[int, int, dict[str, Any]]] = {}
    for r, row in enumerate(grid.rows):
        for c, cell in enumerate(row):
            if cell is None:
                continue
            for rr in range(r, r + cell.get("rowspan", 1)):
                for cc in range(c, c + cell.get("colspan", 1)):
                    origin[(rr, cc)] = (r, c, cell)

    def border_of(side: str, spec: dict[str, Any] | None) -> str:
        if spec is None:
            return f"\\clbrdr{side}\\brdrnil"
        line = _RTF_LINE.get(spec.get("style", "solid"), _RTF_LINE["solid"])
        width = max(1, min(75, round(float(spec["width_pt"]) * 20)))          # RTF stops at 75 twips (3.75 pt) per line
        return f"\\clbrdr{side}{line}\\brdrw{width}\\brdrcf{color_index(spec['color'])}"

    out = []
    plain_border = "".join(f"\\clbrdr{side}\\brdrs\\brdrw10\\brdrcf1" for side in ("t", "l", "b", "r"))
    heights = look.get("row_heights_mm") if look else None
    for r in range(len(grid.rows)):
        head = "\\trhdr" if r < grid.header_rows else ""
        cells_def = []
        cells_body = []
        for c in range(grid.columns):
            r0, c0, cell = origin[(r, c)]
            merge = ""
            if cell.get("colspan", 1) > 1:
                merge += "\\clmgf" if c == c0 else "\\clmrg"
            if cell.get("rowspan", 1) > 1:
                merge += "\\clvmgf" if r == r0 else "\\clvmrg"
            mine: dict[str, Any] = {}
            if look is None:
                borders = plain_border
            else:
                rs, cs = cell.get("rowspan", 1), cell.get("colspan", 1)
                mine = look["cells"].get(f"{r0},{c0}", {})
                # a merged cell is one cell: its pieces each carry the edges they lie on
                top = look["h"].get(f"{r},{c}")
                bottom = look["h"].get(f"{r + 1},{c}")
                left = look["v"].get(f"{r},{c}")
                right = look["v"].get(f"{r},{c + 1}")
                borders = border_of("t", top) + border_of("l", left) + border_of("b", bottom) + border_of("r", right)
                if mine.get("fill"):
                    borders += f"\\clcbpat{color_index(mine['fill'])}"
                if mine.get("valign"):
                    borders += {"top": "\\clvertalt", "middle": "\\clvertalc", "bottom": "\\clvertalb"}[mine["valign"]]
                if mine.get("padding_mm") is not None:
                    pad = tw(float(mine["padding_mm"]))
                    borders += f"\\clpadt{pad}\\clpadft3\\clpadl{pad}\\clpadfl3\\clpadb{pad}\\clpadfb3\\clpadr{pad}\\clpadfr3"
            cells_def.append(f"{merge}{borders}\\cellx{edges[c]}")
            if (r, c) == (r0, c0):
                bold = "\\b" if (bool(mine.get("bold")) if look is not None else r < grid.header_rows) else ""
                colour = f"\\cf{color_index(mine['color'])}" if mine.get("color") else ""
                align = {"left": "\\ql", "center": "\\qc", "right": "\\qr", "justify": "\\qj"}.get(cell.get("align", ""), "")
                lead = ctrl if (r, c) == (0, 0) else ""        # the page break (or section) the table starts with rides on its first paragraph
                cells_body.append(f"\\pard\\plain\\f0\\fs{fs}\\intbl{align}{bold}{colour}{lead} {rtf_text(cell['text'])}\\cell")
            else:
                cells_body.append("\\pard\\plain\\intbl\\cell")
        height = ""
        if isinstance(heights, list) and len(heights) == len(grid.rows):
            height = f"\\trrh{'-' if look.get('row_height_rule') == 'exact' else ''}{tw(float(heights[r]))}"
        gap = tw(float(look["cell_padding_mm"])) if look and look.get("cell_padding_mm") is not None else 108
        out.append(f"\\trowd\\trgaph{gap}{row_extra}{head}{height}{''.join(cells_def)}\n{''.join(cells_body)}\\row\n")
    return "".join(out)


def _rtf_figure(package: Any, block: dict[str, Any], setup: PageSetup, tw: Any, warnings: list[str], ctrl: str) -> str:
    fig = block["figure"]
    got = asset_bytes(package, fig["asset"])
    if not got:
        warnings.append(f"Image {fig['asset']} is missing from the package and was skipped")
        return ""
    data, media = got
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            px_w, px_h = im.size
            if media not in ("image/png", "image/jpeg"):
                buf = io.BytesIO()
                im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB").save(buf, format="PNG")
                data, media = buf.getvalue(), "image/png"
    except Exception:  # noqa: BLE001
        if media not in ("image/png", "image/jpeg"):
            warnings.append(f"Image {fig['asset']} ({media}) could not be converted for RTF and was skipped")
            return ""
        px_w, px_h = 800, 600
    max_w = setup.width_mm - setup.left - setup.right
    width_mm = min(float(fig.get("width_mm", max_w)), max_w)
    height_mm = width_mm * px_h / max(px_w, 1)
    blip = "\\pngblip" if media == "image/png" else "\\jpegblip"
    hex_data = data.hex()
    lines = "\n".join(hex_data[i:i + 128] for i in range(0, len(hex_data), 128))
    alt = fig.get("alt", "")
    prop = (f"{{\\*\\picprop{{\\sp{{\\sn wzDescription}}{{\\sv {rtf_text(alt)}}}}}}}" if alt else "")
    align = {"left": "\\ql", "center": "\\qc", "right": "\\qr"}.get(fig.get("align", "center"), "\\qc")
    cap = f"\\pard\\plain\\f0\\qc\\i\\fs20\\sa120 {rtf_text(block['text'])}\\par\n" if block["text"] else ""
    return (f"\\pard\\plain{align}\\sa60{ctrl} {{\\pict{prop}{blip}\\picw{px_w}\\pich{px_h}\\picwgoal{tw(width_mm)}"
            f"\\pichgoal{tw(height_mm)}\n{lines}}}\\par\n{cap}")
