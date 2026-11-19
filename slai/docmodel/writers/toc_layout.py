"""Where the parts of a table-of-contents entry go: the text, the leader and the page number.

One small engine, used by every writer that draws an entry itself (PDF) or has to describe it (Word, OpenDocument, RTF tab stops). Writer's editor runs the same
arithmetic (``src/client/toc_layout.ts``); a golden file ties the two together, so the editing view and the exported pages place an entry alike.

An entry is a line of text (it may wrap), a page number and the leader between them. Nothing here measures a font: the caller gives the widths it measured, in points,
and gets positions back, in points from the left edge of the text frame.

* ``position`` says where the page number is: ``right`` (a column at the right margin), ``left`` (a column at the left margin, the text after it), ``custom`` (a
  column set at a tab stop), ``inline`` (right after the text) or ``hidden`` (none).
* ``number_align`` is how the number sits in its column: ``left``, ``center`` or ``right``. The column is as wide as the widest page number of the table (or the
  width the person set), so the numbers line up whatever way they are aligned.
* The text keeps the alignment of its paragraph (left, centred or right) inside the room the number leaves it.
* The leader fills the gap between the text and the number: it starts right after the text (the last line of it, or the first line when the number comes first),
  ends right before the number, and its marks lie on a grid that starts at the column, so they line up from one entry to the next. A leader of 0 marks is
  nothing: it never draws under a number or over the text.
"""
from __future__ import annotations

import math
from typing import Any

POSITIONS = ("right", "left", "inline", "custom", "hidden")
ALIGNS = ("left", "center", "right")
LEADERS = ("none", "dots", "dashes", "solid", "custom")
LEGACY_POSITION = {"right": "right", "inline": "inline", "left": "left"}
#: The room kept clear between the text, the leader and the number, in points.
PAD_PT = 3.0
#: The text of an entry is never given less than this, however far the number column reaches into the line.
MIN_TEXT_PT = 36.0
#: How far a number that follows the text stands from it, in ems.
INLINE_GAP_EM = 0.7
MM_PT = 72 / 25.4


def resolve_look(raw: Any, *, show_page_numbers: bool = True, leader_dots: bool = True) -> dict[str, Any]:
    """The settings of an entry's layout, every one of them with a value. ``raw`` is Writer's ``slai.writer.toc_style`` of the table (or nothing: the native look)."""
    raw = raw if isinstance(raw, dict) else {}
    position = raw.get("page_position")
    if position not in POSITIONS:
        position = LEGACY_POSITION.get(raw.get("page_align"), "right")
    if not show_page_numbers:
        position = "hidden"
    leader = raw.get("leader") if raw.get("leader") in LEADERS else ("dots" if leader_dots else "none")
    align = raw.get("number_align")
    if align not in ALIGNS:
        align = "left" if position in ("left", "inline") else "right"
    char = raw.get("leader_char")
    char = char if isinstance(char, str) and 0 < len(char) <= 3 and char.isprintable() and not char.isspace() else "."
    gap = raw.get("leader_gap_pt")
    gap = float(gap) if isinstance(gap, (int, float)) and not isinstance(gap, bool) and 0 <= gap <= 40 else None
    tab = raw.get("custom_tab_mm")
    tab = float(tab) if isinstance(tab, (int, float)) and not isinstance(tab, bool) and 0 <= tab <= 600 else None
    column = raw.get("number_column_mm")
    column = float(column) if isinstance(column, (int, float)) and not isinstance(column, bool) and 0 < column <= 100 else None
    return {"position": position, "number_align": align, "leader": leader if position not in ("hidden", "inline") else "none", "leader_char": char, "leader_gap_pt": gap,
            "custom_tab_pt": None if tab is None else tab * MM_PT, "number_column_pt": None if column is None else column * MM_PT}


def leader_mark(look: dict[str, Any]) -> str:
    """The character a leader of dots, dashes or the person's own is made of (a solid leader is a rule, not characters)."""
    return {"dots": ".", "dashes": "-", "custom": look["leader_char"]}.get(look["leader"], "")


def leader_gap_pt(look: dict[str, Any], font_size: float) -> float:
    """The room between two marks of the leader: what the person set, or the look's own (dashes breathe, dots touch their advance)."""
    if look["leader_gap_pt"] is not None:
        return look["leader_gap_pt"]
    return {"dashes": 0.35 * font_size, "dots": 0.0, "custom": 0.0}.get(look["leader"], 0.0)


def plan_entry(look: dict[str, Any], width: float, indent: float, right_indent: float, column_w: float, inline_w: float = 0.0, inline_gap: float = 0.0) -> dict[str, Any]:
    """The room an entry's text has and where its number column is. ``width`` is the text frame, ``indent`` and ``right_indent`` those of the paragraph,
    ``column_w`` the width of the number column (the widest page number), ``inline_w`` the width of the number that follows the text (position ``inline``)."""
    position = look["position"]
    edge = width - right_indent
    column: tuple[float, float] | None = None
    if position == "hidden":
        text = (indent, edge)
    elif position == "right":
        column = (edge - column_w, edge)
        text = (indent, column[0] - PAD_PT)
    elif position == "left":
        column = (0.0, column_w)
        text = (column_w + PAD_PT + indent, edge)
    elif position == "custom":
        tab = look["custom_tab_pt"] if look["custom_tab_pt"] is not None else edge - column_w
        tab = min(max(tab, 0.0), edge)
        if look["number_align"] == "right":
            c0 = tab - column_w
        elif look["number_align"] == "left":
            c0 = tab
        else:
            c0 = tab - column_w / 2
        c0 = max(0.0, min(c0, edge - column_w))
        column = (c0, c0 + column_w)
        text = (indent, column[0] - PAD_PT)
    else:                                                                                  # inline: the number follows the text, so the text leaves room for it
        text = (indent, edge - inline_w - inline_gap)
    if text[1] - text[0] < MIN_TEXT_PT:
        text = (text[0], text[0] + MIN_TEXT_PT)
    return {"position": position, "text": text, "column": column}


