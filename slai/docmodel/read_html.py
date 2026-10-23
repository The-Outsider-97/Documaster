"""HTML -> document model. Structure (headings, paragraphs, lists, tables, code, quotes, inline formatting, links,
embedded PNG/JPEG data URIs) is kept. Nothing is executed or fetched: scripts, styles, iframes and remote images are
ignored (and counted in the warnings)."""
from __future__ import annotations

import base64
import re
from html.parser import HTMLParser
from typing import Any

from ..bridge import sldc

_BLOCKS = {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "td", "th", "figcaption", "dt", "dd"}
_SKIP = {"script", "style", "noscript", "template", "iframe", "object", "embed", "svg", "canvas", "head"}
_CELL_FLOW = {"p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "section",
              "article", "figcaption", "dt", "dd"}
_VOID = {"br", "img", "hr", "meta", "link", "input", "wbr", "col", "area", "base", "source", "track"}
_DATA_URI = re.compile(r"^data:(image/(?:png|jpeg|gif|webp));base64,([A-Za-z0-9+/=\s]+)$", re.I)
_SAFE_HREF = re.compile(r"^(https?://|mailto:)", re.I)
_BREAK_BEFORE = re.compile(r"page-break-before\s*:\s*always|break-before\s*:\s*page", re.I)


