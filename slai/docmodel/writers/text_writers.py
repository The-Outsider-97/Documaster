"""TXT, Markdown, HTML, XML and SLDC writers."""
from __future__ import annotations

import base64
import html as htmllib
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from ...bridge import sldc
from ..ir import heading_level, has_page_break, span_segments
from . import WriteOptions, WriteResult
from .common import (Notes, asset_bytes, clean, group_blocks, list_labels, table_grid, table_text)

_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}


# ------------------------------------------------------------------ plain text
def render_text(package: Any, opts: WriteOptions | None = None) -> str:
    doc = package.document
    notes = Notes.of(doc)
    out: list[str] = []
    for kind, item in group_blocks(doc["blocks"]):
        if kind == "list":
            labels = list_labels(item)
            lines = []
            for label, block in zip(labels, item):
                depth = int((block.get("attributes") or {}).get("depth", 0))
                text = _with_marks_text(block, notes)
                first, *rest = text.split("\n")
                pad = "  " * depth
                lines.append(f"{pad}{label} {first}" + "".join(f"\n{pad}   {r}" for r in rest))
            out.append("\n".join(lines))
            continue
        block = item
        t = block["type"]
        if t == "heading":
            out.append(_with_marks_text(block, notes))
        elif t == "quote":
            out.append("\n".join("> " + ln for ln in _with_marks_text(block, notes).split("\n")))
        elif t == "code":
            out.append(block["text"])
        elif t == "table":
            rows = table_text(block)
            piece = "\n".join("\t".join(c.replace("\n", " ") for c in row) for row in rows)
            out.append((block["text"] + "\n" if block["text"] else "") + piece)
        elif t == "figure":
            fig = block["figure"]
            alt = fig.get("alt", "")
            cap = block["text"]
            out.append("[Image" + (f": {alt}" if alt else "") + "]" + (f"\n{cap}" if cap else ""))
        else:
            piece = _with_marks_text(block, notes)
            if piece.strip():
                out.append(piece)
    if opts is None or opts.include_notes:
        if notes.note_numbers:
            out.append("\n".join(f"[{n}] {text}" for n, text in notes.footnotes()))
        if notes.cited:
            out.append("\n".join(notes.citation_lines()))
    return "\n\n".join(out) + "\n"


def _with_marks_text(block: dict[str, Any], notes: Notes) -> str:
    text = block["text"]
    spans = [s for s in block.get("spans", []) if s["type"] == "ref"]
    if not spans:
        return text
    pieces, last = [], 0
    for s in sorted(spans, key=lambda s: s["start"]):
        pieces.append(text[last:s["end"]])
        kind, number = notes.use(s["ref"])
        pieces.append(f"[{number}]" if number else "")
        last = s["end"]
    pieces.append(text[last:])
    return "".join(pieces)


def write_txt(package: Any, dest: Path, opts: WriteOptions) -> WriteResult:
    text = render_text(package, opts)
    if opts.line_ending != "\n":
        text = text.replace("\n", opts.line_ending)
    dest.write_bytes(text.encode("utf-8"))
    return WriteResult(dest, "txt", "Documaster text writer (UTF-8)",
                       kept=["all text", "list markers", "table cells (tab-separated)", "footnotes and citations as numbered notes"],
                       lost=["formatting", "images (alt text is kept)", "page structure"])


