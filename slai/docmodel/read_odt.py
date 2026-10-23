"""OpenDocument Text (.odt) -> document model. SLAI's Reader has no ODT parser, so this is Documaster's own (stdlib
``zipfile`` + ``xml.etree``). It keeps headings, paragraphs, lists, tables, bold/italic/underline/strike, links, page
breaks and embedded PNG/JPEG/GIF pictures. The package is treated as untrusted: size limits, no external entities
(DOCTYPE is rejected), nothing referenced from outside the archive is read.
"""
from __future__ import annotations

import re
import zipfile
from typing import Any
from xml.etree import ElementTree as ET

from ..bridge import sldc
from ..errors import FileRejected

NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "style": "urn:oasis:names:tc:opendocument:xmlns:style:1.0",
    "fo": "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0",
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
    "xlink": "http://www.w3.org/1999/xlink",
    "svg": "urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0",
    "dc": "http://purl.org/dc/elements/1.1/",
    "meta": "urn:oasis:names:tc:opendocument:xmlns:meta:1.0",
}


def q(prefix: str, name: str) -> str:
    return "{%s}%s" % (NS[prefix], name)


MAX_XML = 40 * 1024 * 1024
_IMG = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif"}


def _parse_xml(raw: bytes) -> ET.Element:
    if b"<!DOCTYPE" in raw[:4096].upper() or b"<!ENTITY" in raw[:65536].upper():
        raise FileRejected("This ODT contains a DOCTYPE/entity declaration, which is refused for safety")
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise FileRejected(f"This ODT contains malformed XML: {exc}") from exc


def read_odt(path: str) -> dict[str, Any]:
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise FileRejected(f"This ODT is not a valid OpenDocument archive: {exc}") from exc
    with archive:
        total = sum(i.file_size for i in archive.infolist())
        if total > 400 * 1024 * 1024 or len(archive.infolist()) > 5000:
            raise FileRejected("This ODT archive is larger than the safety limit")
        try:
            content_info = archive.getinfo("content.xml")
        except KeyError as exc:
            raise FileRejected("This ODT has no content.xml") from exc
        if content_info.file_size > MAX_XML:
            raise FileRejected("content.xml is larger than the safety limit")
        root = _parse_xml(archive.read("content.xml"))
        meta_title = ""
        meta_author = ""
        if "meta.xml" in archive.namelist() and archive.getinfo("meta.xml").file_size < 2_000_000:
            try:
                meta = _parse_xml(archive.read("meta.xml"))
                meta_title = (meta.findtext(".//" + q("dc", "title")) or "").strip()
                meta_author = (meta.findtext(".//" + q("dc", "creator")) or "").strip()
            except FileRejected:
                pass
        styles = _style_flags(root)
        if "styles.xml" in archive.namelist() and archive.getinfo("styles.xml").file_size < MAX_XML:
            named = _parse_xml(archive.read("styles.xml")).find(q("office", "styles"))
            if named is not None:
                for key, value in _list_styles(named).items():
                    styles.setdefault(key, value)
        reader = _Walker(archive, styles)
        body = root.find(q("office", "body") + "/" + q("office", "text"))
        if body is None:
            raise FileRejected("This OpenDocument file is not a text document (office:text is missing)")
        reader.walk_children(body)
    dropped = []
    if reader.notes:
        dropped.append("footnotes/endnotes")
    if reader.skipped_images:
        dropped.append(f"{reader.skipped_images} image(s) in unsupported formats or over the size limit")
    return {"blocks": reader.blocks, "assets": reader.assets, "title": meta_title, "author": meta_author,
            "dropped": dropped, "warnings": [], "images": len(reader.assets)}


def _list_styles(container: ET.Element) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for ls in container.findall(q("text", "list-style")):
        name = ls.get(q("style", "name"))
        first = next(iter(ls), None)
        if name and first is not None:
            found[name] = {"__ordered": first.tag == q("text", "list-level-style-number")}
    return found


def _style_flags(root: ET.Element) -> dict[str, dict[str, Any]]:
    """Automatic styles -> formatting flags and 'page break before'."""
    result: dict[str, dict[str, Any]] = {}
    auto = root.find(q("office", "automatic-styles"))
    if auto is None:
        return result
    result.update(_list_styles(auto))
    for style in auto.findall(q("style", "style")):
        name = style.get(q("style", "name"))
        if not name:
            continue
        flags: dict[str, Any] = {}
        tp = style.find(q("style", "text-properties"))
        if tp is not None:
            if tp.get(q("fo", "font-weight")) in ("bold", "700", "800", "900"):
                flags["bold"] = True
            if tp.get(q("fo", "font-style")) in ("italic", "oblique"):
                flags["italic"] = True
            underline = tp.get(q("style", "text-underline-style"))
            if underline and underline != "none":
                flags["underline"] = True
            strike = tp.get(q("style", "text-line-through-style"))
            if strike and strike != "none":
                flags["strike"] = True
            position = tp.get(q("style", "text-position")) or ""
            if position.startswith("super"):
                flags["superscript"] = True
            elif position.startswith("sub"):
                flags["subscript"] = True
        pp = style.find(q("style", "paragraph-properties"))
        if pp is not None and pp.get(q("fo", "break-before")) == "page":
            flags["__break"] = True
        if pp is not None and pp.get(q("fo", "text-align")) in ("center", "end", "right", "justify"):
            flags["__align"] = {"end": "right"}.get(pp.get(q("fo", "text-align")), pp.get(q("fo", "text-align")))
        result[name] = flags
    return result