def place_entry(look: dict[str, Any], plan: dict[str, Any], number_w: float, first: tuple[float, float], last: tuple[float, float], mark_w: float, gap: float,
                inline_gap: float = 0.0) -> dict[str, Any]:
    """Where this entry's page number and leader go, given where its text ended up: ``first`` and ``last`` are the left and right ends of its first and of its last line."""
    position = plan["position"]
    out: dict[str, Any] = {"number_x": None, "leader": None}
    if position == "hidden":
        return out
    if position == "inline":
        out["number_x"] = last[1] + inline_gap
        return out
    c0, c1 = plan["column"]
    align = look["number_align"]
    number_x = c0 if align == "left" else c0 + (c1 - c0 - number_w) / 2 if align == "center" else c1 - number_w
    out["number_x"] = number_x
    kind = look["leader"]
    if kind == "none":
        return out
    if position == "left":                                                                 # the number comes first: the leader runs from it to the text on the first line
        g0, g1 = number_x + number_w + PAD_PT, first[0] - PAD_PT
    else:                                                                                  # the number comes last: the leader runs from the text on the last line to it
        g0, g1 = last[1] + PAD_PT, number_x - PAD_PT
    if g1 - g0 < 1.0:
        return out
    if kind == "solid":
        out["leader"] = {"kind": "solid", "x0": g0, "x1": g1}
        return out
    pitch = mark_w + gap
    if mark_w <= 0 or pitch <= 0:
        return out
    if position == "left":
        anchor = c0                                                                        # a mark starts here and every pitch after it
        k_min, k_max = math.ceil((g0 - anchor) / pitch - 1e-9), math.floor((g1 - mark_w - anchor) / pitch + 1e-9)
        first_x = anchor + k_min * pitch
    else:
        anchor = c1 - PAD_PT                                                               # a mark ends here and every pitch before it
        k_min, k_max = math.ceil((anchor - g1) / pitch - 1e-9), math.floor((anchor - mark_w - g0) / pitch + 1e-9)
        first_x = anchor - k_max * pitch - mark_w
    count = k_max - k_min + 1
    if count >= 1:
        out["leader"] = {"kind": kind, "x0": first_x, "count": count, "pitch": pitch, "mark_w": mark_w}
    return out


def tab_plan(look: dict[str, Any], width: float, indent: float, right_indent: float, column_w: float, text_align: str = "left") -> dict[str, Any]:
    """The same arrangement for programs that lay a line out with tab stops (Word, RTF): the order of the parts of an entry and the tab stop each tab goes to.

    ``sequence`` lists ``"text"``, ``"number"``, ``"tab"`` and ``"gap"`` (a few spaces) in the order they are written; the n-th ``"tab"`` goes to ``stops[n]``, a
    ``{"x": points from the left margin, "align": "left" | "center" | "right", "leader": bool}``. Only the tab that crosses the gap between the text and the number
    carries the leader. Such programs cannot wrap a heading around a number column or space the marks of a leader, so those two are left to them."""
    plan = plan_entry(look, width, indent, right_indent, column_w, 0.0, 0.0)
    position = plan["position"]
    text0, text1 = plan["text"]
    align = text_align if text_align in ALIGNS else "left"
    if position == "hidden":
        return {"sequence": ["text"], "stops": [], "position": position}
    if position == "inline":
        return {"sequence": ["text", "gap", "number"], "stops": [], "position": position}
    c0, c1 = plan["column"]
    number_stop = {"x": c0 if look["number_align"] == "left" else (c0 + c1) / 2 if look["number_align"] == "center" else c1, "align": look["number_align"], "leader": True}
    if position == "left":
        stops: list[dict[str, Any]] = []
        sequence: list[str] = []
        if look["number_align"] != "left":                                        # the number is set inside its column, so it needs a tab of its own to get there
            sequence.append("tab")
            stops.append({**number_stop, "leader": False})
        sequence += ["number", "tab", "text"]
        stops.append({"x": text0 if align == "left" else text1 if align == "right" else (text0 + text1) / 2, "align": align, "leader": True})
        return {"sequence": sequence, "stops": stops, "position": position}
    if align == "left":
        return {"sequence": ["text", "tab", "number"], "stops": [number_stop], "position": position}
    first = {"x": text1 if align == "right" else (text0 + text1) / 2, "align": align, "leader": False}
    return {"sequence": ["tab", "text", "tab", "number"], "stops": [first, number_stop], "position": position}


#: The leader kinds the Word formats know, by the name of ours.
WORD_LEADER = {"dots": "dot", "dashes": "hyphen", "solid": "underscore", "none": None}


def word_leader(look: dict[str, Any]) -> str | None:
    """Word's name for the leader of this look: a character of the person's own becomes the middle dot when it is one and dots otherwise (Word has no other)."""
    if look["leader"] == "custom":
        return "middleDot" if look["leader_char"] in ("·", "•", "∙") else "dot"
    return WORD_LEADER.get(look["leader"])
