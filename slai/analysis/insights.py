"""Deterministic analyses. Each returns plain data and, where a claim is made about the text, evidence (page, block, offsets)."""
from __future__ import annotations

import difflib
import math
import re
from collections import Counter, defaultdict
from typing import Any

from .. import textstats
from .model import Unit, full_text

_VOWELS = "aeiouy"


# ---------------------------------------------------------------------------------------- outline & structure
def outline(units: list[Unit]) -> list[dict[str, Any]]:
    root: list[dict[str, Any]] = []
    stack: list[dict[str, Any]] = []
    for u in units:
        if u.kind != "heading":
            continue
        node = {"title": u.text.strip(), "level": u.level or 1, "page": u.page, "part": u.part, "blockId": u.block_id, "children": []}
        while stack and stack[-1]["level"] >= node["level"]:
            stack.pop()
        (stack[-1]["children"] if stack else root).append(node)
        stack.append(node)
    return root


def structure_checks(package: Any, units: list[Unit], pages_without_text: list[int] | None = None) -> list[dict[str, Any]]:
    """Findings about how the document is built. Each has a severity (info/warning/problem), a rule id and evidence."""
    doc = package.document
    blocks = doc["blocks"]
    by_id = {u.block_id: u for u in units}
    findings: list[dict[str, Any]] = []

    def add(rule: str, sev: str, msg: str, evid: list[dict[str, Any]] | None = None) -> None:
        findings.append({"rule": rule, "severity": sev, "message": msg, "evidence": (evid or [])[:8]})

    headings = [u for u in units if u.kind == "heading"]
    if not headings:
        add("no-headings", "warning", "The document has no headings, so it has no navigable structure.")
    else:
        if (headings[0].level or 1) > 1:
            add("first-heading-level", "info", f"The first heading is level {headings[0].level}, not level 1.", [headings[0].evidence()])
        prev = 0
        for h in headings:
            lvl = h.level or 1
            if prev and lvl > prev + 1:
                add("heading-skip", "warning", f"Heading level jumps from {prev} to {lvl}.", [h.evidence()])
            prev = lvl
        seen: dict[str, Unit] = {}
        for h in headings:
            key = re.sub(r"\s+", " ", h.text.strip().lower())
            if key in seen:
                add("duplicate-heading", "info", f"The heading '{h.text.strip()[:60]}' appears more than once.",
                    [seen[key].evidence(), h.evidence()])
            seen.setdefault(key, h)
        for h in headings:
            if len(h.text.strip()) > 120:
                add("long-heading", "info", "A heading is longer than 120 characters; it may be a paragraph.", [h.evidence()])
    for b in blocks:
        if b["type"] == "heading" and not b.get("text", "").strip():
            add("empty-heading", "problem", "A heading has no text.", [{"blockId": b["id"]}])
        if b["type"] == "figure":
            fig = b["figure"]
            if not fig.get("decorative") and len(fig.get("alt", "").strip()) < 3:
                add("figure-alt", "warning", "An image has no description (alt text).", [{"blockId": b["id"]}])
        if b["type"] == "table":
            t = b["table"]
            if not t.get("header_rows"):
                u = by_id.get(b["id"])
                add("table-header", "info", "A table has no header row.", [u.evidence(quote=False)] if u else [{"blockId": b["id"]}])
    long_paras = [u for u in units if u.kind == "paragraph" and len(textstats.tokenize(u.text)) > 250]
    if long_paras:
        add("long-paragraph", "info", f"{len(long_paras)} paragraph(s) have more than 250 words.", [u.evidence(0, 160) for u in long_paras])
    ids = {b["id"] for b in blocks}
    for b in blocks:
        for s in b.get("spans", []):
            if s["type"] == "xref" and s["target"] not in ids:
                add("broken-xref", "problem", "A cross-reference points at a block that does not exist.", [{"blockId": b["id"]}])
    links = [(u, s) for b, u in ((b, by_id.get(b["id"])) for b in blocks) if u for s in b.get("spans", []) if s["type"] == "link"]
    insecure = [(u, s) for u, s in links if s["href"].lower().startswith("http://")]
    if insecure:
        add("insecure-link", "info", f"{len(insecure)} link(s) use http:// instead of https://.",
            [u.evidence(s["start"], s["end"]) for u, s in insecure])
    if pages_without_text:
        add("no-text-layer", "warning", f"{len(pages_without_text)} page(s) have no text layer (scanned or image-only): "
            f"{pages_without_text[:12]}. They cannot be searched, counted or read aloud until OCR is applied.")
    if not doc.get("title") or doc.get("title") in ("Untitled", ""):
        add("no-title", "info", "The document has no title.")
    return findings


