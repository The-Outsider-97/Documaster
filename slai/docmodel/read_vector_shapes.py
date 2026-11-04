"""Bounded OOXML WordprocessingShape -> editable Writer shape import.

Only known DrawingML preset geometries are reconstructed. Unknown shapes are
reported, not invented. No SVG, macros, external relationships, CSS or scripts
are executed. The data-only SLDC extension is compatible with Writer v2.4.
"""
from __future__ import annotations

import math
import re
from typing import Any

from .writers.vector_shapes import SHAPE_KEY, _WORD_PRESETS

WORD = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
DRAW = "http://schemas.openxmlformats.org/drawingml/2006/main"
WPS = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
W = "{%s}" % WORD
A = "{%s}" % DRAW
S = "{%s}" % WPS
P = "{%s}" % WP
PRESETS = {v: k for k, v in _WORD_PRESETS.items()}
COLOUR = re.compile(r"^[0-9A-Fa-f]{6}$")


def _child(root: Any, *parts: str) -> Any | None:
    for part in parts:
        if root is None:
            return None
        root = root.find(part)
    return root


def _decimal(value: Any, low: float, high: float, default: float) -> float:
    try:
        n = float(value)
    except (ValueError, TypeError, OverflowError):
        return default
    return n if math.isfinite(n) and low <= n <= high else default


def _color(root: Any, default: str) -> tuple[str, float]:
    node = _child(root, A + "srgbClr")
    if node is None or not COLOUR.fullmatch(node.get("val", "")):
        return default, 1.0
    alpha = _child(node, A + "alpha")
    return "#" + node.get("val").lower(), _decimal(
        alpha.get("val") if alpha is not None else None, 0, 100000, 100000) / 100000


