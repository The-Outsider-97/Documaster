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
    #: Book layout (Writer's ``slai.writer.book_layouts``). ``left`` and ``right`` above are the margins of a *right-hand* page (page 1 and every odd page) with
    #: the gutter already included once; ``mirror`` says that a left-hand page (every even one) has them the other way round. ``gutter`` is informational.
    mirror: bool = False
    #: Mirrored sections swap what sits at the left and right of the header and footer on left-hand pages (the page number stays on the outer edge).
    mirror_edges: bool = True
    gutter: float = 0.0
    gutter_position: str = "left"
    #: Odd and even pages with their own running heads (Writer's ``book_layouts[...].odd_even``): left-hand pages (every even one) use ``even_header`` and
    #: ``even_footer`` where given, and the ordinary header and footer (edges swapped in a mirrored section) where not.
    odd_even: bool = False
    even_header: dict[str, Any] | None = None
    even_footer: dict[str, Any] | None = None
    #: The stored inside and outside margins (what the person typed, before the gutter), for writers that have fields for them.
    inside: float = DEFAULT_MARGIN_MM
    outside: float = DEFAULT_MARGIN_MM

    def left_for(self, physical: int) -> float:
        """The left margin of the page at this 1-based position in the document (never its printed number)."""
        return self.right if self.mirror and physical % 2 == 0 else self.left

    def right_for(self, physical: int) -> float:
        return self.left if self.mirror and physical % 2 == 0 else self.right


def book_of(doc: dict[str, Any] | None, section_id: str | None) -> dict[str, Any] | None:
    """Writer's book layout of a section (mirrored margins, gutter), or None."""
    books = (((doc or {}).get("extensions") or {}).get("slai.writer") or {}).get("book_layouts") or {}
    entry = books.get(section_id) if section_id else None
    return entry if isinstance(entry, dict) else None


def swap_edges(template: dict[str, Any] | None) -> dict[str, Any] | None:
    """A header or footer as it reads on a left-hand page of a book: what sat at the left sits at the right and the other way round (text, formats, runs)."""
    if not template:
        return template
    flip = {"left": "right", "right": "left"}
    out = {k: v for k, v in template.items() if k not in ("formats", "runs") and k not in flip}
    for key in flip:
        if key in template:
            out[flip[key]] = template[key]
    for group in ("formats", "runs"):
        if isinstance(template.get(group), dict):
            out[group] = {flip.get(k, k): v for k, v in template[group].items()}
    return out


def even_page_template(edge: str, template: dict[str, Any] | None, setup: PageSetup) -> dict[str, Any] | None:
    """The header or footer of a left-hand (even) page. A section with its own even-page text uses it as written, in the typography of the same position on
    the ordinary band; otherwise the ordinary band is used, with its left and right swapped in a mirrored section so the page number stays on the outer edge."""
    own = (setup.even_header if edge == "header" else setup.even_footer) if setup.odd_even else None
    if own:
        out = {k: v for k, v in own.items() if k in ("left", "center", "right") and v}
        if template and template.get("formats"):
            out["formats"] = template["formats"]
        return out or None
    return swap_edges(template) if setup.mirror and setup.mirror_edges else template


def uses_facing_pages(doc: dict[str, Any] | None, section_ids: list[str]) -> bool:
    """True when any section is mirrored or has odd and even pages: Word, ODT and RTF keep right-hand and left-hand running heads document-wide."""
    for sid in section_ids:
        book = book_of(doc, sid) or {}
        if book.get("mirror") is True or book.get("odd_even") is True:
            return True
    return False


def page_setup(section: dict[str, Any] | None, default: PageSetup | None = None, book: dict[str, Any] | None = None) -> PageSetup:
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
    if book:
        gutter = float(book.get("gutter_mm", 0) or 0)
        position = book.get("gutter_position", "left")
        out.inside, out.outside = out.left, out.right
        out.gutter, out.gutter_position = gutter, position
        if book.get("odd_even") is True:
            out.odd_even = True
            out.even_header = book.get("header_even") if isinstance(book.get("header_even"), dict) else None
            out.even_footer = book.get("footer_even") if isinstance(book.get("footer_even"), dict) else None
        if book.get("mirror") is True:
            out.mirror = True
            out.mirror_edges = book.get("mirror_edges") is not False
            if position != "top":
                out.left += gutter                    # a right-hand page: the inside is on its left
        elif position == "right":
            out.right += gutter
        elif position != "top":
            out.left += gutter
        if position == "top":
            out.top += gutter
    else:
        out.inside, out.outside = out.left, out.right
    return out