# ---------------------------------------------------------------------------------------- keywords
def keywords(units: list[Unit], lang: str, top: int = 20) -> list[dict[str, Any]]:
    stops = textstats._STOP_SETS.get(lang, set())
    counts: Counter = Counter()
    where: dict[str, list[Unit]] = defaultdict(list)
    for u in units:
        words = [w.lower() for w in textstats.tokenize(u.text)]
        clean = [w for w in words if len(w) >= 3 and w not in stops and not w.isdigit()]
        for w in clean:
            counts[w] += 1
            if len(where[w]) < 3 and u not in where[w]:
                where[w].append(u)
        for a, b in zip(words, words[1:]):
            if a in stops or b in stops or len(a) < 3 or len(b) < 3 or a.isdigit() or b.isdigit():
                continue
            counts[f"{a} {b}"] += 1
            key = f"{a} {b}"
            if len(where[key]) < 3 and u not in where[key]:
                where[key].append(u)
    ranked = [(t, c) for t, c in counts.most_common(top * 6) if c >= 2 or " " not in t]
    out: list[dict[str, Any]] = []
    used: set[str] = set()
    for term, c in sorted(ranked, key=lambda x: (-(x[1] * (1.6 if " " in x[0] else 1.0)), x[0])):
        parts = term.split()
        if len(parts) == 1 and any(term in o.split() for o in used if " " in o) and c <= counts.get(term, 0):
            if any(counts[o] >= c * 0.8 for o in used if " " in o and term in o.split()):
                continue
        used.add(term)
        out.append({"term": term, "count": c, "evidence": [u.evidence(max(0, u.text.lower().find(parts[0])),
                                                                         max(0, u.text.lower().find(parts[0])) + len(term)) for u in where[term][:2]]})
        if len(out) >= top:
            break
    return out


# ---------------------------------------------------------------------------------------- readability
def _syllables(word: str) -> int:
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return 0
    groups = re.findall(r"[aeiouy]+", w)
    n = len(groups)
    if w.endswith("e") and not w.endswith(("le", "ee")) and n > 1:
        n -= 1
    return max(1, n)


def readability(text: str, lang: str) -> dict[str, Any]:
    spans = textstats.sentence_spans(text)
    words = [w for w in textstats.tokenize(text) if not textstats._CJK_CHAR.fullmatch(w)]
    n_s, n_w = max(len(spans), 1), len(words)
    if n_w < 30:
        return {"available": False, "reason": "Too little text (fewer than 30 words) for a meaningful score"}
    avg_sentence = n_w / n_s
    long_words = sum(1 for w in words if len(w) > 6)
    lix = avg_sentence + 100.0 * long_words / n_w
    out: dict[str, Any] = {"available": True, "words": n_w, "sentences": n_s, "avgWordsPerSentence": round(avg_sentence, 1),
                           "avgWordLength": round(sum(len(w) for w in words) / n_w, 2),
                           "longSentences": sum(1 for a, b in spans if len(textstats.tokenize(text[a:b])) > 30),
                           "lix": round(lix, 1),
                           "lixLevel": ("very easy" if lix < 25 else "easy" if lix < 35 else "average" if lix < 45
                                        else "difficult" if lix < 55 else "very difficult"),
                           "formulas": ["LIX = words/sentences + 100 * long words (>6 letters)/words (Björnsson); language-independent for alphabetic scripts"]}
    if lang == "en":
        syl = sum(_syllables(w) for w in words)
        fre = 206.835 - 1.015 * avg_sentence - 84.6 * (syl / n_w)
        fk = 0.39 * avg_sentence + 11.8 * (syl / n_w) - 15.59
        out.update(fleschReadingEase=round(fre, 1), fleschKincaidGrade=round(fk, 1),
                   fleschLevel=("very easy" if fre >= 90 else "easy" if fre >= 70 else "fairly easy" if fre >= 60 else "standard" if fre >= 50
                                else "fairly difficult" if fre >= 30 else "very difficult"))
        out["formulas"].append("Flesch Reading Ease and Flesch-Kincaid grade (English only; syllables estimated from spelling)")
    else:
        out["note"] = "Flesch scores are defined for English only; LIX is shown for this language."
    return out