# ------------------------------------------------------------------ Markdown
def write_md(package: Any, dest: Path, opts: WriteOptions) -> WriteResult:
    s = sldc()
    folder = f"{opts.stem}_files"
    names = {aid: f"{aid}.{_EXT.get(a.media_type, 'bin')}" for aid, a in package.assets.items()}
    used = _used_assets(package)
    text = s.to_markdown(package, validate=False, asset_href=lambda aid: f"{folder}/{names.get(aid, aid)}")
    if opts.line_ending != "\n":
        text = text.replace("\n", opts.line_ending)
    dest.write_bytes(text.encode("utf-8"))
    result = WriteResult(dest, "md", "SLDC to_markdown (src.utils.sldc)",
                         kept=["headings", "lists", "quotes", "code", "tables", "bold/italic/code/strike", "links",
                               "footnotes", "citations", "images (as files in a folder)"],
                         lost=["fonts and colours", "page layout, headers/footers, page breaks", "underline, super/subscript",
                               "styles and sections"])
    for aid in used:
        asset = package.assets[aid]
        result.extra_data.append((f"{folder}/{names[aid]}", asset.data))
    if used:
        result.warnings.append(f"{len(used)} image(s) are saved next to the Markdown file in the folder '{folder}'")
    return result


def _used_assets(package: Any) -> list[str]:
    seen: list[str] = []
    for b in package.document["blocks"]:
        if b["type"] == "figure" and b["figure"]["asset"] not in seen and b["figure"]["asset"] in package.assets:
            seen.append(b["figure"]["asset"])
    return seen


# ------------------------------------------------------------------ HTML
CSS = """
:root{color-scheme:light dark}
body{font:16px/1.6 -apple-system,"Segoe UI",system-ui,Roboto,"Noto Sans",Arial,sans-serif;max-width:46rem;margin:2rem auto;padding:0 1rem;color:#1b1f24;background:#fff}
@media (prefers-color-scheme:dark){body{color:#e6e8eb;background:#14171b}a{color:#8ab4ff}th{background:#222831}td,th{border-color:#3a414b}pre,code{background:#1d2229}}
h1,h2,h3,h4,h5,h6{line-height:1.25;margin:1.6em 0 .5em}
p{margin:.7em 0}
blockquote{margin:1em 0;padding:.1em 1em;border-left:4px solid #9aa4b2;color:inherit;opacity:.92}
pre{background:#f3f4f6;padding:.8em 1em;overflow:auto;border-radius:6px;white-space:pre}
code{font-family:ui-monospace,Consolas,"DejaVu Sans Mono",monospace;background:#f3f4f6;padding:.05em .3em;border-radius:4px}
pre code{background:none;padding:0}
table{border-collapse:collapse;margin:1em 0;width:100%}
td,th{border:1px solid #c9ced6;padding:.35em .6em;vertical-align:top}
th{background:#f3f4f6;text-align:left}
figure{margin:1em 0;text-align:center}figure img{max-width:100%;height:auto}
figcaption{font-size:.9em;opacity:.8}
.notes{font-size:.9em;border-top:1px solid #c9ced6;margin-top:2em;padding-top:.5em}
@media print{body{max-width:none;margin:0}.pb{page-break-before:always}}
""".strip()