def _text_runs(shape: Any, limit: int = 4000) -> tuple[dict[str, Any], list[str]]:
    result = {"content": "", "color": "#171717", "font_size_pt": 12,
              "bold": False, "italic": False, "align": "center", "padding_mm": 2,
              "fit": "keep", "spans": []}
    warnings: list[str] = []
    contents = _child(shape, S + "txbx", W + "txbxContent")
    padding = _child(shape, S + "bodyPr")
    if padding is not None:
        result["padding_mm"] = _decimal(padding.get("lIns"), 0, 1_800_000, 72000) / 36000
    if contents is None:
        return result, warnings
    pieces: list[str] = []
    spans: list[dict[str, Any]] = []
    position = 0
    for paragraph in list(contents):
        if paragraph.tag != W + "p":
            continue
        if pieces:
            pieces.append("\n")
            position += 1
        align = _child(paragraph, W + "pPr", W + "jc")
        if align is not None and align.get(W + "val") in ("left", "center", "right"):
            result["align"] = align.get(W + "val")
        for element in paragraph:
            if element.tag != W + "r":
                continue
            props = element.find(W + "rPr")
            attrs: dict[str, Any] = {}
            if props is not None:
                if props.find(W + "b") is not None:
                    attrs["bold"] = True
                if props.find(W + "i") is not None:
                    attrs["italic"] = True
                if props.find(W + "u") is not None:
                    attrs["underline"] = True
                col = props.find(W + "color")
                if col is not None and COLOUR.fullmatch(col.get(W + "val", "")):
                    attrs["color"] = "#" + col.get(W + "val").lower()
                size = props.find(W + "sz")
                if size is not None:
                    attrs["font_size_pt"] = _decimal(size.get(W + "val"), 10, 288, 24) / 2
                family = props.find(W + "rFonts")
                if family is not None:
                    name = family.get(W + "ascii")
                    if (isinstance(name, str) and 1 <= len(name) <= 100
                            and not any(c in name for c in "\x00\r\n<>;{}")):
                        attrs["font_family"] = name
            run = []
            for child in element:
                if child.tag == W + "t":
                    run.append(child.text or "")
                elif child.tag == W + "br":
                    run.append("\n")
                elif child.tag == W + "tab":
                    run.append("\t")
            part = "".join(run)
            if not part:
                continue
            room = max(0, limit-position)
            if len(part) > room:
                warnings.append("Word shape text was longer than the Writer shape limit and could not be imported in full")
                part = part[:room]
            if not part:
                break
            end = position + len(part.encode("utf-16-le", errors="surrogatepass")) // 2
            pieces.append(part)
            if attrs:
                spans.append({"type": "format", "start": position, "end": end, **attrs})
            position = end
            if len(spans) > 300:
                warnings.append("Excessive Word shape formatting spans were merged or omitted")
                spans = spans[:300]
    result["content"] = "".join(pieces)[:limit]
    result["spans"] = [s for s in spans if s["end"] <=
                       len(result["content"].encode("utf-16-le", errors="surrogatepass")) // 2]
    return result, warnings


def parse_shape(element: Any, margin_left: float = 25.0, margin_top: float = 25.0
                ) -> tuple[dict[str, Any] | None, list[str]]:
    """Parse a wp:inline/wp:anchor; unsupported geometry is not converted."""
    warn: list[str] = []
    graphic = element.find(".//" + S + "wsp")
    if graphic is None:
        return None, warn
    sppr = graphic.find(S + "spPr")
    geom = _child(sppr, A + "prstGeom")
    kind = PRESETS.get(geom.get("prst", "")) if geom is not None else None
    if kind is None:
        return None, ["Unsupported Word DrawingML preset geometry retained only in the original DOCX"]
    extent = element.find(P + "extent")
    if extent is None:
        return None, ["Word shape has no dimensions and could not be imported"]
    w = _decimal(extent.get("cx"), 72000, 36_000_000, -1) / 36000
    h = _decimal(extent.get("cy"), 72000, 36_000_000, -1) / 36000
    if w < 2 or h < 2:
        return None, ["Word shape has unsupported dimensions"]
    transform = _child(sppr, A + "xfrm")
    rotation = _decimal(transform.get("rot") if transform is not None else None,
                        -21_600_000, 21_600_000, 0) / 60000
    fillnode = _child(sppr, A + "solidFill")
    fill_color, fill_alpha = _color(fillnode, "#f1d24b")
    fill = {"enabled": fillnode is not None and kind != "line",
            "color": fill_color, "opacity": fill_alpha}
    ln = _child(sppr, A + "ln")
    lnfill = _child(ln, A + "solidFill")
    lcolor, lalpha = _color(lnfill, "#202020")
    dash = _child(ln, A + "prstDash")
    style = {"dash": "dashed", "dot": "dotted"}.get(
        dash.get("val") if dash is not None else "", "solid")
    width = _decimal(ln.get("w") if ln is not None else None, 1270, 914400, 19050)/12700
    outline = {"enabled": ln is not None and lnfill is not None,
               "color": lcolor, "opacity": lalpha, "width_pt": width, "style": style}
    text, textwarn = _text_runs(graphic)
    warn.extend(textwarn)
    text["padding_mm"] = min(text["padding_mm"], w/4, h/4)
    shadows: list[dict[str, Any]] = []
    shadow = _child(sppr, A + "effectLst", A + "outerShdw")
    if shadow is not None:
        dist = _decimal(shadow.get("dist"), 0, 3_600_000, 0)/36000
        theta = _decimal(shadow.get("dir"), 0, 21_600_000, 0)/60000*math.pi/180
        c, a = _color(shadow, "#555555")
        shadows.append({"enabled": True, "color": c, "opacity": a,
                        "offset_x_mm": max(-100,min(100,dist*math.cos(theta))),
                        "offset_y_mm": max(-100,min(100,dist*math.sin(theta))),
                        "blur_mm": _decimal(shadow.get("blurRad"), 0, 1_080_000, 0)/36000,
                        "spread_mm": 0})
    shape = {"version": 1, "kind": kind, "width_mm": w, "height_mm": h,
             "rotation_deg": rotation, "opacity": 1, "fill": fill, "outline": outline,
             "corners_mm": [0, 0, 0, 0], "text": text, "shadows": shadows, "z_index": 0}
    if element.tag == P + "anchor":
        x_node = _child(element, P + "positionH", P + "posOffset")
        y_node = _child(element, P + "positionV", P + "posOffset")
        x = _decimal(x_node.text if x_node is not None else None,-180_000_000,180_000_000,
                     margin_left*36000)/36000-margin_left
        y = _decimal(y_node.text if y_node is not None else None,-180_000_000,180_000_000,
                     margin_top*36000)/36000-margin_top
        behind = element.get("behindDoc") == "1"
        wrapped = element.find(P + "wrapTopAndBottom") is not None or element.find(P + "wrapSquare") is not None
        if wrapped:
            warn.append("Word shape text wrapping requires block anchoring; imported as page overlay")
        shape["layout"] = {"mode": "floating", "anchor": "page",
                           "x_mm": max(-5000,min(5000,x)), "y_mm": max(-5000,min(5000,y)),
                           "wrap": "none", "layer": "behind_text" if behind else "in_front_of_text"}
        z = _decimal(element.get("relativeHeight"),0,5_000_000,1_000_000)
        shape["z_index"] = max(-1000,min(1000,round((z-1_000_000)/100)))
    else:
        shape["layout"] = {"mode": "inline"}
    return shape, warn


def shapes_in_paragraph(paragraph: Any, left: float = 25, top: float = 25
                        ) -> tuple[list[dict[str, Any]], list[str]]:
    shapes: list[dict[str, Any]] = []
    warnings: list[str] = []
    for element in paragraph._p.iter():
        if element.tag not in (P + "inline",P + "anchor"):
            continue
        shape, notice = parse_shape(element, left, top)
        warnings.extend(notice)
        if shape is not None:
            shapes.append(shape)
    return shapes[:200], warnings + (
        ["More than 200 Word shapes in one paragraph; excess shapes were not imported"]
        if len(shapes)>200 else [])
