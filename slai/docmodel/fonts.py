"""Font discovery for the PDF writer.

PDF output needs a TrueType font that has the document's glyphs. Documaster looks for a sans family (regular, bold, italic,
bold-italic) and a monospace font in the usual system locations, uses a fallback family for symbols the main font lacks,
and CJK TrueType fonts (or Adobe's CID fonts when none is installed) for Han/kana/Hangul. Characters no font covers are
counted and reported, never silently dropped.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

WIN = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
MAC = [Path("/System/Library/Fonts/Supplemental"), Path("/Library/Fonts"), Path.home() / "Library/Fonts"]
LINUX = [Path("/usr/share/fonts/truetype"), Path("/usr/share/fonts/TTF"), Path("/usr/share/fonts"),
         Path("/usr/local/share/fonts"), Path.home() / ".fonts", Path.home() / ".local/share/fonts"]

# (family label, [regular, bold, italic, bolditalic] file names)
SANS = [
    ("Segoe UI", ["segoeui.ttf", "segoeuib.ttf", "segoeuii.ttf", "segoeuiz.ttf"]),
    ("Arial", ["arial.ttf", "arialbd.ttf", "ariali.ttf", "arialbi.ttf", "Arial.ttf", "Arial Bold.ttf", "Arial Italic.ttf", "Arial Bold Italic.ttf"]),
    ("Calibri", ["calibri.ttf", "calibrib.ttf", "calibrii.ttf", "calibriz.ttf"]),
    ("Liberation Sans", ["LiberationSans-Regular.ttf", "LiberationSans-Bold.ttf", "LiberationSans-Italic.ttf", "LiberationSans-BoldItalic.ttf"]),
    ("DejaVu Sans", ["DejaVuSans.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans-Oblique.ttf", "DejaVuSans-BoldOblique.ttf"]),
    ("Noto Sans", ["NotoSans-Regular.ttf", "NotoSans-Bold.ttf", "NotoSans-Italic.ttf", "NotoSans-BoldItalic.ttf"]),
]
SERIF = [
    ("Times New Roman", ["times.ttf", "timesbd.ttf", "timesi.ttf", "timesbi.ttf"]),
    ("Liberation Serif", ["LiberationSerif-Regular.ttf", "LiberationSerif-Bold.ttf", "LiberationSerif-Italic.ttf", "LiberationSerif-BoldItalic.ttf"]),
    ("DejaVu Serif", ["DejaVuSerif.ttf", "DejaVuSerif-Bold.ttf", "DejaVuSerif-Italic.ttf", "DejaVuSerif-BoldItalic.ttf"]),
]
MONO = [
    ("Consolas", ["consola.ttf", "consolab.ttf", "consolai.ttf", "consolaz.ttf"]),
    ("Courier New", ["cour.ttf", "courbd.ttf", "couri.ttf", "courbi.ttf"]),
    ("Liberation Mono", ["LiberationMono-Regular.ttf", "LiberationMono-Bold.ttf", "LiberationMono-Italic.ttf", "LiberationMono-BoldItalic.ttf"]),
    ("DejaVu Sans Mono", ["DejaVuSansMono.ttf", "DejaVuSansMono-Bold.ttf", "DejaVuSansMono-Oblique.ttf", "DejaVuSansMono-BoldOblique.ttf"]),
]
# TrueType (glyf) CJK fonts; reportlab cannot use CFF/OpenType-PostScript fonts such as Noto Sans CJK
CJK_TTF = [("Microsoft YaHei", "msyh.ttc", 0), ("SimSun", "simsun.ttc", 0), ("Malgun Gothic", "malgun.ttf", 0),
           ("Meiryo", "meiryo.ttc", 0), ("Yu Gothic", "YuGothR.ttc", 0), ("PingFang", "PingFang.ttc", 0),
           ("Arial Unicode MS", "ARIALUNI.TTF", 0), ("WenQuanYi Micro Hei", "wqy-microhei.ttc", 0),
           ("Droid Sans Fallback", "DroidSansFallbackFull.ttf", 0), ("AR PL UMing", "uming.ttc", 0)]

_CJK_RANGES = ((0x2E80, 0x2FDF), (0x3000, 0x30FF), (0x3100, 0x312F), (0x31F0, 0x31FF), (0x3400, 0x4DBF), (0x4E00, 0x9FFF),
               (0xAC00, 0xD7AF), (0xF900, 0xFAFF), (0xFF00, 0xFFEF), (0x1100, 0x11FF), (0x20000, 0x2FA1F))
_RTL_RANGES = ((0x0590, 0x08FF), (0xFB1D, 0xFDFF), (0xFE70, 0xFEFF))


def is_cjk(ch: str) -> bool:
    c = ord(ch)
    return any(a <= c <= b for a, b in _CJK_RANGES)


def has_rtl(text: str) -> bool:
    return any(a <= ord(ch) <= b for ch in text for a, b in _RTL_RANGES)


def _dirs() -> list[Path]:
    if sys.platform.startswith("win"):
        return [WIN, Path.home() / "AppData/Local/Microsoft/Windows/Fonts"]
    if sys.platform == "darwin":
        return MAC
    return LINUX


def _find(name: str) -> Path | None:
    for base in _dirs():
        if not base.exists():
            continue
        direct = base / name
        if direct.exists():
            return direct
        try:
            for hit in base.rglob(name):
                return hit
        except (OSError, PermissionError):
            continue
    return None


@dataclass
class Family:
    label: str
    files: list[Path]            # regular, bold, italic, bolditalic (missing variants repeat the closest)


@dataclass
class FontPlan:
    body: Family | None
    mono: Family | None
    fallback: Family | None
    cjk: tuple[str, Path, int] | None
    notes: list[str] = field(default_factory=list)


def find_family(candidates: list[tuple[str, list[str]]], prefer: str | None = None) -> Family | None:
    order = list(candidates)
    if prefer:
        order.sort(key=lambda c: 0 if c[0].lower() == prefer.lower() else 1)
    for label, names in order:
        stride = 4 if len(names) in (4, 8) else 4
        groups = [names[:4]] + ([names[4:8]] if len(names) > 4 else [])
        for group in groups:
            reg = _find(group[0])
            if reg is None:
                continue
            files = [reg]
            for name in group[1:]:
                found = _find(name)
                files.append(found if found else files[0])
            return Family(label, files)
    return None


def plan(prefer_body: str | None = None, serif: bool = False) -> FontPlan:
    notes: list[str] = []
    body = find_family((SERIF if serif else SANS) if not prefer_body else SANS + SERIF, prefer_body)
    if prefer_body and body and body.label.lower() != prefer_body.lower():
        notes.append(f"Font '{prefer_body}' was not found on this machine; using {body.label}")
    mono = find_family(MONO)
    fallback = find_family([c for c in SANS if c[0] in ("DejaVu Sans", "Noto Sans", "Segoe UI", "Arial", "Liberation Sans")])
    cjk = None
    for label, name, index in CJK_TTF:
        found = _find(name)
        if found:
            cjk = (label, found, index)
            break
    return FontPlan(body, mono, fallback, cjk, notes)


def coverage_of(path: Path, index: int = 0) -> set[int]:
    """Code points a TrueType font maps (empty set when it cannot be read)."""
    try:
        from reportlab.pdfbase.ttfonts import TTFont
        font = TTFont("probe", str(path), subfontIndex=index)
        return set(font.face.charToGlyph.keys())
    except Exception:  # noqa: BLE001
        return set()


def missing_chars(text: str, covered: Iterable[set[int]]) -> set[str]:
    sets = list(covered)
    return {ch for ch in set(text) if not ch.isspace() and ord(ch) >= 32 and not any(ord(ch) in s for s in sets)}
