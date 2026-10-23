"""Page geometry: the visible page box, its rotation, and the mapping between what the user sees and PDF user space.

The UI works in *normalised display coordinates*: ``(0, 0)`` is the top-left of the page as displayed (after ``/Rotate``),
``(1, 1)`` the bottom-right. These functions turn them into the unrotated user-space coordinates PDF content needs.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence


@dataclass(frozen=True)
class PageGeometry:
    x0: float
    y0: float
    width: float        # unrotated width of the visible box
    height: float
    rotate: int         # 0, 90, 180, 270 (clockwise, as displayed)

    @property
    def display_size(self) -> tuple[float, float]:
        return (self.height, self.width) if self.rotate in (90, 270) else (self.width, self.height)


def geometry_of(page: Any) -> PageGeometry:
    box = page.cropbox if page.get("/CropBox") is not None else page.mediabox
    x0, y0 = float(box.left), float(box.bottom)
    w, h = float(box.right) - x0, float(box.top) - y0
    rotate = int(page.get("/Rotate", 0) or 0) % 360
    if rotate % 90:
        rotate = 0
    return PageGeometry(x0, y0, abs(w), abs(h), rotate)


def to_user(u: float, v: float, g: PageGeometry) -> tuple[float, float]:
    """Normalised display point -> user-space point."""
    if g.rotate == 0:
        x, y = u * g.width, (1 - v) * g.height
    elif g.rotate == 90:
        x, y = v * g.width, u * g.height
    elif g.rotate == 180:
        x, y = (1 - u) * g.width, v * g.height
    else:
        x, y = (1 - v) * g.width, (1 - u) * g.height
    return g.x0 + x, g.y0 + y


def rect_to_user(rect: Sequence[float], g: PageGeometry) -> tuple[float, float, float, float]:
    """Normalised display rectangle ``[left, top, right, bottom]`` -> user-space ``(x0, y0, x1, y1)`` (normalised order)."""
    l, t, r, b = rect
    (xa, ya), (xb, yb) = to_user(l, t, g), to_user(r, b, g)
    return min(xa, xb), min(ya, yb), max(xa, xb), max(ya, yb)


def from_user(x: float, y: float, g: PageGeometry) -> tuple[float, float]:
    """User-space point -> normalised display point (inverse of :func:`to_user`)."""
    x, y = x - g.x0, y - g.y0
    if g.rotate == 0:
        return x / g.width, 1 - y / g.height
    if g.rotate == 90:
        return y / g.height, x / g.width
    if g.rotate == 180:
        return 1 - x / g.width, y / g.height
    return 1 - y / g.height, 1 - x / g.width


def clamp_rect(rect: Sequence[float]) -> list[float]:
    if len(rect) != 4 or not all(isinstance(v, (int, float)) for v in rect):
        raise ValueError("a rectangle is four numbers [left, top, right, bottom]")
    l, t, r, b = (min(1.0, max(0.0, float(v))) for v in rect)
    l, r = min(l, r), max(l, r)
    t, b = min(t, b), max(t, b)
    if r - l < 1e-4 or b - t < 1e-4:
        raise ValueError("the rectangle is empty")
    return [l, t, r, b]


def hex_colour(value: str | Sequence[float] | None, default: tuple[float, float, float]) -> tuple[float, float, float]:
    if value is None:
        return default
    if isinstance(value, str):
        v = value.lstrip("#")
        if len(v) == 3:
            v = "".join(c * 2 for c in v)
        if len(v) != 6:
            raise ValueError("colours look like #rrggbb")
        return tuple(int(v[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]
    if len(value) == 3:
        return tuple(float(c) for c in value)  # type: ignore[return-value]
    raise ValueError("colours look like #rrggbb")
