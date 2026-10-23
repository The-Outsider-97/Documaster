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
from .common import (Notes, PageSetup, PAGE_SIZES_MM, asset_bytes, char_style_table, clean, group_blocks, page_setup,
                     style_table, table_grid)

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
    title = opts.title or doc["title"]
    warnings: list[str] = []
    colors: list[str] = ["#000000", "#0563C1", "#F2F2F2"]

    def color_index(value: str) -> int:
        value = value.upper()
        if value not in colors:
            colors.append(value)
        return colors.index(value) + 1

    def setup_for(section: dict[str, Any] | None) -> PageSetup:
        s = page_setup(section)
        if opts.page_size and not (section and "page" in section) and opts.page_size in PAGE_SIZES_MM:
            s.width_mm, s.height_mm = PAGE_SIZES_MM[opts.page_size]
        if opts.margins_mm is not None and not (section and "page" in section):
            s.top = s.right = s.bottom = s.left = opts.margins_mm
        return s

    def section_props(section: dict[str, Any] | None) -> tuple[str, PageSetup]:
        s = setup_for(section)
        landscape = "\\lndscpsxn" if s.width_mm > s.height_mm else ""
        cols = f"\\cols{s.columns}\\colsx{tw(s.column_gap_mm)}" if s.columns > 1 else ""
        text = (f"\\sectd\\sbkpage\\pgwsxn{tw(s.width_mm)}\\pghsxn{tw(s.height_mm)}{landscape}\\marglsxn{tw(s.left)}"
                f"\\margrsxn{tw(s.right)}\\margtsxn{tw(s.top)}\\margbsxn{tw(s.bottom)}{cols}")
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
            if template:
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

    def new_section(block: dict[str, Any]) -> None:
        nonlocal current
        sid = block.get("section")
        if sid and sid != current:
            current = sid
            model = sections.get(sid)
            props, s = section_props(model)
            out.append("\\sect" + props + hf_for(model, s))

    for kind, item in group_blocks(doc["blocks"]):
        if kind == "list":
            new_section(item[0])
            ordered_run_ls: dict[int, int] = {}
            for block in item:
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
            out.append(f"\\pard\\plain\\f0\\keepn\\sb240\\sa80\\outlinelevel{level - 1}\\b\\fs{size}{ctrl} {inline(block, base)}\\par\n")
        elif t == "quote":
            out.append(f"\\pard\\plain\\f0\\fs{fs}\\li720\\ri720\\sa120\\i{ctrl} {inline(block, base)}\\par\n")
        elif t == "code":
            out.append(f"\\pard\\plain\\f1\\fs{max(16, fs - 3)}\\cbpat3\\sa120{ctrl} {rtf_text(block['text'])}\\par\n")
        elif t == "table":
            if block["text"]:
                out.append(f"\\pard\\plain\\f0\\fs{fs}\\i\\keepn\\sa60{ctrl} {rtf_text(block['text'])}\\par\n")
                ctrl = ""
            out.append(_rtf_table(block, setup_for(sections.get(current)), tw, inline, fs, ctrl))
            out.append(f"\\pard\\plain\\f0\\fs{fs}\\par\n")
        elif t == "figure":
            out.append(_rtf_figure(package, block, setup_for(sections.get(current)), tw, warnings, ctrl))
        else:
            if not block["text"].strip():
                if has_page_break(block):
                    out.append(f"\\pard\\plain\\f0\\fs{fs}\\pagebb\\par\n")
                continue
            out.append(f"\\pard\\plain\\f0\\fs{fs}\\sa120{ctrl} {inline(block, base)}\\par\n")
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
              + "{\\*\\generator SLAI Documaster;}}\n\\viewkind1\\fet0\n")
    dest.write_bytes((header + "".join(out) + "}").encode("ascii"))
    return WriteResult(dest, "rtf", "Documaster RTF writer", warnings=warnings,
                       kept=["headings", "bullet and numbered lists", "tables with merged cells and header rows", "quotes", "code",
                             "bold/italic/underline/strike/super/subscript/colour", "hyperlinks", "native footnotes",
                             "PNG/JPEG images with alt text", "page size, margins, headers/footers with page numbers",
                             "page breaks", "title and author"],
                       lost=["GIF/WebP images (RTF cannot hold them)", "comments and revision history"])


def list_table_with_overrides(table: str, runs: list[int]) -> str:
    base = "{\\listoverride\\listid1\\listoverridecount0\\ls1}"
    return table + "{\\*\\listoverridetable" + base + "".join(
        f"{{\\listoverride\\listid{1000 + k}\\listoverridecount0\\ls{1000 + k}}}" for k in runs) + "}"


def _rtf_table(block: dict[str, Any], setup: PageSetup, tw: Any, inline: Any, fs: int, ctrl: str) -> str:
    grid = table_grid(block["table"])
    width = setup.width_mm - setup.left - setup.right
    col_w = int(tw(width) / grid.columns)
    origin: dict[tuple[int, int], tuple[int, int, dict[str, Any]]] = {}
    for r, row in enumerate(grid.rows):
        for c, cell in enumerate(row):
            if cell is None:
                continue
            for rr in range(r, r + cell.get("rowspan", 1)):
                for cc in range(c, c + cell.get("colspan", 1)):
                    origin[(rr, cc)] = (r, c, cell)
    out = []
    border = "".join(f"\\clbrdr{side}\\brdrs\\brdrw10\\brdrcf1" for side in ("t", "l", "b", "r"))
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
            cells_def.append(f"{merge}{border}\\cellx{col_w * (c + 1)}")
            if (r, c) == (r0, c0):
                bold = "\\b" if r < grid.header_rows else ""
                align = {"left": "\\ql", "center": "\\qc", "right": "\\qr", "justify": "\\qj"}.get(cell.get("align", ""), "")
                cells_body.append(f"\\pard\\plain\\f0\\fs{fs}\\intbl{align}{bold} {rtf_text(cell['text'])}\\cell")
            else:
                cells_body.append("\\pard\\plain\\intbl\\cell")
        out.append(f"\\trowd\\trgaph108\\trleft0{head}{''.join(cells_def)}\n{''.join(cells_body)}\\row\n")
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
