"""Rule-based request understanding (English and Dutch verbs). Returns a candidate tool with arguments and a confidence.
The Language agent, when available and usable, can break ties; it never overrides a confident rule match."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

TARGET_WORDS = {"pdf": "pdf", "word": "docx", "docx": "docx", "odt": "odt", "opendocument": "odt", "rtf": "rtf", "html": "html",
                "markdown": "md", "md": "md", "txt": "txt", "text": "txt", "plain text": "txt", "xml": "xml", "sldc": "sldc"}
PAGES = re.compile(r"\bpages?\s+(\d+(?:\s*[-–]\s*\d*)?(?:\s*(?:,|and|en)\s*\d+(?:\s*[-–]\s*\d*)?)*)", re.I)
QUOTED = re.compile(r"[\"“'‘]([^\"”'’]{1,200})[\"”'’]")


@dataclass
class Intent:
    tool: str | None
    args: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    via: str = "rules"
    doc_mentions: list[str] = field(default_factory=list)


RULES: list[tuple[str, float, re.Pattern[str]]] = [(t, c, re.compile(p, re.I)) for t, c, p in [
    ("count_words", 0.9, r"\b(word ?count|how many (words|characters|sentences|pages)|hoeveel (woorden|tekens|zinnen|pagina'?s)|count (the )?(words|characters)|aantal woorden|tel (de )?woorden|reading time|leestijd|how long is)\b"),
    ("redact_pii", 0.9, r"\b(redact|black ?out|anonymi[sz]e|maak onleesbaar|remove (all )?(the )?(e-?mail|phone|iban|personal)|verwijder .*(e-?mail|telefoon|persoonlijk))"),
    ("find_pii", 0.85, r"\b(personal data|pii|sensitive (data|information)|privacy check|persoonsgegevens|gevoelige)\b"),
    ("compare", 0.9, r"\b(compare|difference between|differences between|diff|vergelijk|verschil)\b"),
    ("merge", 0.9, r"\b(merge|combine|join together|samenvoegen|voeg .* samen|put .* together)\b"),
    ("compress", 0.9, r"\b(compress|shrink|reduce (the )?(file )?size|make .* smaller|verklein|comprimeer)\b"),
    ("ocr", 0.9, r"\b(ocr|make (it )?searchable|recogni[sz]e text|doorzoekbaar)\b"),
    ("rotate", 0.9, r"\b(rotate|draai)\b"),
    ("delete_pages", 0.9, r"\b(delete|remove|drop|verwijder)\b.*\bpages?\b"),
    ("watermark", 0.9, r"\bwatermark\b"),
    ("page_numbers", 0.9, r"\bpage numbers?\b|\bpaginanummers?\b"),
    ("set_password", 0.85, r"\b(password[- ]protect|encrypt|set (a )?password|add (a )?password|remove (the )?password|wachtwoord)\b"),
    ("convert", 0.85, r"\b(convert|export|save as|transform|change .* to|zet .* om|converteer)\b"),
    ("check_sections", 0.85, r"\b(missing sections?|required sections?|(has|have|contains?|includes?|got) (a|an|the)?\s*\w+( \w+)? section|checklist|ontbrekende)\b"),
    ("check_structure", 0.85, r"\b(check (the )?(structure|headings|accessibility)|structure check|proofread structure|problems? with (the )?structure)\b"),
    ("find_duplicates", 0.85, r"\b(duplicate|repeated|repetition|dubbel)\b"),
    ("readability", 0.85, r"\b(readab\w*|reading level|how hard .* read|leesbaar\w*|flesch|difficult to read)\b"),
    ("keywords", 0.85, r"\b(keywords?|key terms|main topics?|trefwoorden)\b"),
    ("summarize", 0.9, r"\b(summari[sz]e|summary|tl;?dr|samenvat\w*|in short|key points)\b"),
    ("outline", 0.85, r"\b(outline|table of contents|toc|headings?|inhoudsopgave|structure of)\b"),
    ("search_text", 0.8, r"\b(find|search( for)?|look for|where does|zoek)\b"),
    ("list_documents", 0.9, r"\b(what (documents|files)|list (my |the )?(documents|files)|open documents|welke documenten)\b"),
    ("describe_document", 0.8, r"\b(describe|tell me about|what is this (document|file)|info about)\b"),
    ("capabilities", 0.9, r"\b(what can you do|help|capabilit\w*|which formats|supported formats|wat kun je)\b"),
]]
QUESTION = re.compile(r"^\s*(what|who|when|where|why|how|which|is|are|does|do|did|can|wat|wie|wanneer|waar|waarom|hoe|welke|is|zijn)\b.*\?\s*$", re.I | re.S)


def parse(text: str, doc_names: list[str]) -> Intent:
    t = text.strip()
    orig = t
    low = t.lower()
    def word(x: str) -> re.Pattern[str]:
        return re.compile(r"(?<![\w.])" + re.escape(x) + r"(?![\w])", re.I)
    full = {n.lower() for n in doc_names}
    # "report" may stand for "report.pdf", but not when it is the whole name of another document ("report.pdf" for "report.pdf.bak")
    mentions = [n for n in doc_names if word(n).search(t) or (len(n.rsplit(".", 1)[0]) > 2 and n.rsplit(".", 1)[0].lower() not in full
                                                              and word(n.rsplit(".", 1)[0]).search(t))]
    # A file name is a name, not a request: "summarise pii.pdf" must not be read as a personal-data request.
    for x in sorted({y for n in mentions for y in (n, n.rsplit(".", 1)[0]) if len(y) > 2}, key=len, reverse=True):
        t = word(x).sub("DOC", t)
    low = t.lower()
    best: Intent | None = None
    for tool, conf, pat in RULES:
        if pat.search(t):
            best = Intent(tool, {}, conf, doc_mentions=mentions)
            break
    if best is None and QUESTION.match(t):
        best = Intent("answer_question", {"question": orig}, 0.6, doc_mentions=mentions)
    if best is None:
        return Intent(None, {}, 0.0, doc_mentions=mentions)
    a = best.args
    q = QUOTED.search(orig)
    pm = PAGES.search(t)
    if pm:
        a["pages"] = re.sub(r"\s*(and|en)\s*", ",", re.sub(r"\s+", "", pm.group(1)).replace("–", "-")).replace("and", ",")
    if best.tool in ("convert",):
        m = re.search(r"\b(?:to|into|as|naar)\s+(plain text|opendocument|markdown|word|pdf|docx|odt|rtf|html|md|txt|text|xml|sldc)\b", low) or \
            re.search(r"\b(plain text|opendocument|markdown|word|pdf|docx|odt|rtf|html|md|txt|text|xml|sldc)\b", low)
        if m:
            a["target"] = TARGET_WORDS[m.group(1)]
    if best.tool == "merge":
        m = re.search(r"\b(?:as|into|to|naar)\s+(plain text|opendocument|markdown|word|pdf|docx|odt|rtf|html|md|txt|text|xml|sldc)\b", low)
        a["target"] = TARGET_WORDS[m.group(1)] if m else "pdf"
    if best.tool == "rotate":
        m = re.search(r"\b(90|180|270)\b", low)
        a["degrees"] = int(m.group(1)) if m else 90
        if "counter" in low or "anti" in low or "left" in low:
            a["degrees"] = {90: 270, 180: 180, 270: 90}[a["degrees"]]
    if best.tool == "watermark" and q:
        a["text"] = q.group(1)
    if best.tool == "watermark" and not q:
        m = re.search(r"watermark\s+(?:saying|that says|with|reading|:)?\s*([A-Za-z0-9 _\-]{2,40})$", t, re.I)
        if m:
            a["text"] = m.group(1).strip()
    if best.tool == "search_text" and q:
        a["query"] = q.group(1)
    elif best.tool == "search_text":
        m = re.search(r"(?:find|search(?: for)?|look for|zoek(?: naar)?)\s+(.+?)(?:\s+in\s+.*)?$", t, re.I)
        if m:
            a["query"] = m.group(1).strip().strip("?.")
    if best.tool == "set_password":
        a["remove"] = bool(re.search(r"\bremove\b", low))
        if q:
            a["password"] = q.group(1)
    if best.tool == "redact_pii":
        kinds = [k for k, w in (("email", r"e-?mail"), ("phone", r"phone|telefoon|mobile"), ("iban", r"iban|bank"), ("card", r"card|credit"),
                                ("bsn", r"bsn|citizen"), ("ipv4", r"\bip\b")) if re.search(w, low)]
        a["types"] = kinds or ["email", "phone", "iban", "card"]
    if best.tool == "check_sections":
        for name, preset in (("proposal", "proposal"), ("contract", "contract"), ("research", "research paper"), ("paper", "research paper"), ("report", "report")):
            if name in low:
                a["preset"] = preset
                break
        m = re.search(r"(?:has|have|contain|include)\s+(?:a|an|the)?\s*([A-Za-z ]{3,40}?)\s+section", t, re.I)
        if m:
            a["required"] = [m.group(1).strip()]
        if "preset" not in a and "required" not in a:
            a["preset"] = "report"
    if best.tool == "summarize":
        m = re.search(r"\b(\d{1,2})\s+(?:sentences|bullets|points|zinnen)\b", low)
        if m:
            a["sentences"] = int(m.group(1))
    if best.tool == "compare" and len(mentions) >= 2:
        a["a"], a["b"] = mentions[0], mentions[1]
    if best.tool == "merge" and len(mentions) >= 2:
        a["docs_named"] = mentions
    return best
