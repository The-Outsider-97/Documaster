"""Helpers around the SLDC document model that Documaster uses as its intermediate representation.

The schema is SLDC's (``src/utils/sldc.py``). Nothing here defines a competing structure: these are builders and
readers for the documented fields only (``blocks``, ``spans``, ``styles``, ``sections``, ``references`` ...).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable

from .. import __version__
from ..bridge import sldc

_WS = re.compile(r"\s+")


@dataclass
class IRResult:
    """A document read into the model, with an honest account of how it was read."""
    package: Any                                   # sldc.SLDCPackage
    backend: str                                   # what actually parsed the bytes
    warnings: list[str] = field(default_factory=list)
    features: list[str] = field(default_factory=list)     # what was carried over
    dropped: list[str] = field(default_factory=list)      # what was not
    source_format: str = ""
    pages: int | None = None
    ocr_pages: list[int] = field(default_factory=list)

    def info(self) -> dict[str, Any]:
        return {"backend": self.backend, "warnings": self.warnings, "features": self.features,
                "dropped": self.dropped, "sourceFormat": self.source_format, "pages": self.pages,
                "ocrPages": self.ocr_pages}


def normalise_ws(text: str) -> str:
    return _WS.sub(" ", unicodedata.normalize("NFC", text)).strip()


def import_provenance(source_name: str, backend: str, sha256: str | None, *, source_format: str,
                      ocr_pages: Iterable[int] = ()) -> dict[str, Any]:
    details: dict[str, Any] = {"backend": backend, "source_format": source_format}
    if sha256:
        details["raw_sha256"] = sha256
    pages = list(ocr_pages)
    if pages:
        details["ocr_pages"] = pages
    return sldc().provenance_entry("documaster.import", agent=f"documaster/{__version__}",
                                   source=source_name, **details)


def build_package(title: str, blocks: list[dict[str, Any]], *, language: str = "en",
                  assets: Iterable[Any] = (), styles: dict[str, Any] | None = None,
                  sections: list[dict[str, Any]] | None = None,
                  references: list[dict[str, Any]] | None = None,
                  publication: dict[str, Any] | None = None,
                  provenance: list[dict[str, Any]] | None = None,
                  metadata: dict[str, Any] | None = None) -> Any:
    clean = clamp_title(title)
    package = sldc().new_package(clean, blocks, language=language, assets=assets, styles=styles,
                                 sections=sections, references=references, publication=publication,
                                 provenance=provenance, metadata=metadata)
    return package


def clamp_title(title: str) -> str:
    title = normalise_ws(title or "") or "Untitled"
    return title[:300]


# ------------------------------------------------------------------ plain-text view of the model
def document_text(package: Any) -> str:
    return sldc().extract_text(package, validate=False)


def blocks(package: Any) -> list[dict[str, Any]]:
    return package.document["blocks"]


def text_fingerprint(text: str) -> str:
    """Whitespace/Unicode-normalised text used to compare what was written against what was intended."""
    return normalise_ws(text)


# ------------------------------------------------------------------ inline Markdown -> text + spans
_ESCAPABLE = set("\\`*_{}[]()#+-.!|>~<")


def parse_inline(source: str) -> tuple[str, list[dict[str, Any]]]:
    """Interpret a small, safe inline-Markdown subset (``**bold**``, ``*italic*``, ``~~strike~~``, `` `code` ``,
    ``[text](https://...)``) into plain text and SLDC spans. Anything not understood stays literal."""
    out: list[str] = []
    spans: list[dict[str, Any]] = []
    length = [0]

    def emit(piece: str) -> None:
        out.append(piece)
        length[0] += len(piece)

    markers = (("**", "bold"), ("__", "bold"), ("~~", "strike"), ("*", "italic"), ("_", "italic"))

    def scan(s: str) -> None:
        i, n = 0, len(s)
        while i < n:
            ch = s[i]
            if ch == "\\" and i + 1 < n and s[i + 1] in _ESCAPABLE:
                emit(s[i + 1])
                i += 2
                continue
            if ch == "`":
                end = s.find("`", i + 1)
                if end > i + 1:
                    begin = length[0]
                    emit(s[i + 1:end])
                    spans.append({"type": "format", "start": begin, "end": length[0], "code": True})
                    i = end + 1
                    continue
            matched = False
            for marker, key in markers:
                if not s.startswith(marker, i):
                    continue
                if marker[0] == "_" and i > 0 and s[i - 1].isalnum():
                    continue
                close = _find_close(s, marker, i + len(marker))
                if close is None or close <= i + len(marker):
                    continue
                begin = length[0]
                scan(s[i + len(marker):close])
                if length[0] > begin:
                    spans.append({"type": "format", "start": begin, "end": length[0], key: True})
                i = close + len(marker)
                matched = True
                break
            if matched:
                continue
            if ch == "[":
                m = re.match(r"\[([^\]\n]+)\]\((https?://[^\s)]+|mailto:[^\s)]+)\)", s[i:])
                if m:
                    begin = length[0]
                    scan(m.group(1))
                    if length[0] > begin:
                        spans.append({"type": "link", "start": begin, "end": length[0], "href": m.group(2)})
                    i += m.end()
                    continue
            emit(ch)
            i += 1

    scan(source)
    text = "".join(out)
    return text, _merge_spans(spans, len(text))


def _find_close(s: str, marker: str, start: int) -> int | None:
    idx = s.find(marker, start)
    while idx != -1:
        if idx > start and s[idx - 1] != "\\" and not s[idx - 1].isspace():
            if marker in ("_", "__") and idx + len(marker) < len(s) and s[idx + len(marker)].isalnum():
                idx = s.find(marker, idx + 1)
                continue
            return idx
        idx = s.find(marker, idx + 1)
    return None


def _merge_spans(spans: list[dict[str, Any]], length: int) -> list[dict[str, Any]]:
    """Combine format spans that cover the same range and sort into SLDC's canonical order."""
    formats: dict[tuple[int, int], dict[str, Any]] = {}
    other: list[dict[str, Any]] = []
    for span in spans:
        if not 0 <= span["start"] < span["end"] <= length:
            continue
        if span["type"] == "format":
            key = (span["start"], span["end"])
            merged = formats.setdefault(key, {"type": "format", "start": key[0], "end": key[1]})
            merged.update({k: v for k, v in span.items() if k not in ("type", "start", "end")})
        else:
            other.append(span)
    result = list(formats.values()) + other
    result.sort(key=lambda s: (s["start"], s["end"], s["type"]))
    return result


# ------------------------------------------------------------------ span helpers for writers
def span_segments(text: str, spans: list[dict[str, Any]] | None) -> list[tuple[str, dict[str, Any]]]:
    """Split ``text`` into pieces with the combined formatting that applies to each.

    Each segment's attribute dict may contain: bold, italic, underline, strike, code, superscript, subscript, color,
    background, href, style (character style name), ref (reference id), xref (block id).
    """
    if not spans:
        return [(text, {})] if text else []
    cuts = {0, len(text)}
    for span in spans:
        cuts.add(span["start"])
        cuts.add(span["end"])
    points = sorted(c for c in cuts if 0 <= c <= len(text))
    segments: list[tuple[str, dict[str, Any]]] = []
    for a, b in zip(points, points[1:]):
        if a == b:
            continue
        attrs: dict[str, Any] = {}
        for span in spans:
            if span["start"] <= a and span["end"] >= b:
                kind = span["type"]
                if kind == "format":
                    attrs.update({k: v for k, v in span.items() if k not in ("type", "start", "end")})
                elif kind == "link":
                    attrs["href"] = span["href"]
                elif kind == "style":
                    attrs["style"] = span["style"]
                elif kind == "ref":
                    attrs["ref"] = span["ref"]
                elif kind == "xref":
                    attrs["xref"] = span["target"]
        segments.append((text[a:b], attrs))
    return segments


def heading_level(block: dict[str, Any]) -> int:
    level = (block.get("attributes") or {}).get("level")
    return level if isinstance(level, int) and 1 <= level <= 6 else 1


def has_page_break(block: dict[str, Any]) -> bool:
    return bool((block.get("attributes") or {}).get("page_break_before"))