def write_html(package: Any, dest: Path, opts: WriteOptions) -> WriteResult:
    doc = package.document
    notes = Notes.of(doc)
    body: list[str] = []
    e = lambda t: htmllib.escape(clean(t), quote=True)  # noqa: E731
    ids = {b["id"] for b in doc["blocks"]}

    def inline(block: dict[str, Any]) -> str:
        parts = []
        for seg, attrs in span_segments(block["text"], block.get("spans")):
            h = e(seg).replace("\n", "<br>")
            if attrs.get("code"):
                h = f"<code>{h}</code>"
            for flag, tag in (("bold", "strong"), ("italic", "em"), ("underline", "u"), ("strike", "s"),
                              ("superscript", "sup"), ("subscript", "sub")):
                if attrs.get(flag):
                    h = f"<{tag}>{h}</{tag}>"
            style = []
            if attrs.get("color"):
                style.append(f"color:{attrs['color']}")
            if attrs.get("background"):
                style.append(f"background:{attrs['background']}")
            if style:
                h = f'<span style="{";".join(style)}">{h}</span>'
            if attrs.get("href"):
                h = f'<a href="{e(attrs["href"])}" rel="noopener noreferrer">{h}</a>'
            if attrs.get("xref") and attrs["xref"] in ids:
                h = f'<a href="#{e(attrs["xref"])}">{h}</a>'
            if attrs.get("ref"):
                kind, number = notes.use(attrs["ref"])
                if number:
                    h += f'<sup><a href="#note-{number}">[{number}]</a></sup>' if kind == "footnote" else \
                        f'<sup><a href="#cite-{number}">[{number}]</a></sup>'
            parts.append(h)
        return "".join(parts)

    def pb(block: dict[str, Any]) -> str:
        return ' class="pb"' if has_page_break(block) else ""

    for kind, item in group_blocks(doc["blocks"]):
        if kind == "list":
            body.append(_html_list(item, inline, ids, pb))
            continue
        b = item
        bid = f' id="{e(b["id"])}"'
        align = (b.get("attributes") or {}).get("align")
        style = f' style="text-align:{align}"' if align in ("center", "right", "justify") else ""
        t = b["type"]
        if t == "heading":
            lv = heading_level(b)
            body.append(f"<h{lv}{bid}{pb(b)}>{inline(b)}</h{lv}>")
        elif t == "quote":
            body.append(f"<blockquote{bid}{pb(b)}><p>{inline(b)}</p></blockquote>")
        elif t == "code":
            lang = (b.get("attributes") or {}).get("language")
            cls = f' class="language-{e(lang)}"' if lang else ""
            body.append(f"<pre{bid}{pb(b)}><code{cls}>{e(b['text'])}</code></pre>")
        elif t == "table":
            body.append(_html_table(b, e, pb(b)))
        elif t == "figure":
            body.append(_html_figure(package, b, e, pb(b)))
        else:
            if b["text"].strip():
                body.append(f"<p{bid}{style}{pb(b) or ''}>{inline(b)}</p>")
    if opts.include_notes and (notes.note_numbers or notes.cited):
        extra = ['<section class="notes">']
        if notes.note_numbers:
            extra.append("<ol>" + "".join(f'<li id="note-{n}" value="{n}">{e(t)}</li>' for n, t in notes.footnotes()) + "</ol>")
        if notes.cited:
            extra.append("<ol>" + "".join(f'<li id="cite-{i}" value="{i}">{e(line.split("] ", 1)[1])}</li>'
                                          for i, line in enumerate(notes.citation_lines(), 1)) + "</ol>")
        extra.append("</section>")
        body.append("".join(extra))
    title = opts.title or doc["title"]
    lang = e(doc.get("language", "und"))
    page = (f'<!DOCTYPE html>\n<html lang="{lang}">\n<head>\n<meta charset="utf-8">\n'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src data:; style-src \'unsafe-inline\'">\n'
            '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
            f"<title>{e(title)}</title>\n<style>{CSS}</style>\n</head>\n<body>\n" + "\n".join(body) + "\n</body>\n</html>\n")
    if opts.line_ending != "\n":
        page = page.replace("\n", opts.line_ending)
    dest.write_bytes(page.encode("utf-8"))
    return WriteResult(dest, "html", "Documaster HTML writer (self-contained, no scripts)",
                       kept=["headings", "lists", "tables incl. merged cells", "quotes", "code", "bold/italic/underline/strike/"
                             "super/subscript/colour", "links", "images (embedded)", "footnotes and citations", "page breaks for printing"],
                       lost=["page size, margins, headers/footers", "named paragraph styles (their look is not applied)"])