class _Inline:
    """Accumulates the text of one block together with its formatting spans."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.length = 0
        self.spans: list[dict[str, Any]] = []

    def add(self, text: str) -> None:
        self.parts.append(text)
        self.length += len(text)

    def text(self) -> str:
        return "".join(self.parts)


class _Reader(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.lang: str | None = None
        self.blocks: list[dict[str, Any]] = []
        self.assets: list[Any] = []
        self.warnings: set[str] = set()
        self.cur: _Inline | None = None
        self.kind = "paragraph"
        self.attrs: dict[str, Any] = {}
        self.skip_depth = 0
        self.in_title = False
        self.list_stack: list[dict[str, Any]] = []
        self.pre_depth = 0
        self.quote_depth = 0
        self.open_spans: list[tuple[str, int, dict[str, Any]]] = []
        self.table: dict[str, Any] | None = None
        self.next_break = False
        self.asset_count = 0
        self.dropped_images = 0
        self.stack: list[str] = []
        self.in_cell = False

    # ---- block helpers
    def _start(self, kind: str, attrs: dict[str, Any] | None = None) -> None:
        self._end()
        self.cur, self.kind, self.attrs = _Inline(), kind, dict(attrs or {})

    def _end(self) -> None:
        if self.cur is None:
            return
        self.open_spans = []  # a formatting tag left open across a block boundary applies to the first block only
        text = self.cur.text()
        if self.kind != "code":
            lead = len(text) - len(text.lstrip())
            stripped = text.strip()
            shift = lead
            spans = []
            for span in self.cur.spans:
                a, b = max(span["start"] - shift, 0), min(span["end"] - shift, len(stripped))
                if a < b:
                    spans.append({**span, "start": a, "end": b})
            text, self.cur.spans = stripped, spans
        if self.table is not None and self.kind == "cell":
            self.table["cell"] = (text.replace("\n", " ").strip(), self.table["cell_meta"])
        elif text.strip():
            block = sldc().make_block(self.kind, text, attributes=self._block_attrs(),
                                      spans=sorted(self.cur.spans, key=lambda s: (s["start"], s["end"], s["type"])) or None)
            self.blocks.append(block)
            self.next_break = False
        self.cur = None

    def _block_attrs(self) -> dict[str, Any]:
        attrs = dict(self.attrs)
        if self.next_break and self.kind != "cell":
            attrs["page_break_before"] = True
        return attrs

    def _ensure(self, kind: str = "paragraph") -> _Inline:
        if self.cur is None:
            attrs: dict[str, Any] = {}
            if self.list_stack and kind == "paragraph":
                kind = "list_item"
                attrs = {"ordered": self.list_stack[-1]["ordered"]}
                if len(self.list_stack) > 1:
                    attrs["depth"] = len(self.list_stack) - 1
            elif self.quote_depth and kind == "paragraph":
                kind = "quote"
            self._start(kind, attrs)
        assert self.cur is not None
        return self.cur

    # ---- parser callbacks
    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {k: (v or "") for k, v in attrs_list}
        if tag in _SKIP:
            if tag != "head":
                self.warnings.add(f"<{tag}> content was ignored (it is never executed or fetched)")
            if tag not in _VOID:
                self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag == "html" and attrs.get("lang"):
            self.lang = attrs["lang"].split("-")[0][:8] if re.fullmatch(r"[A-Za-z0-9-]{2,12}", attrs["lang"]) else None
        if tag == "title":
            self.in_title = True
            return
        if self.in_cell and tag in _CELL_FLOW:
            if self.cur is not None and self.cur.length and not self.cur.text().endswith("\n"):
                self.cur.add("\n")
            return
        style = attrs.get("style", "")
        if style and _BREAK_BEFORE.search(style):
            self._end()
            self.next_break = True
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._start("heading", {"level": int(tag[1])})
        elif tag in ("ul", "ol"):
            self._end()
            self.list_stack.append({"ordered": tag == "ol"})
        elif tag == "li":
            self._start("list_item", {"ordered": bool(self.list_stack and self.list_stack[-1]["ordered"]),
                                      **({"depth": len(self.list_stack) - 1} if len(self.list_stack) > 1 else {})})
        elif tag == "pre":
            self._start("code", {})
            self.pre_depth += 1
        elif tag == "code" and not self.pre_depth:
            self._open_span(tag, {"code": True})
        elif tag == "blockquote":
            self._end()
            self.quote_depth += 1
        elif tag in ("p", "div", "figcaption", "dt", "dd", "section", "article"):
            self._end()
        elif tag == "table":
            self._end()
            self.table = {"rows": [], "row": None, "cell": None, "cell_meta": {}, "header_rows": 0, "caption": "", "in_head": False}
        elif tag in ("thead",) and self.table is not None:
            self.table["in_head"] = True
        elif tag == "tr" and self.table is not None:
            self.table["row"] = []
        elif tag in ("td", "th") and self.table is not None:
            self._end()
            meta: dict[str, Any] = {}
            for key in ("colspan", "rowspan"):
                value = attrs.get(key, "")
                if value.isdigit() and 1 <= int(value) <= 100:
                    meta[key] = int(value)
            if tag == "th":
                meta["header"] = True
            self.table["cell_meta"] = meta
            self._start("cell")
            self.in_cell = True
        elif tag == "br":
            self._ensure().add("\n")
        elif tag == "hr":
            self._end()
        elif tag in ("strong", "b"):
            self._open_span(tag, {"bold": True})
        elif tag in ("em", "i", "cite", "var"):
            self._open_span(tag, {"italic": True})
        elif tag in ("u", "ins"):
            self._open_span(tag, {"underline": True})
        elif tag in ("s", "strike", "del"):
            self._open_span(tag, {"strike": True})
        elif tag == "sup":
            self._open_span(tag, {"superscript": True})
        elif tag == "sub":
            self._open_span(tag, {"subscript": True})
        elif tag == "a":
            href = attrs.get("href", "").strip()
            if _SAFE_HREF.match(href) and len(href) < 2000:
                self._open_span(tag, {"__href": href})
            else:
                self._open_span(tag, {})
        elif tag == "img":
            self._image(attrs)
        if tag not in _VOID:
            self.stack.append(tag)

    def _open_span(self, tag: str, props: dict[str, Any]) -> None:
        inline = self._ensure()
        self.open_spans.append((tag, inline.length, props))

    def _close_span(self, tag: str) -> None:
        for index in range(len(self.open_spans) - 1, -1, -1):
            name, start, props = self.open_spans[index]
            if name == tag:
                del self.open_spans[index]
                if self.cur is not None and self.cur.length > start and props:
                    if "__href" in props:
                        self.cur.spans.append({"type": "link", "start": start, "end": self.cur.length, "href": props["__href"]})
                    else:
                        self.cur.spans.append({"type": "format", "start": start, "end": self.cur.length, **props})
                return

    def _image(self, attrs: dict[str, str]) -> None:
        src = attrs.get("src", "")
        match = _DATA_URI.match(src.strip())
        if not match:
            self.dropped_images += 1
            return
        try:
            data = base64.b64decode(re.sub(r"\s+", "", match.group(2)), validate=True)
        except Exception:  # noqa: BLE001
            self.dropped_images += 1
            return
        if len(data) > 8 * 1024 * 1024 or self.asset_count >= 200:
            self.dropped_images += 1
            return
        self._end()
        self.asset_count += 1
        asset = sldc().make_asset(data)
        self.assets.append(asset)
        alt = attrs.get("alt", "").strip()
        figure = {"asset": asset.id, "alt": alt} if alt else {"asset": asset.id, "alt": "", "decorative": True}
        self.blocks.append(sldc().make_block("figure", "", figure=figure))

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            if self.skip_depth:
                self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        if tag == "title":
            self.in_title = False
            return
        if self.in_cell and tag in _CELL_FLOW:
            return
        if tag in ("strong", "b", "em", "i", "cite", "var", "u", "ins", "s", "strike", "del", "sup", "sub", "a", "code"):
            self._close_span(tag)
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6", "li", "p", "div", "figcaption", "dt", "dd", "section", "article"):
            if not (self.table is not None and tag in ("p", "div")):
                self._end()
            if tag == "li":
                pass
        elif tag == "pre":
            self._end()
            self.pre_depth = max(0, self.pre_depth - 1)
        elif tag in ("ul", "ol"):
            self._end()
            if self.list_stack:
                self.list_stack.pop()
        elif tag == "blockquote":
            self._end()
            self.quote_depth = max(0, self.quote_depth - 1)
        elif tag in ("td", "th") and self.table is not None:
            self._end()
            self.in_cell = False
            cell = self.table.get("cell")
            if cell is not None and self.table["row"] is not None:
                text, meta = cell
                self.table["row"].append({"text": text, **{k: v for k, v in meta.items() if k != "header" or v}})
                self.table["cell"] = None
        elif tag == "tr" and self.table is not None:
            self._end()
            if self.table["row"]:
                self.table["rows"].append((self.table["row"], self.table["in_head"]))
            self.table["row"] = None
        elif tag == "thead" and self.table is not None:
            self.table["in_head"] = False
        elif tag == "caption" and self.table is not None:
            self._end()
        elif tag == "table" and self.table is not None:
            self._finish_table()

    def _finish_table(self) -> None:
        table, self.table = self.table, None
        assert table is not None
        rows = [r for r, _ in table["rows"]]
        if not rows:
            return
        head = sum(1 for _, in_head in table["rows"] if in_head)
        if not head and rows and all(c.get("header") for c in rows[0]):
            head = 1
        rows = _normalise_rows(rows)
        for row in rows:
            for cell in row:
                cell.pop("header", None)
        if not rows or len(rows) > 10_000:
            return
        columns = max(sum(c.get("colspan", 1) for c in row) for row in rows)
        if columns > 256:
            self.warnings.add("A table with more than 256 columns was skipped")
            return
        self.blocks.append(sldc().make_table(rows, header_rows=head, columns=columns))

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        if self.in_title:
            self.title += data
            return
        if self.pre_depth:
            inline = self._ensure("code")
            inline.add(data)
            return
        if not data.strip() and (self.cur is None or not self.cur.parts):
            return
        text = re.sub(r"\s+", " ", data)
        inline = self._ensure()
        if inline.parts and inline.parts[-1].endswith(" ") and text.startswith(" "):
            text = text[1:]
        inline.add(text)

    def close(self) -> None:  # type: ignore[override]
        super().close()
        self._end()
        if self.table is not None:
            self._finish_table()


def _normalise_rows(rows: list[list[dict[str, Any]]]) -> list[list[dict[str, Any]]]:
    """Make ragged HTML rows satisfy SLDC's rectangular-table rule: place cells on a grid (honouring row/column
    spans), pad free grid positions with empty cells and clip spans that would run past the table."""
    cover: list[int] = []
    starts: list[dict[int, dict[str, Any]]] = []
    columns = 0
    for r, row in enumerate(rows):
        col = 0
        here: dict[int, dict[str, Any]] = {}
        occupied = [v > 0 for v in cover]
        for cell in row:
            while col < len(occupied) and occupied[col]:
                col += 1
            span = cell.get("colspan", 1)
            rowspan = min(cell.get("rowspan", 1), len(rows) - r)
            if rowspan > 1:
                cell["rowspan"] = rowspan
            else:
                cell.pop("rowspan", None)
            if len(occupied) < col + span:
                occupied += [False] * (col + span - len(occupied))
            here[col] = cell
            for c in range(col, col + span):
                occupied[c] = True
            col += span
        columns = max(columns, len(occupied))
        new_cover = [max(v - 1, 0) for v in cover] + [0] * (len(occupied) - len(cover))
        for c0, cell in here.items():
            for c in range(c0, c0 + cell.get("colspan", 1)):
                new_cover[c] = cell.get("rowspan", 1) - 1
        cover = new_cover
        starts.append(here)
    cover = [0] * columns
    result: list[list[dict[str, Any]]] = []
    for here in starts:
        occupied = [v > 0 for v in cover]
        out: list[dict[str, Any]] = []
        c = 0
        while c < columns:
            if occupied[c]:
                c += 1
            elif c in here:
                out.append(here[c])
                c += here[c].get("colspan", 1)
            else:
                out.append({"text": ""})
                c += 1
        new_cover = [max(v - 1, 0) for v in cover]
        for c0, cell in here.items():
            for k in range(c0, c0 + cell.get("colspan", 1)):
                new_cover[k] = cell.get("rowspan", 1) - 1
        cover = new_cover
        result.append(out)
    return result


def read_html_bytes(data: bytes) -> dict[str, Any]:
    """Decode and parse HTML; returns ``{"title","lang","blocks","assets","warnings"}``."""
    text = _decode(data)
    parser = _Reader()
    parser.feed(text)
    parser.close()
    warnings = sorted(parser.warnings)
    if parser.dropped_images:
        warnings.append(f"{parser.dropped_images} image(s) were not imported (only embedded PNG/JPEG data is read; "
                        "nothing is downloaded)")
    return {"title": re.sub(r"\s+", " ", parser.title).strip(), "lang": parser.lang, "blocks": parser.blocks,
            "assets": parser.assets, "warnings": warnings}


def _decode(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    head = data[:2048].decode("ascii", "ignore")
    match = re.search(r"charset\s*=\s*['\"]?([A-Za-z0-9_-]+)", head, re.I)
    for encoding in ([match.group(1)] if match else []) + ["utf-8", "cp1252"]:
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("latin-1")