# ---------------------------------------------------------------------------------------- extractive summary
def summary(units: list[Unit], lang: str, n: int = 5) -> dict[str, Any]:
    stops = textstats._STOP_SETS.get(lang, set())
    sentences: list[tuple[Unit, int, int, str]] = []
    for u in units:
        if u.kind in ("heading", "code", "table"):
            continue
        for a, b in textstats.sentence_spans(u.text):
            s = u.text[a:b]
            nw = len(textstats.tokenize(s))
            if 6 <= nw <= 60:
                sentences.append((u, a, b, s))
    if not sentences:
        return {"sentences": [], "method": "extractive", "note": "No sentences of suitable length were found."}
    freq: Counter = Counter()
    for _, _, _, s in sentences:
        for w in textstats.tokenize(s):
            lw = w.lower()
            if lw not in stops and len(lw) > 2 and not lw.isdigit():
                freq[lw] += 1
    top = max(freq.values()) if freq else 1
    scored = []
    for i, (u, a, b, s) in enumerate(sentences):
        toks = [w.lower() for w in textstats.tokenize(s) if w.lower() not in stops and len(w) > 2]
        if not toks:
            continue
        score = sum(freq[w] / top for w in toks) / math.sqrt(len(toks))
        score *= 1.0 + 0.25 * (1.0 if a == 0 else 0.0) + 0.15 * (1.0 - i / max(len(sentences), 1))
        scored.append((score, i))
    chosen = sorted(sorted(scored, reverse=True)[:n], key=lambda x: x[1])
    return {"method": "extractive", "note": "These are sentences taken from the document, ranked by how many of the document's main terms they contain.",
            "sentences": [{"text": sentences[i][3], "score": round(s, 3), "evidence": sentences[i][0].evidence(sentences[i][1], sentences[i][2], quote=False)}
                          for s, i in chosen]}


# ---------------------------------------------------------------------------------------- duplicates
def _norm(s: str) -> str:
    return re.sub(r"\W+", " ", s.lower()).strip()


def duplicates(units: list[Unit], min_words: int = 8, near: float = 0.85) -> dict[str, Any]:
    cands = [u for u in units if u.kind in ("paragraph", "list_item", "quote") and len(textstats.tokenize(u.text)) >= min_words]
    exact: dict[str, list[Unit]] = defaultdict(list)
    for u in cands:
        exact[_norm(u.text)].append(u)
    groups = [{"kind": "exact", "text": v[0].text[:200], "count": len(v), "evidence": [x.evidence(quote=False) for x in v[:6]]}
              for v in exact.values() if len(v) > 1]
    seen_exact = {id(x) for v in exact.values() if len(v) > 1 for x in v}
    rest = [u for u in cands if id(u) not in seen_exact]

    def shingles(t: str) -> set[str]:
        w = _norm(t).split()
        return {" ".join(w[i:i + 4]) for i in range(max(1, len(w) - 3))}

    sh = [(u, shingles(u.text)) for u in rest[:1500]]
    inverted: dict[str, list[int]] = defaultdict(list)
    pairs: set[tuple[int, int]] = set()
    for i, (_, s) in enumerate(sh):
        cand: Counter = Counter()
        for g in s:
            for j in inverted[g]:
                cand[j] += 1
        for j, c in cand.items():
            if c / max(1, min(len(s), len(sh[j][1]))) >= near * 0.7:
                inter = len(s & sh[j][1])
                if inter / len(s | sh[j][1]) >= near * 0.8:
                    pairs.add((j, i))
        for g in s:
            inverted[g].append(i)
    for j, i in sorted(pairs)[:200]:
        ratio = difflib.SequenceMatcher(None, _norm(sh[j][0].text), _norm(sh[i][0].text)).ratio()
        if ratio >= near:
            groups.append({"kind": "near", "similarity": round(ratio, 3), "text": sh[j][0].text[:200], "count": 2,
                           "evidence": [sh[j][0].evidence(quote=False), sh[i][0].evidence(quote=False)]})
    return {"groups": groups[:100], "total": len(groups), "note": "Paragraphs of at least %d words; near-duplicates are %d%% or more similar." % (min_words, int(near * 100))}