def _html_list(items: list[dict[str, Any]], inline: Any, ids: set[str], pb: Any) -> str:
    out: list[str] = []
    stack: list[str] = []                      # open list tags, index = nesting depth
    for block in items:
        attrs = block.get("attributes") or {}
        want = min(int(attrs.get("depth", 0)), len(stack))   # a list can open at most one new level at a time
        tag = "ol" if attrs.get("ordered") else "ul"
        while len(stack) > want + 1:
            out.append(f"</li></{stack.pop()}>")
        if len(stack) == want + 1:
            if stack[-1] != tag:
                out.append(f"</li></{stack.pop()}><{tag}>")
                stack.append(tag)
            else:
                out.append("</li>")
        else:
            out.append(f"<{tag}>")
            stack.append(tag)
        out.append(f'<li id="{htmllib.escape(block["id"])}"{pb(block)}>{inline(block)}')
    while stack:
        out.append(f"</li></{stack.pop()}>")
    return "".join(out)


def _html_table(block: dict[str, Any], e: Any, pagebreak: str) -> str:
    grid = table_grid(block["table"])
    rows = []
    for r, row in enumerate(grid.rows):
        cells = []
        for cell in row:
            if cell is None:
                continue
            tag = "th" if r < grid.header_rows or cell.get("header") else "td"
            attrs = ""
            if cell.get("colspan", 1) > 1:
                attrs += f' colspan="{cell["colspan"]}"'
            if cell.get("rowspan", 1) > 1:
                attrs += f' rowspan="{cell["rowspan"]}"'
            if cell.get("align"):
                attrs += f' style="text-align:{cell["align"]}"'
            cells.append(f"<{tag}{attrs}>{e(cell['text']).replace(chr(10), '<br>')}</{tag}>")
        rows.append(f"<tr>{''.join(cells)}</tr>")
    head = "".join(rows[:grid.header_rows])
    tail = "".join(rows[grid.header_rows:])
    caption = f"<caption>{e(block['text'])}</caption>" if block["text"] else ""
    return (f"<table id=\"{e(block['id'])}\"{pagebreak}>{caption}" + (f"<thead>{head}</thead>" if head else "") +
            f"<tbody>{tail}</tbody></table>")


def _html_figure(package: Any, block: dict[str, Any], e: Any, pagebreak: str) -> str:
    fig = block["figure"]
    got = asset_bytes(package, fig["asset"])
    if not got:
        return ""
    data, media = got
    src = f"data:{media};base64,{base64.b64encode(data).decode('ascii')}"
    width = f' style="width:{fig["width_mm"]}mm;max-width:100%"' if "width_mm" in fig else ""
    alt = e(fig.get("alt", ""))
    cap = f"<figcaption>{e(block['text'])}</figcaption>" if block["text"] else ""
    return f'<figure id="{e(block["id"])}"{pagebreak}><img src="{src}" alt="{alt}"{width}>{cap}</figure>'


# ------------------------------------------------------------------ XML
NS_XML = "urn:slai:documaster:document:1"