class _Walker:
    def __init__(self, archive: zipfile.ZipFile, styles: dict[str, dict[str, Any]]) -> None:
        self.archive = archive
        self.styles = styles
        self.blocks: list[dict[str, Any]] = []
        self.assets: list[Any] = []
        self.pending_break = False
        self.notes = False
        self.skipped_images = 0
        self._asset_ids: set[str] = set()

    def push(self, block: dict[str, Any], *, break_before: bool = False) -> None:
        if self.pending_break or break_before:
            block.setdefault("attributes", {})["page_break_before"] = True
            self.pending_break = False
        self.blocks.append(block)

    def walk_children(self, parent: ET.Element, list_ctx: tuple[bool, int] | None = None) -> None:
        for child in parent:
            tag = child.tag
            if tag == q("text", "h"):
                self.paragraph(child, "heading", list_ctx)
            elif tag == q("text", "p"):
                self.paragraph(child, "paragraph", list_ctx)
            elif tag == q("text", "list"):
                list_style = self.styles.get(child.get(q("text", "style-name")) or "", {})
                ordered = bool(list_style.get("__ordered", (child.get(q("text", "style-name")) or "").lower().find("num") >= 0))
                if list_ctx is not None and child.get(q("text", "style-name")) is None:
                    ordered = list_ctx[0]
                depth = (list_ctx[1] + 1) if list_ctx else 0
                for item in child:
                    if item.tag in (q("text", "list-item"), q("text", "list-header")):
                        self.walk_children(item, (ordered, depth))
            elif tag == q("table", "table"):
                self.table(child)
            elif tag in (q("text", "section"), q("text", "index-body"), q("text", "table-of-content"), q("text", "index-title")):
                self.walk_children(child, list_ctx)
            elif tag == q("text", "soft-page-break"):
                self.pending_break = True

    def inline(self, element: ET.Element) -> tuple[str, list[dict[str, Any]], list[ET.Element]]:
        parts: list[str] = []
        spans: list[dict[str, Any]] = []
        images: list[ET.Element] = []
        length = 0

        def emit(text: str) -> None:
            nonlocal length
            parts.append(text)
            length += len(text)

        def walk(node: ET.Element, flags: dict[str, Any], href: str | None) -> None:
            if node.text:
                add(node.text, flags, href)
            for child in node:
                tag = child.tag
                if tag == q("text", "span"):
                    sub = dict(flags)
                    sub.update({k: v for k, v in self.styles.get(child.get(q("text", "style-name")) or "", {}).items()
                                if not k.startswith("__")})
                    walk(child, sub, href)
                elif tag == q("text", "a"):
                    target = child.get(q("xlink", "href")) or ""
                    safe = target if re.match(r"^(https?://|mailto:)", target, re.I) and len(target) < 2000 else None
                    walk(child, flags, safe or href)
                elif tag == q("text", "s"):
                    count = child.get(q("text", "c"))
                    add(" " * (int(count) if (count or "").isdigit() and int(count) < 200 else 1), flags, href)
                elif tag == q("text", "tab"):
                    add("\t", flags, href)
                elif tag == q("text", "line-break"):
                    add("\n", flags, href)
                elif tag == q("text", "soft-page-break"):
                    self.pending_break = True
                elif tag == q("draw", "frame"):
                    images.append(child)
                elif tag in (q("text", "note"),):
                    self.notes = True
                elif tag in (q("text", "bookmark"), q("text", "bookmark-start"), q("text", "bookmark-end")):
                    pass
                else:
                    walk(child, flags, href)
                if child.tail:
                    add(child.tail, flags, href)

        def add(text: str, flags: dict[str, Any], href: str | None) -> None:
            if not text:
                return
            start = length
            emit(text)
            end = length
            if flags:
                spans.append({"type": "format", "start": start, "end": end, **flags})
            if href:
                spans.append({"type": "link", "start": start, "end": end, "href": href})

        walk(element, {}, None)
        return "".join(parts), spans, images

    def paragraph(self, element: ET.Element, kind: str, list_ctx: tuple[bool, int] | None) -> None:
        text, spans, frames = self.inline(element)
        style = self.styles.get(element.get(q("text", "style-name")) or "", {})
        stripped = text.strip()
        lead = len(text) - len(text.lstrip())
        pictures = [b for b in (self.picture(frame) for frame in frames) if b is not None]
        if not stripped and not pictures:
            if style.get("__break"):
                self.pending_break = True
            return
        attrs: dict[str, Any] = {}
        block_kind = kind
        style_name = element.get(q("text", "style-name")) or ""
        if kind == "paragraph" and list_ctx is None and style_name in ("Title", "Subtitle"):
            kind = block_kind = "heading"
            element.set(q("text", "outline-level"), "1" if style_name == "Title" else "2")
        if kind == "heading":
            level = element.get(q("text", "outline-level")) or "1"
            attrs["level"] = max(1, min(6, int(level))) if level.isdigit() else 1
        elif list_ctx is not None:
            block_kind = "list_item"
            attrs["ordered"] = list_ctx[0]
            if list_ctx[1]:
                attrs["depth"] = min(list_ctx[1], 32)
        elif style.get("__align") and block_kind == "paragraph":
            attrs["align"] = style["__align"]
        if stripped:
            clipped = []
            for s in spans:
                a, b = max(s["start"] - lead, 0), min(s["end"] - lead, len(stripped))
                if a < b:
                    clipped.append({**s, "start": a, "end": b})
            clipped.sort(key=lambda s: (s["start"], s["end"], s["type"]))
            self.push(sldc().make_block(block_kind, stripped, attributes=attrs or None, spans=clipped or None),
                      break_before=bool(style.get("__break")))
        elif style.get("__break"):
            self.pending_break = True
        for picture in pictures:
            self.push(picture)

    def picture(self, frame: ET.Element) -> dict[str, Any] | None:
        image = frame.find(q("draw", "image"))
        if image is None:
            return None
        href = image.get(q("xlink", "href")) or ""
        if href.startswith(("http:", "https:", "/", "..")) or "\\" in href:
            self.skipped_images += 1
            return None
        ext = "." + href.rsplit(".", 1)[-1].lower() if "." in href else ""
        if ext not in _IMG:
            self.skipped_images += 1
            return None
        try:
            info = self.archive.getinfo(href)
            if info.file_size > 8 * 1024 * 1024 or len(self.assets) >= 200:
                self.skipped_images += 1
                return None
            data = self.archive.read(info)
        except KeyError:
            self.skipped_images += 1
            return None
        asset = sldc().make_asset(data)
        if asset.id not in self._asset_ids:
            self._asset_ids.add(asset.id)
            self.assets.append(asset)
        alt = (frame.findtext(q("svg", "desc")) or frame.findtext(q("svg", "title")) or "").strip()
        figure = {"asset": asset.id, "alt": alt} if alt else {"asset": asset.id, "alt": "", "decorative": True}
        return sldc().make_block("figure", "", figure=figure)

    def table(self, element: ET.Element) -> None:
        rows: list[list[dict[str, Any]]] = []
        header_rows = 0

        def collect(container: ET.Element, header: bool) -> None:
            nonlocal header_rows
            for row in container:
                if row.tag == q("table", "table-header-rows"):
                    collect(row, True)
                elif row.tag == q("table", "table-row-group"):
                    collect(row, header)
                elif row.tag == q("table", "table-row"):
                    cells: list[dict[str, Any]] = []
                    for cell in row:
                        if cell.tag == q("table", "covered-table-cell"):
                            continue
                        if cell.tag != q("table", "table-cell"):
                            continue
                        parts = []
                        for p in cell.iter():
                            if p.tag in (q("text", "p"), q("text", "h")):
                                text, _, _ = self.inline(p)
                                parts.append(text)
                        item: dict[str, Any] = {"text": "\n".join(parts).strip()}
                        span = cell.get(q("table", "number-columns-spanned"))
                        rspan = cell.get(q("table", "number-rows-spanned"))
                        if span and span.isdigit() and int(span) > 1:
                            item["colspan"] = min(int(span), 256)
                        if rspan and rspan.isdigit() and int(rspan) > 1:
                            item["rowspan"] = int(rspan)
                        repeat = cell.get(q("table", "number-columns-repeated"))
                        times = int(repeat) if repeat and repeat.isdigit() and int(repeat) <= 256 else 1
                        cells.extend(dict(item) for _ in range(times))
                    if cells:
                        rows.append(cells)
                        if header:
                            header_rows += 1

        collect(element, False)
        if not rows:
            return
        from .read_html import _normalise_rows
        rows = _normalise_rows(rows)
        columns = max(sum(c.get("colspan", 1) for c in row) for row in rows)
        if columns > 256 or len(rows) > 10_000:
            return
        self.push(sldc().make_table(rows, header_rows=header_rows, columns=columns))
