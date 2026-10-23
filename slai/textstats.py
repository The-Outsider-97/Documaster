"""Deterministic text statistics: tokenising, word/character/sentence counts, reading time, language guess.

Counting rules (also shown to the user):

* A *word* is a run of letters/digits that may contain an inner apostrophe or hyphen (``don't``, ``well-known``); a number keeps
  its inner decimal point or thousands separator (``3.14``, ``1,250,000``).
* Han ideographs and Japanese kana are written without spaces, so each such character counts as one word (the
  convention Microsoft Word uses). Hangul is written with spaces and is counted as words.
* Scripts without spaces that are not covered (Thai, Lao, Khmer, Myanmar) are counted per space-separated run,
  which under-counts; the report says so when such text is present.
* A *sentence* ends at ``. ! ? 。 ！ ？ …`` followed by space/end, except after a short list of common abbreviations: titles
  (``Dr.``) never end a sentence, other abbreviations (``etc.``, ``p.m.``) only when the next word starts in lower case.
* Reading time uses 238 words per minute (silent reading, adult average), speaking time 150.

The language guess is a heuristic (script plus stop-word profile). It is labelled as a guess with a confidence and
returns ``und`` when the evidence is thin.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from typing import Any, Iterable

_CJK = r"㐀-䶿一-鿿豈-﫿぀-ゟ゠-ヿㇰ-ㇿ\U00020000-\U0002fa1f"
_NOSPACE = r"฀-໿က-႟ក-៿"
_WORD = re.compile(rf"[{_CJK}]|[{_NOSPACE}]+|(?<![^\W_])\d+(?:[.,]\d+)+(?![^\W_])|[^\W_]+(?:['’\-][^\W_]+)*", re.UNICODE)
_CJK_CHAR = re.compile(rf"[{_CJK}]")
_NOSPACE_CHAR = re.compile(rf"[{_NOSPACE}]")
_SENT_END = re.compile(r"[.!?…]+[\"'”’)\]]*(?:\s+|$)|[。！？]+[\"'”’)\]」』）]*\s*")
_TITLES = {"mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "mt"}
_ABBREV = _TITLES | {"etc", "e.g", "i.e", "fig", "no", "inc", "ltd", "co", "cf", "al", "approx", "dept", "est", "vol", "pp", "ca", "a.m", "p.m"}
_TAIL_WORD = re.compile(r"[^\W\d_]+(?:\.[^\W\d_]+)*$")


def _abbreviation_dot(text: str, before: str, m: "re.Match[str]") -> bool:
    """True if the full stop matched by ``m`` belongs to an abbreviation rather than ending a sentence."""
    if not m.group().strip().startswith("."):
        return False
    tail = _TAIL_WORD.search(before)
    word = tail.group().lower() if tail else ""
    if word not in _ABBREV:
        return False
    if word in _TITLES:
        return True
    nxt = text[m.end():m.end() + 1]
    return bool(nxt) and not nxt.isupper()

READ_WPM = 238
SPEAK_WPM = 150


def tokenize(text: str) -> list[str]:
    return _WORD.findall(text)


def count_sentences(text: str) -> int:
    text = text.strip()
    if not text:
        return 0
    count = 0
    last = 0
    for m in _SENT_END.finditer(text):
        before = text[last:m.start()].rstrip()
        if _abbreviation_dot(text, before, m):
            continue
        if before:
            count += 1
        last = m.end()
    if text[last:].strip():
        count += 1
    return count


def sentence_spans(text: str) -> list[tuple[int, int]]:
    """Sentence boundaries as ``(start, end)`` offsets into ``text`` (same rules as :func:`count_sentences`)."""
    spans: list[tuple[int, int]] = []
    last = 0

    def push(a: int, b: int) -> None:
        while a < b and text[a].isspace():
            a += 1
        while b > a and text[b - 1].isspace():
            b -= 1
        if b > a:
            spans.append((a, b))

    for m in _SENT_END.finditer(text):
        before = text[last:m.start()].rstrip()
        if _abbreviation_dot(text, before, m):
            continue
        if before:
            push(last, m.end())
        last = m.end()
    push(last, len(text))
    return spans


def count_paragraphs(text: str) -> int:
    return len([p for p in re.split(r"\n\s*\n", text.replace("\r", "")) if p.strip()])


def _categories(text: str) -> Counter:
    c: Counter = Counter()
    for ch in text:
        cat = unicodedata.category(ch)[0]
        c[cat] += 1
    return c


def basic_counts(text: str) -> dict[str, Any]:
    words = tokenize(text)
    cats = _categories(text)
    no_space = sum(1 for ch in text if not ch.isspace())
    wc = len(words)
    unique = len({w.lower() for w in words})
    lengths = [len(w) for w in words if not _CJK_CHAR.fullmatch(w)]
    return {
        "words": wc,
        "uniqueWords": unique,
        "characters": len(text),
        "charactersNoSpaces": no_space,
        "letters": cats.get("L", 0),
        "digits": cats.get("N", 0),
        "punctuation": cats.get("P", 0),
        "sentences": count_sentences(text),
        "paragraphs": count_paragraphs(text),
        "lines": len(text.splitlines()) if text else 0,
        "avgWordLength": round(sum(lengths) / len(lengths), 2) if lengths else 0.0,
        "longestWord": max(words, key=len) if words else "",
        "cjkCharacters": len(_CJK_CHAR.findall(text)),
        "unspacedScriptRuns": len(re.findall(rf"[{_NOSPACE}]+", text)),
        "readingMinutes": round(wc / READ_WPM, 1) if wc else 0.0,
        "speakingMinutes": round(wc / SPEAK_WPM, 1) if wc else 0.0,
    }


# ------------------------------------------------------------------ language guess
_STOP = {
    "en": "the of and to in is that it for was on with as be at by this are from or an have not but they his which you we their has had were been can will would there what all if more when who so no out up one about into than them its our also may any these other some could only then new such over most".split(),
    "nl": "de het een en van ik te dat die in is niet op zijn met voor ze aan er maar om ook dan als wat bij nog uit naar hij haar wij over door heeft hebben werd worden kan zou deze dit veel meer tot onder zonder wel geen".split(),
    "de": "der die das und ist nicht zu den von mit sich des auf für ein eine dem im auch als an es werden aus er hat dass sie nach wird bei einer um am sind noch wie einem über einen so zum war haben nur oder aber vor zur bis mehr durch man sein wurde".split(),
    "fr": "le la les de des du un une et est en que qui dans pour pas au sur se ce il elle ne plus par avec son sont ou mais comme nous vous leur été être a cette ses tout fait faire aux sans entre aussi très bien".split(),
    "es": "el la los las de del un una y es en que por con para no se su al lo como más pero sus le ya o este sí porque esta entre cuando muy sin sobre también me hasta hay donde quien desde todo nos durante".split(),
    "it": "il lo la i gli le di del della un una e è che in per con non si da su come più ma al alla sono anche questo questa dei delle nel nella ha ho essere fra tra suo loro tutto molto dove quando senza".split(),
    "pt": "o a os as de do da dos das um uma e é que em para com não se por mais como mas ao na no foi são tem ser seu sua ou também já entre quando muito sem sobre pelo pela está até isso esse essa".split(),
    "sv": "och i att det som en på är av för med till den har de inte om ett men var jag sig från vi så kan man när år hans hon eller ska också efter vid mot sin sina nu utan".split(),
    "da": "og i at det en den til er som på de med han af for ikke der var mig sig men et har om vi min havde ham hun nu over da fra du ud sin dem os op man hans hvor eller hvad skal selv".split(),
    "pl": "i w nie na się z do to że jest jak ale po co tak za od przez dla o czy już tylko być ten są jego był która może przy też tym lub bardzo gdy oraz".split(),
    "id": "yang dan di ke dari ini itu dengan untuk tidak akan pada juga dalam saya adalah ada mereka kita atau oleh sebagai bisa telah lebih karena sudah kami apa jika tetapi seperti".split(),
    "tr": "ve bir bu da de için ile mi çok daha gibi ama o ne var olarak olan kadar sonra ben sen biz onlar her bütün ancak ya değil hem en".split(),
}
_STOP_SETS = {k: set(v) for k, v in _STOP.items()}
_SCRIPT_RANGES = {
    "han": re.compile(r"[㐀-䶿一-鿿]"), "kana": re.compile(r"[぀-ヿ]"),
    "hangul": re.compile(r"[가-힯ᄀ-ᇿ]"), "cyrillic": re.compile(r"[Ѐ-ӿ]"),
    "arabic": re.compile(r"[؀-ۿ]"), "hebrew": re.compile(r"[֐-׿]"),
    "greek": re.compile(r"[Ͱ-Ͽ]"), "devanagari": re.compile(r"[ऀ-ॿ]"),
    "thai": re.compile(r"[฀-๿]"), "latin": re.compile(r"[A-Za-zÀ-ɏ]"),
}


def detect_language(text: str, *, sample: int = 200_000) -> dict[str, Any]:
    """Heuristic guess: ``{"code": "en"|…|"und", "confidence": 0..1, "method": …}``."""
    text = text[:sample]
    letters = [ch for ch in text if ch.isalpha()]
    if len(letters) < 20:
        return {"code": "und", "confidence": 0.0, "method": "too little text"}
    counts = {name: len(rx.findall(text)) for name, rx in _SCRIPT_RANGES.items()}
    total = sum(counts.values()) or 1
    top = max(counts, key=counts.get)
    share = counts[top] / total
    if top in ("han", "kana") or counts["kana"] > 0.05 * total:
        if counts["kana"] > 0.1 * (counts["han"] + counts["kana"]):
            return {"code": "ja", "confidence": round(min(0.95, 0.6 + share / 2), 2), "method": "script"}
        if share > 0.5:
            return {"code": "zh", "confidence": round(min(0.9, 0.5 + share / 2), 2), "method": "script"}
    if top == "hangul" and share > 0.5:
        return {"code": "ko", "confidence": 0.95, "method": "script"}
    if top == "greek" and share > 0.5:
        return {"code": "el", "confidence": 0.95, "method": "script"}
    if top == "hebrew" and share > 0.5:
        return {"code": "he", "confidence": 0.95, "method": "script"}
    if top == "thai" and share > 0.5:
        return {"code": "th", "confidence": 0.95, "method": "script"}
    if top == "devanagari" and share > 0.5:
        return {"code": "hi", "confidence": 0.7, "method": "script (Hindi assumed; Marathi/Nepali share the script)"}
    if top == "arabic" and share > 0.5:
        code = "fa" if re.search(r"[پچژگ]", text) else ("ur" if re.search(r"[ٹڈڑںے]", text) else "ar")
        return {"code": code, "confidence": 0.7, "method": "script + letters"}
    if top == "cyrillic" and share > 0.5:
        code = "uk" if re.search(r"[іїєґ]", text.lower()) else ("bg" if re.search(r"\b(е|на|и|за|са)\b", text.lower()) and "ы" not in text.lower() else "ru")
        return {"code": code, "confidence": 0.7, "method": "script + letters"}
    if top == "latin":
        words = [w.lower() for w in tokenize(text) if not w.isdigit()]
        if len(words) < 8:
            return {"code": "und", "confidence": 0.0, "method": "too few words"}
        scores = {lang: sum(1 for w in words if w in stops) / len(words) for lang, stops in _STOP_SETS.items()}
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        best, runner = ranked[0], ranked[1]
        if best[1] < 0.08:
            return {"code": "und", "confidence": round(best[1], 2), "method": "stop-word profile (weak)"}
        margin = best[1] - runner[1]
        confidence = round(min(0.97, 0.45 + best[1] * 1.2 + margin * 2), 2)
        return {"code": best[0], "confidence": confidence, "method": "stop-word profile"}
    return {"code": "und", "confidence": 0.0, "method": "unsupported script"}


def sldc_language(code: str) -> str:
    """A language tag acceptable to SLDC (``und`` when unknown)."""
    return code if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}", code or "") else "und"


def top_terms(text: str, stopword_lang: str, limit: int = 20, min_len: int = 3) -> list[tuple[str, int]]:
    stops = _STOP_SETS.get(stopword_lang, set())
    counter: Counter = Counter()
    for w in tokenize(text):
        lw = w.lower()
        if len(lw) < min_len or lw in stops or lw.isdigit() or _CJK_CHAR.fullmatch(lw):
            continue
        counter[lw] += 1
    return counter.most_common(limit)


def frequency(words: Iterable[str]) -> Counter:
    return Counter(w.lower() for w in words)