def write_xml(package: Any, dest: Path, opts: WriteOptions) -> WriteResult:
    doc = package.document
    root = ET.Element("document", {"xmlns": NS_XML, "id": doc["id"], "title": clean(doc["title"]),
                                   "language": doc.get("language", "und")})
    notes = Notes.of(doc)

    def fill(parent: ET.Element, block: dict[str, Any]) -> None:
        last: ET.Element | None = None
        parent.text = ""
        for seg, attrs in span_segments(block["text"], block.get("spans")):
            node: ET.Element | None = None
            wrapped: ET.Element | None = None
            chain = [(k, t) for k, t in (("bold", "b"), ("italic", "i"), ("underline", "u"), ("strike", "s"),
                                         ("superscript", "sup"), ("subscript", "sub"), ("code", "code")) if attrs.get(k)]
            if attrs.get("href"):
                chain.append(("href", "a"))
            if attrs.get("ref"):
                kind, number = notes.use(attrs["ref"])
                chain.append(("ref", "ref"))
            if not chain:
                if last is None:
                    parent.text = (parent.text or "") + clean(seg)
                else:
                    last.tail = (last.tail or "") + clean(seg)
                continue
            top = ET.SubElement(parent, chain[0][1])
            cur = top
            for i, (key, tag) in enumerate(chain):
                if i:
                    cur = ET.SubElement(cur, tag)
                if key == "href":
                    cur.set("href", attrs["href"])
                if key == "ref":
                    cur.set("ref", attrs["ref"])
            cur.text = clean(seg)
            last = top

    for kind, item in group_blocks(doc["blocks"]):
        blocks = item if kind == "list" else [item]
        for b in blocks:
            t = b["type"]
            attrs = b.get("attributes") or {}
            if t == "heading":
                el = ET.SubElement(root, "heading", {"level": str(heading_level(b)), "id": b["id"]})
                fill(el, b)
            elif t == "list_item":
                el = ET.SubElement(root, "item", {"id": b["id"], "ordered": str(bool(attrs.get("ordered"))).lower(),
                                                 "depth": str(attrs.get("depth", 0))})
                fill(el, b)
            elif t == "quote":
                el = ET.SubElement(root, "quote", {"id": b["id"]})
                fill(el, b)
            elif t == "code":
                el = ET.SubElement(root, "code", {"id": b["id"], **({"language": attrs["language"]} if attrs.get("language") else {})})
                el.text = clean(b["text"])
            elif t == "table":
                el = ET.SubElement(root, "table", {"id": b["id"], "columns": str(b["table"]["columns"]),
                                                  "headerRows": str(b["table"].get("header_rows", 0))})
                if b["text"]:
                    ET.SubElement(el, "caption").text = clean(b["text"])
                for row in b["table"]["rows"]:
                    tr = ET.SubElement(el, "row")
                    for cell in row:
                        td = ET.SubElement(tr, "cell", {k: str(v) for k, v in cell.items() if k in ("colspan", "rowspan", "align")})
                        td.text = clean(cell["text"])
            elif t == "figure":
                fig = b["figure"]
                got = asset_bytes(package, fig["asset"])
                el = ET.SubElement(root, "figure", {"id": b["id"], "alt": clean(fig.get("alt", "")),
                                                   "mediaType": got[1] if got else ""})
                if b["text"]:
                    ET.SubElement(el, "caption").text = clean(b["text"])
                if got:
                    img = ET.SubElement(el, "data", {"encoding": "base64"})
                    img.text = base64.b64encode(got[0]).decode("ascii")
            else:
                if b["text"].strip():
                    el = ET.SubElement(root, "p", {"id": b["id"], **({"align": attrs["align"]} if attrs.get("align") else {})})
                    fill(el, b)
    if notes.note_numbers or notes.cited:
        ns = ET.SubElement(root, "notes")
        for n, text in notes.footnotes():
            ET.SubElement(ns, "footnote", {"number": str(n)}).text = clean(text)
        for line in notes.citation_lines():
            ET.SubElement(ns, "citation").text = clean(line)
    ET.indent(root)
    xml = ET.tostring(root, encoding="unicode")
    dest.write_bytes(('<?xml version="1.0" encoding="UTF-8"?>\n' + xml + "\n").encode("utf-8"))
    return WriteResult(dest, "xml", "Documaster XML writer (ElementTree)",
                       kept=["headings", "lists", "tables incl. merged cells", "quotes", "code", "inline formatting and links",
                             "images (base64)", "footnotes and citations"],
                       lost=["page layout", "named styles", "comments and revisions"])


# ------------------------------------------------------------------ SLDC
def write_sldc(package: Any, dest: Path, opts: WriteOptions) -> WriteResult:
    s = sldc()
    pkg = package.copy()
    if opts.title:
        pkg.document["title"] = opts.title
    data = s.dumps_package(pkg)
    dest.write_bytes(data)
    return WriteResult(dest, "sldc", "SLDC (src.utils.sldc.dumps_package)",
                       kept=["everything the model holds: structure, styles, sections, tables, figures, references, "
                             "comments, provenance, revisions"],
                       lost=[])