# ---------------------------------------------------------------------------------------- checklist
PRESETS = {
    "report": ["Summary", "Introduction", "Method", "Results", "Discussion", "Conclusion", "References"],
    "proposal": ["Summary", "Background", "Objectives", "Approach", "Timeline", "Budget", "Risks"],
    "contract": ["Parties", "Definitions", "Term", "Payment", "Confidentiality", "Liability", "Termination", "Governing law", "Signatures"],
    "research paper": ["Abstract", "Introduction", "Related work", "Method", "Results", "Discussion", "Conclusion", "References"],
}
_SYN = {"summary": ["executive summary", "abstract", "overview", "samenvatting"], "method": ["methods", "methodology", "approach", "materials and methods"],
        "conclusion": ["conclusions", "concluding remarks", "slotsom"], "references": ["bibliography", "sources", "works cited", "literature"],
        "introduction": ["intro", "background", "inleiding"], "results": ["findings", "resultaten"], "timeline": ["schedule", "planning", "milestones"],
        "budget": ["costs", "pricing", "financials"], "termination": ["term and termination", "cancellation"], "liability": ["limitation of liability"],
        "governing law": ["applicable law", "jurisdiction"], "signatures": ["signature", "signed"], "definitions": ["definition", "interpretation"]}


def checklist(units: list[Unit], required: list[str]) -> dict[str, Any]:
    heads = [(u, _norm(u.text)) for u in units if u.kind == "heading"]
    items = []
    for req in required:
        r = _norm(req)
        names = [r, *_SYN.get(r, [])]
        best: tuple[float, Unit] | None = None
        how = ""
        for u, h in heads:
            for nme in names:
                sc = 1.0 if h == nme else 0.95 if h.endswith(nme) or h.startswith(nme) and len(h) < len(nme) + 14 else \
                    difflib.SequenceMatcher(None, h, nme).ratio()
                if nme != r and sc >= 0.9:
                    sc = min(sc, 0.9)
                if best is None or sc > best[0]:
                    best, how = (sc, u), "exact" if sc == 1.0 else "similar" if sc >= 0.8 else ""
        found = bool(best and best[0] >= 0.8)
        items.append({"required": req, "found": found, "match": how if found else None,
                      "evidence": [best[1].evidence(quote=True)] if found and best else []})
    missing = [i["required"] for i in items if not i["found"]]
    return {"items": items, "missing": missing, "complete": not missing,
            "note": "Sections are matched by heading text (exact, common synonyms, or at least 80% similar)."}


# ---------------------------------------------------------------------------------------- search
def search(units: list[Unit], query: str, *, regex: bool = False, case: bool = False, limit: int = 300,
           whole_word: bool = False, page_from: int | None = None, page_to: int | None = None) -> list[dict[str, Any]]:
    """Locate bounded matches with source offsets. Page filters use logical parts for non-PDFs.

    User-supplied regexes must run through the timeout-capable `regex` package; Python's
    standard `re` has no execution time limit and is unsafe for untrusted patterns.
    """
    if not query or len(query) > 500 or limit < 1 or limit > 1000:
        raise ValueError("Invalid search text or result limit")
    flags = 0 if case else re.IGNORECASE
    expression = query if regex else re.escape(query)
    if whole_word:
        expression = rf"(?<!\w)(?:{expression})(?!\w)"
    if regex:
        try:
            import regex as safe_regex
        except ImportError as exc:
            raise ValueError("Pattern search requires the optional 'regex' package") from exc
        pattern = safe_regex.compile(expression, flags)
    else:
        pattern = re.compile(expression, flags)
    hits: list[dict[str, Any]] = []
    for u in units:
        loc = u.page or u.part
        if (page_from is not None and loc < page_from) or (page_to is not None and loc > page_to):
            continue
        matches = pattern.finditer(u.text, timeout=0.05) if regex else pattern.finditer(u.text)
        for m in matches:
            if m.end() == m.start():
                continue
            a = max(0, m.start() - 60)
            hits.append({"evidence": u.evidence(m.start(), m.end(), quote=False), "match": m.group(),
                         "context": u.text[a:m.end() + 60].replace("\n", " ")})
            if len(hits) >= limit:
                return hits
    return hits
