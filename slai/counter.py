"""Word Counter: statistics for pasted text or any open document, per document and per page/section.

Everything is computed locally with the rules in ``textstats`` (documented in the user guide): CJK ideographs and kana count as
one word each, Hangul and the other spaced scripts count words between spaces, hyphenated and apostrophised words are one word.
PDF counts come from the text layer; a scanned page without text counts as zero words and is listed as such.
"""
from __future__ import annotations

from typing import Any

from . import textstats
from .docmodel import ir as irmod
from .docmodel.sldc_merge import logical_pages
from .errors import ValidationFailed
from .services import NO_CONTROL, JobControl


def count_text(text: str, *, top: int = 15, goals: dict[str, int] | None = None) -> dict[str, Any]:
    if len(text) > 50_000_000:
        raise ValidationFailed("That text is too long to count in one go")
    stats = textstats.basic_counts(text)
    lang = textstats.detect_language(text)
    stats["language"] = lang
    stats["topTerms"] = [{"term": t, "count": c} for t, c in textstats.top_terms(text, lang.get("code", "und"), limit=top)]
    if goals:
        stats["goals"] = _goals(stats, goals)
    return stats


def _goals(stats: dict[str, Any], goals: dict[str, int]) -> list[dict[str, Any]]:
    labels = {"minWords": ("words", ">="), "maxWords": ("words", "<="), "minCharacters": ("characters", ">="),
              "maxCharacters": ("characters", "<="), "maxCharactersNoSpaces": ("charactersNoSpaces", "<="),
              "maxSentences": ("sentences", "<="), "minSentences": ("sentences", ">=")}
    out = []
    for key, limit in goals.items():
        if key not in labels:
            raise ValidationFailed(f"Unknown goal {key!r}", details={"goals": sorted(labels)})
        if not isinstance(limit, int) or limit < 0:
            raise ValidationFailed(f"Goal {key} must be a whole number")
        field, op = labels[key]
        value = stats[field]
        ok = value >= limit if op == ">=" else value <= limit
        out.append({"goal": key, "limit": limit, "value": value, "met": ok})
    return out


def count_document(svc: Any, ws: Any, doc: Any, *, top: int = 15, goals: dict[str, int] | None = None,
                   ocr: bool = False, ocr_lang: str = "eng", control: JobControl = NO_CONTROL) -> dict[str, Any]:
    control.report(0.05, f"Reading {doc.name}")
    result = ws.ir(doc, control, ocr=ocr, ocr_lang=ocr_lang)
    package = result.package
    text = irmod.document_text(package)
    control.report(0.6, "Counting")
    stats = count_text(text, top=top, goals=goals)
    pages = logical_pages(package, 0)
    real_pages = doc.format == "pdf"
    breakdown: list[dict[str, Any]] = []
    blocks = package.document["blocks"]
    for p in pages:
        chunk = "\n\n".join(b.get("text", "") for b in blocks[p.start:p.end] if b.get("text"))
        c = textstats.basic_counts(chunk)
        breakdown.append({"kind": "page" if real_pages else "part", "index": p.index + 1, "label": p.label,
                          "words": c["words"], "characters": c["characters"], "sentences": c["sentences"]})
    headings = [{"level": int((b.get("attributes") or {}).get("level", 1)), "text": b.get("text", "")} for b in blocks
                if b["type"] == "heading"][:300]
    empty_pages = [b["index"] for b in breakdown if real_pages and b["words"] == 0]
    notes = list(result.warnings)
    if empty_pages:
        notes.append(f"{len(empty_pages)} page(s) have no text layer (scanned or image-only): {empty_pages[:15]}. "
                     "They count as 0 words unless OCR is switched on.")
    stats.update({"document": {"name": doc.name, "format": doc.format, "pages": doc.pages if real_pages else None,
                               "backend": result.backend},
                  "breakdown": breakdown, "headings": headings, "notes": notes, "ocrPages": result.ocr_pages,
                  "tables": sum(1 for b in blocks if b["type"] == "table"),
                  "images": sum(1 for b in blocks if b["type"] == "figure")})
    control.report(1.0, "Done")
    return stats


def totals(items: list[dict[str, Any]]) -> dict[str, Any]:
    keys = ("words", "characters", "charactersNoSpaces", "sentences", "paragraphs", "letters", "digits")
    out = {k: sum(i.get(k, 0) for i in items) for k in keys}
    out["readingMinutes"] = round(out["words"] / textstats.READ_WPM, 1) if out["words"] else 0.0
    out["speakingMinutes"] = round(out["words"] / textstats.SPEAK_WPM, 1) if out["words"] else 0.0
    out["documents"] = len(items)
    return out