def column_widths(widths: Any, setup: PageSetup) -> list[float] | None:
    """Unequal column widths scaled to fill the text area (what is left after the gaps), or None when the widths are not usable.

    Word takes the widths literally, LibreOffice fills the line with them; writing widths that already fill it gives both the same page."""
    if not isinstance(widths, list) or len(widths) != setup.columns or setup.columns < 2:
        return None
    try:
        values = [float(w) for w in widths]
    except (TypeError, ValueError):
        return None
    total = sum(values)
    usable = setup.width_mm - setup.left - setup.right - setup.column_gap_mm * (setup.columns - 1)
    if total <= 0 or usable <= 0 or any(v <= 0 for v in values):
        return None
    return [v * usable / total for v in values]


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


TABLE_LOOK = "slai.writer.table_look"

RULE = "slai.writer.rule"
_RULE_DEFAULTS: dict[str, Any] = {"style": "solid", "width_pt": 1.0, "color": "#404040", "space_before_pt": 6.0, "space_after_pt": 6.0}


def rule_look(block: dict[str, Any]) -> dict[str, Any] | None:
    """The look of a horizontal rule (``slai.writer.rule``, written by SLAI Writer), complete and clamped, or None when the block is not a rule.

    A rule is an empty paragraph that carries the extension, so a reader that does not know it shows an empty paragraph. Every writer calls this one function so
    PDF, Word, OpenDocument, RTF, HTML and the text formats agree on thickness, colour, style and spacing."""
    raw = (block.get("extensions") or {}).get(RULE)
    if not isinstance(raw, dict) or block.get("type") != "paragraph" or (block.get("text") or "").strip():
        return None

    def number(key: str, low: float, high: float) -> float:
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
            return float(_RULE_DEFAULTS[key])
        return float(min(high, max(low, value)))
    colour = raw.get("color")
    return {
        "style": raw.get("style") if raw.get("style") in ("solid", "dashed", "dotted", "double") else "solid",
        "width_pt": number("width_pt", 0.25, 12.0),
        "color": colour if isinstance(colour, str) and len(colour) == 7 and hex_rgb(colour) is not None else _RULE_DEFAULTS["color"],
        "space_before_pt": number("space_before_pt", 0.0, 72.0),
        "space_after_pt": number("space_after_pt", 0.0, 72.0),
    }


def table_look(block: dict[str, Any], grid: Grid) -> dict[str, Any] | None:
    """The resolved look Writer attached to a table for export (fills, borders, widths, heights), or None when there is none or it does not fit the grid.

    Documaster does not resolve table styles itself: the application that owns them hands over the result, and a table without it is drawn with the
    writer's plain grid, as before."""
    look = (block.get("extensions") or {}).get(TABLE_LOOK)
    if not isinstance(look, dict) or look.get("columns") != grid.columns or look.get("rows") != len(grid.rows):
        return None
    if not all(isinstance(look.get(k), dict) for k in ("cells", "h", "v")):
        return None
    return look


def hex_rgb(value: Any) -> tuple[float, float, float] | None:
    """``#rrggbb`` as 0..1 floats, or None."""
    if isinstance(value, str) and len(value) == 7 and value[0] == "#":
        try:
            return int(value[1:3], 16) / 255, int(value[3:5], 16) / 255, int(value[5:7], 16) / 255
        except ValueError:
            return None
    return None


def border_runs(look: dict[str, Any], rows: int, columns: int) -> list[tuple[str, int, int, int, dict[str, Any]]]:
    """The borders of a table as runs of equal neighbouring edges: ``("h", line, first column, last column, border)`` and ``("v", line, first row, last row, border)``."""
    out: list[tuple[str, int, int, int, dict[str, Any]]] = []
    for axis, size, count, lines in (("h", columns, rows + 1, look["h"]), ("v", rows, columns + 1, look["v"])):
        for line in range(count):
            run: tuple[int, int, dict[str, Any]] | None = None
            for index in range(size):
                key = f"{line},{index}" if axis == "h" else f"{index},{line}"
                spec = lines.get(key)
                if spec is not None and run is not None and run[1] == index - 1 and run[2] == spec:
                    run = (run[0], index, spec)
                    continue
                if run is not None:
                    out.append((axis, line, run[0], run[1], run[2]))
                run = (index, index, spec) if spec is not None else None
            if run is not None:
                out.append((axis, line, run[0], run[1], run[2]))
    return out


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
