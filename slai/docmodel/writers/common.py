"""Pieces every writer shares: list grouping and numbering, table grids, notes/citations, resolved page setup."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

from ...bridge import sldc

# characters that XML 1.0 (and therefore DOCX/ODT/HTML-as-XML) cannot carry
_BAD_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")

PAGE_SIZES_MM = {"A3": (297.0, 420.0), "A4": (210.0, 297.0), "A5": (148.0, 210.0), "B5": (176.0, 250.0),
                 "Letter": (215.9, 279.4), "Legal": (215.9, 355.6), "Tabloid": (279.4, 431.8)}
DEFAULT_MARGIN_MM = 25.0


def clean(text: str) -> str:
    return _BAD_XML.sub("", text)


@dataclass
class PageSetup:
    width_mm: float = 210.0
    height_mm: float = 297.0
    top: float = DEFAULT_MARGIN_MM
    right: float = DEFAULT_MARGIN_MM
    bottom: float = DEFAULT_MARGIN_MM
    left: float = DEFAULT_MARGIN_MM
    columns: int = 1
    column_gap_mm: float = 8.0


def page_setup(section: dict[str, Any] | None, default: PageSetup | None = None) -> PageSetup:
    base = default or PageSetup()
    if not section or "page" not in section:
        out = PageSetup(**{**base.__dict__})
    else:
        page = section["page"]
        if "size" in page:
            w, h = PAGE_SIZES_MM[page["size"]]
        else:
            w, h = page["width_mm"], page["height_mm"]
        if page.get("orientation") == "landscape":
            w, h = h, w
        m = page.get("margins_mm", {})
        out = PageSetup(w, h, m.get("top", base.top), m.get("right", base.right), m.get("bottom", base.bottom),
                        m.get("left", base.left))
    if section:
        out.columns = int(section.get("columns", 1))
        out.column_gap_mm = float(section.get("column_gap_mm", 8.0))
    return out


def resolve_header_footer(template: dict[str, Any] | None, title: str, page: int, pages: int) -> tuple[str, str, str]:
    if not template:
        return "", "", ""

    def fill(text: str) -> str:
        text = text.replace("{{", "\x01").replace("}}", "\x02")
        text = text.replace("{page}", str(page)).replace("{pages}", str(pages)).replace("{title}", title)
        return text.replace("\x01", "{").replace("\x02", "}")

    return fill(template.get("left", "")), fill(template.get("center", "")), fill(template.get("right", ""))


def format_number(n: int, fmt: str) -> str:
    if fmt == "lower-roman":
        return _roman(n).lower()
    if fmt == "upper-roman":
        return _roman(n)
    if fmt in ("lower-alpha", "upper-alpha"):
        s = ""
        k = n
        while k > 0:
            k, r = divmod(k - 1, 26)
            s = chr(97 + r) + s
        return s.upper() if fmt == "upper-alpha" else s
    return str(n)


def _roman(n: int) -> str:
    out = ""
    for value, sym in ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"), (50, "L"),
                       (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
        while n >= value:
            out += sym
            n -= value
    return out


# ------------------------------------------------------------------ blocks grouping
def list_labels(items: list[dict[str, Any]]) -> list[str]:
    """Marker text for a run of consecutive list items: ``•`` or ``1.`` / ``2.`` per depth (restarts when depth drops)."""
    counters: dict[int, int] = {}
    labels: list[str] = []
    previous_depth = 0
    for item in items:
        attrs = item.get("attributes") or {}
        depth = int(attrs.get("depth", 0))
        for deeper in [d for d in counters if d > depth]:
            del counters[deeper]
        if attrs.get("ordered"):
            counters[depth] = counters.get(depth, 0) + 1
            number = counters[depth]
            labels.append(f"{number}." if depth % 2 == 0 else f"{format_number(number, 'lower-alpha')}.")
        else:
            counters.pop(depth, None)
            labels.append("•" if depth % 2 == 0 else "–")
        previous_depth = depth
    return labels


def group_blocks(blocks: Iterable[dict[str, Any]]) -> Iterator[tuple[str, Any]]:
    """Yield ``("list", [items])`` for runs of list items and ``("block", block)`` for everything else."""
    run: list[dict[str, Any]] = []
    for block in blocks:
        if block["type"] == "list_item":
            run.append(block)
            continue
        if run:
            yield "list", run
            run = []
        yield "block", block
    if run:
        yield "list", run


@dataclass
class Grid:
    rows: list[list[dict[str, Any] | None]]   # cell dict at its origin, None where covered by a span
    header_rows: int
    columns: int


def table_grid(table: dict[str, Any]) -> Grid:
    columns = table["columns"]
    carry = [0] * columns
    rows: list[list[dict[str, Any] | None]] = []
    for row in table["rows"]:
        line: list[dict[str, Any] | None] = [None] * columns
        occupied = [c > 0 for c in carry]
        col = 0
        new_carry = [max(c - 1, 0) for c in carry]
        for cell in row:
            while col < columns and occupied[col]:
                col += 1
            line[col] = cell
            span, rspan = cell.get("colspan", 1), cell.get("rowspan", 1)
            for c in range(col, col + span):
                occupied[c] = True
                new_carry[c] = rspan - 1
            col += span
        rows.append(line)
        carry = new_carry
    return Grid(rows, int(table.get("header_rows", 0)), columns)


# ------------------------------------------------------------------ references (footnotes and citations)
@dataclass
class Notes:
    """Footnotes are numbered in order of first use; citations are listed once each."""
    references: dict[str, dict[str, Any]]
    note_numbers: dict[str, int] = field(default_factory=dict)
    cited: list[str] = field(default_factory=list)

    @classmethod
    def of(cls, doc: dict[str, Any]) -> "Notes":
        return cls({r["id"]: r for r in doc.get("references", [])})

    def use(self, ref_id: str) -> tuple[str, int | None]:
        ref = self.references.get(ref_id)
        if ref is None:
            return "missing", None
        if ref["kind"] == "footnote":
            return "footnote", self.note_numbers.setdefault(ref_id, len(self.note_numbers) + 1)
        if ref_id not in self.cited:
            self.cited.append(ref_id)
        return "citation", self.cited.index(ref_id) + 1

    def citation_label(self, ref_id: str) -> str:
        return f"[{self.cited.index(ref_id) + 1}]" if ref_id in self.cited else "[?]"

    def footnotes(self) -> list[tuple[int, str]]:
        return sorted((n, self.references[r]["text"]) for r, n in self.note_numbers.items())

    def citation_lines(self) -> list[str]:
        out = []
        for i, ref_id in enumerate(self.cited, 1):
            r = self.references[ref_id]
            who = ", ".join(r.get("authors", []))
            year = f" ({r['year']})" if "year" in r else ""
            tail = ". ".join(str(r[k]) for k in ("container", "publisher", "volume", "issue", "pages", "doi", "url") if k in r)
            parts = [p for p in (f"{who}{year}".strip(), r["title"], tail) if p]
            out.append(f"[{i}] " + ". ".join(parts))
        return out


def has_text(block: dict[str, Any]) -> bool:
    return bool(block.get("text", "").strip()) or block["type"] in ("table", "figure")


def sections_in_order(doc: dict[str, Any]) -> list[dict[str, Any]]:
    return list(doc.get("sections", []))


def style_table(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Paragraph styles with ``based_on`` chains resolved (child values win)."""
    paragraph = doc.get("styles", {}).get("paragraph", {})
    resolved: dict[str, dict[str, Any]] = {}

    def resolve(name: str) -> dict[str, Any]:
        if name in resolved:
            return resolved[name]
        props = dict(paragraph.get(name, {}))
        parent = props.pop("based_on", None)
        merged = {**resolve(parent), **props} if parent else props
        resolved[name] = merged
        return merged

    for name in paragraph:
        resolve(name)
    return resolved


def char_style_table(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    char = doc.get("styles", {}).get("character", {})
    resolved: dict[str, dict[str, Any]] = {}

    def resolve(name: str) -> dict[str, Any]:
        if name in resolved:
            return resolved[name]
        props = dict(char.get(name, {}))
        parent = props.pop("based_on", None)
        resolved[name] = {**resolve(parent), **props} if parent else props
        return resolved[name]

    for name in char:
        resolve(name)
    return resolved


def asset_bytes(package: Any, asset_id: str) -> tuple[bytes, str] | None:
    asset = package.assets.get(asset_id)
    return (asset.data, asset.media_type) if asset else None


def table_text(block: dict[str, Any]) -> list[list[str]]:
    grid = table_grid(block["table"])
    return [["" if c is None else c["text"] for c in row] for row in grid.rows]


def sldc_text(package: Any) -> str:
    return sldc().extract_text(package, validate=False)
