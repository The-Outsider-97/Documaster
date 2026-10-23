"""Assistant engine: a typed tool registry, request understanding, and confirm-before-change proposals.

* Read-only tools run immediately and return evidence (document, page, block, offsets).
* Tools that would change or create a file never run from a message. They return a *proposal* describing exactly what will
  happen; the person confirms it (optionally entering a password), and only then does the same request run as a normal job.
  A proposal is single-use, expires after 15 minutes and is bound to the revision of each document it concerns.
* Everything the assistant says about a document is computed from the document. When no tool applies, it says so and offers
  the actions it does have. A Language agent may help choose a tool or order passages when it is available and permitted;
  what it contributed is always shown.
"""
from __future__ import annotations

import io
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import textstats
from ..analysis import agent_calls, insights, pii
from ..analysis.compare import compare as compare_docs
from ..analysis.model import Unit, units_of, full_text
from ..errors import DocumasterError, NotFound, UnsupportedOperation, ValidationFailed
from ..formats import TARGETS
from ..router import Request
from . import nlu
from .schema import validate

PROPOSAL_TTL = 900
DOC = {"type": "string", "minLength": 1, "maxLength": 64}


@dataclass
class Out:
    text: str
    data: Any = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    proposal: dict[str, Any] | None = None
    notes: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Tool:
    name: str
    title: str
    description: str
    schema: dict[str, Any]
    changes: bool
    fn: Callable[["Assistant", dict[str, Any]], Out]

    def public(self) -> dict[str, Any]:
        return {"name": self.name, "title": self.title, "description": self.description, "schema": self.schema, "changesFiles": self.changes}


TOOLS: dict[str, Tool] = {}


def tool(name: str, title: str, description: str, props: dict[str, Any], required: list[str] | None = None, changes: bool = False
         ) -> Callable[[Callable[["Assistant", dict[str, Any]], Out]], Callable[["Assistant", dict[str, Any]], Out]]:
    def deco(fn: Callable[["Assistant", dict[str, Any]], Out]) -> Callable[["Assistant", dict[str, Any]], Out]:
        TOOLS[name] = Tool(name, title, description, {"type": "object", "properties": props, "required": required or [],
                                                      "additionalProperties": False}, changes, fn)
        return fn
    return deco


class Assistant:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.proposals: dict[str, dict[str, Any]] = {}
        self.active_doc: str | None = None

    # ------------------------------------------------------------------ helpers
    @property
    def ws(self) -> Any:
        return self.app.ws

    @property
    def svc(self) -> Any:
        return self.app.svc

    def doc(self, doc_id: str) -> Any:
        d = self.ws.get(doc_id)
        self.ws.require_open(d)
        return d

    def units(self, doc: Any) -> tuple[list[Unit], Any]:
        res = self.ws.ir(doc)
        return units_of(doc, res), res

    def lang(self, units: list[Unit]) -> str:
        return textstats.detect_language(full_text(units)[:100000]).get("code", "und")

    def agents_allowed(self, doc: Any, units: list[Unit]) -> dict[str, Any]:
        text = full_text(units)
        pii_n = pii.scan(units, limit=50)["total"]
        return agent_calls.privacy_gate(self.svc, self.app.consent, doc.id, text, pii_n)

    # ------------------------------------------------------------------ entry points
    def run_tool(self, name: str, args: dict[str, Any]) -> Out:
        t = TOOLS.get(name)
        if t is None:
            raise ValidationFailed(f"Unknown tool {name!r}", details={"tools": sorted(TOOLS)})
        validate(args, t.schema)
        return t.fn(self, args)

    def message(self, text: str, active_doc: str | None = None) -> dict[str, Any]:
        text = (text or "").strip()
        if not text:
            raise ValidationFailed("Type a request")
        if len(text) > 4000:
            raise ValidationFailed("That message is too long")
        if active_doc and active_doc in self.ws.documents:
            self.active_doc = active_doc
        docs = [d for d in self.ws.documents.values()]
        intent = nlu.parse(text, [d.name for d in docs])
        notes: list[dict[str, Any]] = []
        via = "rules"
        if (intent.tool is None or intent.confidence < 0.7) and self.svc.settings.agents != "none":
            labels = [n for n in TOOLS if n != "capabilities"]
            note = agent_calls.classify_intent(self.svc, text, labels)
            notes.append(note.public())
            if note.status == "used" and isinstance(note.output, dict) and note.output.get("label") in TOOLS:
                if intent.tool is None or (note.output.get("score") or 0) >= 0.6:
                    intent.tool, via = note.output["label"], "Language agent"
        if intent.tool is None:
            return {"text": "I couldn't map that to something I can do. I can count words, convert, merge, edit and redact PDFs, "
                            "search, summarise, check structure, compare documents and answer questions about an open document. "
                            "Try one of the suggestions.", "understood": None, "evidence": [], "proposal": None,
                    "agents": notes, "suggestions": self.suggestions()}
        t = TOOLS[intent.tool]
        args: dict[str, Any] = {}
        try:
            args = self._arguments(intent, docs)
            out = self.run_tool(intent.tool, args)
        except DocumasterError as exc:
            msg = exc.message
            if exc.code == "invalid_request" and msg.endswith("is required"):
                msg = f"I understood this as '{t.title}' but I need more detail: {msg.replace('arguments.', '').replace('is required', 'is missing')}."
            return {"text": msg, "understood": {"tool": intent.tool, "via": via, "arguments": _safe_args(args)},
                    "error": exc.to_dict(), "evidence": [], "proposal": None, "agents": notes, "suggestions": []}
        return {"text": out.text, "data": out.data, "evidence": out.evidence[:40], "proposal": out.proposal,
                "understood": {"tool": t.name, "title": t.title, "via": via, "changesFiles": t.changes, "arguments": _safe_args(args)},
                "agents": notes + out.notes, "suggestions": []}

    def suggestions(self) -> list[str]:
        d = self.ws.documents.get(self.active_doc or "") or next(iter(self.ws.documents.values()), None)
        if d is None:
            return ["What can you do?"]
        base = [f"Count the words in {d.name}", f"Summarise {d.name}", f"Check the structure of {d.name}"]
        if d.format == "pdf":
            base.append("Compress this PDF")
        return base

    def _named(self, docs: list[Any], name: str) -> Any:
        """The one document a name refers to. Several documents may share a name; a guess is never made silently."""
        same = [d for d in docs if d.name.lower() == name.lower()]
        if len(same) == 1:
            return same[0]
        active = self.ws.documents.get(self.active_doc or "")
        if active is not None and active in same:
            return active
        raise ValidationFailed(f"{len(same)} open documents are called '{name}'. Choose the one you mean in 'Working with', or close the others.")

    def _arguments(self, intent: nlu.Intent, docs: list[Any]) -> dict[str, Any]:
        args = dict(intent.args)
        t = TOOLS[intent.tool]  # type: ignore[index]
        props = t.schema["properties"]
        mentioned: list[Any] = []
        for name in dict.fromkeys(intent.doc_mentions):          # each name once, in the order it was written
            d = self._named(docs, name)
            if d not in mentioned:
                mentioned.append(d)
        default = self.ws.documents.get(self.active_doc or "") or (docs[0] if len(docs) == 1 else None)
        if "doc" in props and "doc" not in args:
            chosen = mentioned[0] if mentioned else default
            if chosen is not None:
                args["doc"] = chosen.id
        if "docs" in props:
            named = args.pop("docs_named", None)
            args["docs"] = [self._named(docs, n).id for n in named] if named else ([d.id for d in mentioned] or ([default.id] if default else []))
            if intent.tool == "merge" and len(args["docs"]) < 2:
                args["docs"] = [d.id for d in docs]
        if intent.tool == "compare":
            a, b = args.pop("a", None), args.pop("b", None)
            pair = [self._named(docs, x) for x in (a, b) if x] if a and b else docs[:2]
            if len(pair) == 2:
                args["a"], args["b"] = pair[0].id, pair[1].id
        if intent.tool == "answer_question":
            args["docs"] = args.get("docs") or ([d.id for d in mentioned] or ([default.id] if default else [d.id for d in docs]))
        return {k: v for k, v in args.items() if k in props}

    # ------------------------------------------------------------------ proposals
    def propose(self, tool_name: str, title: str, description: str, request: dict[str, Any], docs: list[Any],
                needs: list[str] | None = None, consequences: list[str] | None = None) -> dict[str, Any]:
        self._expire()
        pid = uuid.uuid4().hex[:12]
        p = {"id": pid, "tool": tool_name, "title": title, "description": description, "consequences": consequences or [],
             "needs": needs or [], "request": request, "expires": time.time() + PROPOSAL_TTL,
             "revisions": {d.id: d.revision.sha256 for d in docs}, "used": False}
        self.proposals[pid] = p
        return self.public_proposal(p)

    @staticmethod
    def public_proposal(p: dict[str, Any]) -> dict[str, Any]:
        return {k: p[k] for k in ("id", "tool", "title", "description", "consequences", "needs")} | {"expiresInS": max(0, round(p["expires"] - time.time()))}

    def _expire(self) -> None:
        now = time.time()
        for k in [k for k, p in self.proposals.items() if p["expires"] < now or p["used"]]:
            self.proposals.pop(k, None)

    def reject(self, pid: str) -> dict[str, Any]:
        self.proposals.pop(pid, None)
        return {"rejected": pid}

    def confirm(self, pid: str, inputs: dict[str, Any] | None = None) -> Any:
        from .. import api
        self._expire()
        p = self.proposals.get(pid)
        if p is None:
            raise NotFound("That proposal has expired or was already used. Ask again.")
        for doc_id, sha in p["revisions"].items():
            d = self.ws.documents.get(doc_id)
            if d is None or d.revision.sha256 != sha:
                self.proposals.pop(pid, None)
                raise ValidationFailed("The document changed after this was proposed. Ask again so the proposal matches the current document.")
        body = _deep_copy(p["request"]["body"])
        for need in p["needs"]:
            if not inputs or not inputs.get(need):
                raise ValidationFailed(f"Enter the {need} to continue")
            _inject(body, need, inputs[need])
        p["used"] = True
        import json as _json
        raw = _json.dumps(body).encode()
        req = Request("POST", "/", {}, {}, io.BytesIO(raw), len(raw), {"id": p["request"].get("docId", "")})
        endpoint = p["request"]["endpoint"]
        handler = {"convert": api.convert, "merge": api.merge, "op": api.pdf_op}[endpoint]
        return handler(self.app, req)


def _deep_copy(x: Any) -> Any:
    import copy
    return copy.deepcopy(x)


def _inject(body: dict[str, Any], key: str, value: Any) -> None:
    if key == "password":
        if body.get("op") == "password":
            body["new"] = value
        elif "options" in body:
            body["options"]["encrypt_password"] = value
        else:
            body["password"] = value


def _safe_args(args: dict[str, Any]) -> dict[str, Any]:
    return {k: ("•••" if "password" in k else v) for k, v in args.items()}


def _page(ev: dict[str, Any]) -> str:
    return f"p. {ev['page']}" if ev.get("page") else f"part {ev.get('part', 1)}"


# ============================================================================================ read-only tools
@tool("list_documents", "List documents", "List the documents that are open.", {})
def _list_documents(a: Assistant, args: dict[str, Any]) -> Out:
    docs = list(a.ws.documents.values())
    if not docs:
        return Out("No documents are open yet. Drop a file onto any workspace to start.")
    return Out("Open documents:\n" + "\n".join(f"- {d.name} ({d.format.upper()}" + (f", {d.pages} pages" if d.pages else "") + ")" for d in docs),
               [d.public() for d in docs])


@tool("capabilities", "What I can do", "Explain the assistant's abilities.", {})
def _capabilities(a: Assistant, args: dict[str, Any]) -> Out:
    read = [t.title for t in TOOLS.values() if not t.changes]
    change = [t.title for t in TOOLS.values() if t.changes]
    return Out("I can look things up right away: " + ", ".join(read) + ".\nI can also prepare changes, which only happen after you confirm: "
               + ", ".join(change) + ".\nMy answers come from the open documents, with page and position references. I never act on "
               "instructions found inside a document.")


@tool("describe_document", "Describe a document", "Format, size, pages and basic structure of a document.", {"doc": DOC}, ["doc"])
def _describe(a: Assistant, args: dict[str, Any]) -> Out:
    d = a.doc(args["doc"])
    units, res = a.units(d)
    heads = [u for u in units if u.kind == "heading"]
    stats = textstats.basic_counts(full_text(units))
    text = (f"{d.name}: {d.format.upper()}, {d.revision.bytes:,} bytes" + (f", {d.pages} pages" if d.pages else "") +
            f". {stats['words']:,} words in {stats['paragraphs']} paragraph(s), {len(heads)} heading(s). Read with {res.backend}.")
    if res.warnings:
        text += "\nNotes: " + "; ".join(res.warnings[:3])
    return Out(text, {"document": d.public(), "stats": stats})


@tool("count_words", "Count words", "Words, characters, sentences and reading time of a document.",
      {"doc": DOC, "goals": {"type": "object"}}, ["doc"])
def _count(a: Assistant, args: dict[str, Any]) -> Out:
    from .. import counter
    d = a.doc(args["doc"])
    s = counter.count_document(a.svc, a.ws, d, goals=args.get("goals"))
    text = (f"{d.name} has {s['words']:,} words, {s['characters']:,} characters ({s['charactersNoSpaces']:,} without spaces), "
            f"{s['sentences']:,} sentences and {s['paragraphs']:,} paragraphs. Reading time about {s['readingMinutes']} min.")
    if s.get("notes"):
        text += "\n" + s["notes"][-1]
    return Out(text, s)


@tool("outline", "Show outline", "The heading structure of a document.", {"doc": DOC}, ["doc"])
def _outline(a: Assistant, args: dict[str, Any]) -> Out:
    d = a.doc(args["doc"])
    units, _ = a.units(d)
    tree = insights.outline(units)
    lines: list[str] = []

    def walk(nodes: list[dict[str, Any]], depth: int) -> None:
        for n in nodes:
            lines.append("  " * depth + f"- {n['title']}" + (f" (p. {n['page']})" if n["page"] else ""))
            walk(n["children"], depth + 1)

    walk(tree, 0)
    return Out("\n".join(lines[:80]) if lines else f"{d.name} has no headings.", tree)


@tool("search_text", "Search text", "Find text in a document, with page and position.",
      {"doc": DOC, "query": {"type": "string", "minLength": 1, "maxLength": 300}, "regex": {"type": "boolean"}, "case": {"type": "boolean"}},
      ["doc", "query"])
def _search(a: Assistant, args: dict[str, Any]) -> Out:
    d = a.doc(args["doc"])
    units, _ = a.units(d)
    try:
        hits = insights.search(units, args["query"], regex=bool(args.get("regex")), case=bool(args.get("case")))
    except re.error as exc:
        raise ValidationFailed(f"That is not a valid pattern: {exc}") from exc
    if not hits:
        return Out(f"I did not find '{args['query']}' in {d.name}.", {"hits": []})
    shown = hits[:8]
    lines = [f"{i + 1}. ({_page(h['evidence'])}) …{h['context']}…" for i, h in enumerate(shown)]
    return Out(f"Found {len(hits)} match(es) in {d.name}:\n" + "\n".join(lines), {"count": len(hits)}, [h["evidence"] for h in shown])


@tool("summarize", "Summarise", "An extractive summary: the most representative sentences of a document, with where they come from.",
      {"doc": DOC, "sentences": {"type": "integer", "minimum": 1, "maximum": 15}}, ["doc"])
def _summarize(a: Assistant, args: dict[str, Any]) -> Out:
    d = a.doc(args["doc"])
    units, _ = a.units(d)
    s = insights.summary(units, a.lang(units), int(args.get("sentences", 5)))
    if not s["sentences"]:
        return Out(s.get("note", "There is not enough text to summarise."), s)
    lines = [f"- {x['text']} ({_page(x['evidence'])})" for x in s["sentences"]]
    text = "Key sentences from the document (extractive - they are quoted, not paraphrased):\n" + "\n".join(lines)
    notes: list[dict[str, Any]] = []
    gate = a.agents_allowed(d, units)
    if gate["forward"] and a.svc.settings.agents != "none":
        safety = agent_calls.safety_review(a.svc, full_text(units))
        notes.append(safety.public())
        if not agent_calls.safety_blocks(safety):
            ab = agent_calls.abstract_summary(a.svc, full_text(units), 80)
            notes.append(ab.public())
            if ab.status == "used":
                text += "\n\nMachine-written summary (Language agent - check it against the sentences above):\n" + ab.output["text"]
        else:
            notes.append({"role": "language", "status": "skipped", "reason": "The Safety agent flagged the document text"})
    elif gate.get("note"):
        notes.append(gate["note"].public())
        notes.append({"role": "language", "status": "skipped", "reason": gate["reason"]})
    else:
        notes.append({"role": "language", "status": "skipped", "reason": gate["reason"]})
    return Out(text, s, [x["evidence"] for x in s["sentences"]], notes=notes)


@tool("keywords", "Keywords", "Main terms of a document with where they appear.", {"doc": DOC, "top": {"type": "integer", "minimum": 1, "maximum": 50}}, ["doc"])
def _keywords(a: Assistant, args: dict[str, Any]) -> Out:
    d = a.doc(args["doc"])
    units, _ = a.units(d)
    kw = insights.keywords(units, a.lang(units), int(args.get("top", 15)))
    return Out("Main terms: " + ", ".join(f"{k['term']} ({k['count']})" for k in kw) if kw else "No clear keywords found.", kw,
               [e for k in kw[:5] for e in k["evidence"][:1]])


@tool("readability", "Readability", "Reading ease and sentence length.", {"doc": DOC}, ["doc"])
def _readability(a: Assistant, args: dict[str, Any]) -> Out:
    d = a.doc(args["doc"])
    units, _ = a.units(d)
    r = insights.readability(full_text(units), a.lang(units))
    if not r["available"]:
        return Out(r["reason"], r)
    text = f"Average sentence length {r['avgWordsPerSentence']} words; LIX {r['lix']} ({r['lixLevel']})."
    if "fleschReadingEase" in r:
        text += f" Flesch reading ease {r['fleschReadingEase']} ({r['fleschLevel']}), grade level {r['fleschKincaidGrade']}."
    return Out(text, r)


@tool("check_structure", "Check structure", "Headings, images without descriptions, tables, links and scanned pages.", {"doc": DOC}, ["doc"])
def _structure(a: Assistant, args: dict[str, Any]) -> Out:
    d = a.doc(args["doc"])
    units, res = a.units(d)
    empty = _empty_pages(units, d)
    findings = insights.structure_checks(res.package, units, empty)
    if not findings:
        return Out(f"No structure problems found in {d.name}.", [])
    lines = [f"- [{f['severity']}] {f['message']}" for f in findings[:12]]
    return Out(f"{len(findings)} finding(s) in {d.name}:\n" + "\n".join(lines), findings, [e for f in findings for e in f["evidence"][:1] if e.get("page") or e.get("part")][:20])


def _empty_pages(units: list[Unit], d: Any) -> list[int]:
    if d.format != "pdf" or not d.pages:
        return []
    with_text = {u.page for u in units}
    return [p for p in range(1, d.pages + 1) if p not in with_text]


@tool("find_pii", "Find personal data", "Look for email addresses, phone numbers, IBANs, card numbers and similar.", {"doc": DOC}, ["doc"])
def _pii(a: Assistant, args: dict[str, Any]) -> Out:
    d = a.doc(args["doc"])
    units, _ = a.units(d)
    r = pii.scan(units)
    empty = _empty_pages(units, d)
    caveat = f" {len(empty)} page(s) have no text layer (scanned or already redacted), so they could not be checked: {empty[:10]}." if empty else ""
    if not r["total"]:
        return Out("I found no email addresses, phone numbers, IBANs or card numbers." + caveat + " " + r["note"], r)
    parts = ", ".join(f"{n} {pii.LABELS[k].lower()}" for k, n in r["counts"].items())
    return Out(f"I found {r['total']} item(s) that look like personal data: {parts}.{caveat} {r['note']}", r,
               [i["evidence"] for i in r["items"][:20]])


@tool("find_duplicates", "Find repeated text", "Paragraphs that appear more than once or are nearly identical.", {"doc": DOC}, ["doc"])
def _dups(a: Assistant, args: dict[str, Any]) -> Out:
    d = a.doc(args["doc"])
    units, _ = a.units(d)
    r = insights.duplicates(units)
    if not r["groups"]:
        return Out("No repeated paragraphs found.", r)
    lines = [f"- {g['kind']} ×{g['count']}: \"{g['text'][:90]}…\"" for g in r["groups"][:8]]
    return Out(f"{r['total']} group(s) of repeated text:\n" + "\n".join(lines), r, [g["evidence"][0] for g in r["groups"][:10]])


@tool("compare", "Compare documents", "What was added, removed or changed between two documents.", {"a": DOC, "b": DOC}, ["a", "b"])
def _compare(a: Assistant, args: dict[str, Any]) -> Out:
    da, db = a.doc(args["a"]), a.doc(args["b"])
    (ua, ra), (ub, rb) = a.units(da), a.units(db)
    r = compare_docs(ra.package, ua, rb.package, ub, name_a=da.name, name_b=db.name)
    s = r["summary"]
    text = (f"Comparing {da.name} with {db.name} ({r['method']}): "
            f"{s.get('added', 0)} added, {s.get('removed', 0)} removed, {s.get('modified', 0)} changed, {s.get('unchanged', 0)} unchanged. "
            f"Word-level similarity {int(r['wordSimilarity'] * 100)}%.")
    return Out(text, r, [m["after"] for m in r["modified"][:6]] + r["added"][:4] + r["removed"][:4])


@tool("check_sections", "Check required sections", "Whether a document has the sections a kind of document should have.",
      {"doc": DOC, "preset": {"type": "string", "enum": list(insights.PRESETS)},
       "required": {"type": "array", "items": {"type": "string", "maxLength": 80}, "maxItems": 40}}, ["doc"])
def _sections(a: Assistant, args: dict[str, Any]) -> Out:
    d = a.doc(args["doc"])
    units, _ = a.units(d)
    required = args.get("required") or insights.PRESETS[args.get("preset", "report")]
    r = insights.checklist(units, required)
    if r["complete"]:
        return Out("All expected sections are present: " + ", ".join(required) + ".", r, [e for i in r["items"] for e in i["evidence"]][:12])
    return Out("Missing: " + ", ".join(r["missing"]) + ". Present: " + (", ".join(i["required"] for i in r["items"] if i["found"]) or "none of them") + ". " + r["note"], r,
               [e for i in r["items"] for e in i["evidence"]][:12])


_STOP_Q = set("what who when where why how which is are does do did can the a an of in on for to and or be was were it this that with by at from as about".split())


@tool("answer_question", "Answer a question", "Answer from the text of the open documents, quoting the passages used.",
      {"docs": {"type": "array", "items": DOC, "minItems": 1, "maxItems": 20}, "question": {"type": "string", "minLength": 3, "maxLength": 1000}},
      ["docs", "question"])
def _answer(a: Assistant, args: dict[str, Any]) -> Out:
    import math
    from collections import Counter
    q_terms = [w.lower() for w in textstats.tokenize(args["question"]) if w.lower() not in _STOP_Q and len(w) > 2]
    if not q_terms:
        return Out("Ask about something specific in the document, for example a name, a topic or a number.")
    passages: list[tuple[Unit, int, int, str]] = []
    gates: list[Any] = []
    for did in args["docs"]:
        d = a.doc(did)
        units, _ = a.units(d)
        gates.append((d, units))
        for u in units:
            if u.kind == "code":
                continue
            spans = textstats.sentence_spans(u.text)
            for i, (s, e) in enumerate(spans):
                nxt = spans[i + 1][1] if i + 1 < len(spans) else e
                passages.append((u, s, nxt if nxt - s < 500 else e, u.text[s:(nxt if nxt - s < 500 else e)]))
    if not passages:
        return Out("The documents have no text to search.")
    df: Counter = Counter()
    toks = [[w.lower() for w in textstats.tokenize(p[3])] for p in passages]
    for t in toks:
        for w in set(t):
            df[w] += 1
    n = len(passages)
    avg = sum(len(t) for t in toks) / n
    scored = []
    for i, t in enumerate(toks):
        tf = Counter(t)
        score = 0.0
        for w in q_terms:
            if tf[w]:
                idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
                score += idf * (tf[w] * 2.2) / (tf[w] + 1.2 * (0.25 + 0.75 * len(t) / avg))
        if score > 0:
            scored.append((score, i))
    if not scored:
        return Out(f"I could not find anything about that in the open document(s). The question mentioned: {', '.join(dict.fromkeys(q_terms))}.")
    scored.sort(reverse=True)
    top = [i for _, i in scored[:8]]
    notes: list[dict[str, Any]] = []
    d0, u0 = gates[0]
    if len(gates) == 1 and a.svc.settings.agents != "none":
        gate = a.agents_allowed(d0, u0)
        if gate["forward"]:
            safety = agent_calls.safety_review(a.svc, "\n".join(passages[i][3] for i in top))
            notes.append(safety.public())
            if not agent_calls.safety_blocks(safety):
                note, order = agent_calls.rerank(a.svc, args["question"], [passages[i][3] for i in top], 3)
                notes.append(note.public())
                if order:
                    top = [top[j] for j in order] + [i for k, i in enumerate(top) if k not in order]
        else:
            notes.append({"role": "language", "status": "skipped", "reason": gate["reason"]})
            if gate.get("note"):
                notes.append(gate["note"].public())
    best = top[:3]
    lines = []
    ev = []
    for k, i in enumerate(best, 1):
        u, s, e, text = passages[i]
        lines.append(f"[{k}] ({u.doc_name}, {_page(u.evidence())}) {text.strip()}")
        ev.append(u.evidence(s, e))
    return Out("These passages answer the question best (quoted from the document - I do not add anything beyond them):\n" + "\n".join(lines),
               {"scores": [round(s, 2) for s, _ in scored[:3]]}, ev, notes=notes)


# ============================================================================================ change tools (proposals)
def _need_pdf(d: Any) -> None:
    if d.format != "pdf":
        raise UnsupportedOperation(f"{d.name} is not a PDF. This action works on PDFs; convert it to PDF first.")


def _pdf_proposal(a: Assistant, name: str, title: str, d: Any, op: dict[str, Any], description: str,
                  consequences: list[str] | None = None, needs: list[str] | None = None) -> Out:
    _need_pdf(d)
    p = a.propose(name, title, description, {"endpoint": "op", "docId": d.id, "body": op}, [d], needs, consequences)
    return Out(f"{description} Confirm to go ahead.", None, [], p)


@tool("convert", "Convert", "Convert documents to another format.",
      {"docs": {"type": "array", "items": DOC, "minItems": 1, "maxItems": 50}, "target": {"type": "string", "enum": list(TARGETS)}}, ["docs", "target"], changes=True)
def _convert(a: Assistant, args: dict[str, Any]) -> Out:
    docs = [a.doc(i) for i in args["docs"]]
    names = ", ".join(d.name for d in docs)
    p = a.propose("convert", "Convert", f"Convert {names} to {args['target'].upper()}. The result is checked before it is saved to your output folder; "
                  "your originals are not changed.", {"endpoint": "convert", "body": {"docs": args["docs"], "options": {"target": args["target"]}}}, docs)
    return Out(f"I can convert {names} to {args['target'].upper()}. Confirm to go ahead.", None, [], p)


@tool("merge", "Merge", "Merge documents into one.",
      {"docs": {"type": "array", "items": DOC, "minItems": 2, "maxItems": 50}, "target": {"type": "string", "enum": list(TARGETS)}}, ["docs"], changes=True)
def _merge(a: Assistant, args: dict[str, Any]) -> Out:
    docs = [a.doc(i) for i in args["docs"]]
    target = args.get("target", "pdf")
    names = " + ".join(d.name for d in docs)
    p = a.propose("merge", "Merge", f"Merge {names} into one {target.upper()} in the order shown, using every page.",
                  {"endpoint": "merge", "body": {"docs": args["docs"], "target": target}}, docs,
                  consequences=["Use the Merger workspace if you want to reorder, rotate or exclude pages first."])
    return Out(f"I can merge {names} into one {target.upper()}. Confirm to go ahead.", None, [], p)


@tool("rotate", "Rotate pages", "Rotate pages of a PDF.",
      {"doc": DOC, "degrees": {"type": "integer", "enum": [90, 180, 270]}, "pages": {"type": "string", "maxLength": 100}}, ["doc"], changes=True)
def _rotate(a: Assistant, args: dict[str, Any]) -> Out:
    d = a.doc(args["doc"])
    pages = args.get("pages") or "all"
    return _pdf_proposal(a, "rotate", "Rotate pages", d, {"op": "rotate", "pages": pages, "degrees": args.get("degrees", 90)},
                         f"Rotate {'all pages' if pages == 'all' else 'page(s) ' + pages} of {d.name} by {args.get('degrees', 90)}° clockwise.",
                         ["You can undo this in the PDF Editor."])


@tool("delete_pages", "Delete pages", "Delete pages from a PDF.", {"doc": DOC, "pages": {"type": "string", "minLength": 1, "maxLength": 100}},
      ["doc", "pages"], changes=True)
def _delete(a: Assistant, args: dict[str, Any]) -> Out:
    d = a.doc(args["doc"])
    return _pdf_proposal(a, "delete_pages", "Delete pages", d, {"op": "delete", "pages": args["pages"]},
                         f"Delete page(s) {args['pages']} from {d.name}.", ["You can undo this in the PDF Editor."])


@tool("watermark", "Add a watermark", "Add diagonal watermark text to all pages of a PDF.", {"doc": DOC, "text": {"type": "string", "minLength": 1, "maxLength": 60}},
      ["doc", "text"], changes=True)
def _watermark(a: Assistant, args: dict[str, Any]) -> Out:
    d = a.doc(args["doc"])
    return _pdf_proposal(a, "watermark", "Add a watermark", d, {"op": "stamp", "kind": "watermark", "text": args["text"]},
                         f"Add the watermark \"{args['text']}\" to every page of {d.name}.", ["The watermark becomes part of the pages (undo removes it)."])


@tool("page_numbers", "Add page numbers", "Add page numbers to a PDF.", {"doc": DOC}, ["doc"], changes=True)
def _numbers(a: Assistant, args: dict[str, Any]) -> Out:
    d = a.doc(args["doc"])
    return _pdf_proposal(a, "page_numbers", "Add page numbers", d, {"op": "stamp", "kind": "page_numbers"},
                         f"Add 'Page n of N' at the bottom centre of every page of {d.name}.")


@tool("set_password", "Set or remove a password", "Protect a PDF with a password, or remove it.", {"doc": DOC, "remove": {"type": "boolean"}, "password": {"type": "string", "maxLength": 128}},
      ["doc"], changes=True)
def _password(a: Assistant, args: dict[str, Any]) -> Out:
    d = a.doc(args["doc"])
    if args.get("remove"):
        return _pdf_proposal(a, "set_password", "Remove password", d, {"op": "password", "new": ""}, f"Remove the password from {d.name}.")
    return _pdf_proposal(a, "set_password", "Set password", d, {"op": "password", "new": ""}, f"Protect {d.name} with a password (AES-256). "
                         "Enter the password in the confirmation box; it is not kept in the chat.", needs=["password"])


@tool("compress", "Compress", "Make a PDF smaller without losing quality.", {"doc": DOC}, ["doc"], changes=True)
def _compress(a: Assistant, args: dict[str, Any]) -> Out:
    d = a.doc(args["doc"])
    return _pdf_proposal(a, "compress", "Compress", d, {"op": "compress"}, f"Optimise {d.name} losslessly (SLAI Reader). If the file cannot be made smaller it is left unchanged.")


@tool("ocr", "Recognise text (OCR)", "Make scanned PDF pages searchable.", {"doc": DOC, "lang": {"type": "string", "maxLength": 20}}, ["doc"], changes=True)
def _ocr(a: Assistant, args: dict[str, Any]) -> Out:
    d = a.doc(args["doc"])
    if not a.svc.tools.tesseract:
        raise UnsupportedOperation("OCR needs the Tesseract program, which is not installed on this computer.")
    return _pdf_proposal(a, "ocr", "Recognise text", d, {"op": "ocr", "lang": args.get("lang", "eng")},
                         f"Recognise text on the pages of {d.name} that have none. Recognised text can contain mistakes.")


@tool("redact_pii", "Redact personal data", "Permanently remove personal data (emails, phone numbers, IBANs, cards) from a PDF.",
      {"doc": DOC, "types": {"type": "array", "items": {"type": "string", "enum": list(pii.PATTERNS)}, "minItems": 1}}, ["doc"], changes=True)
def _redact(a: Assistant, args: dict[str, Any]) -> Out:
    d = a.doc(args["doc"])
    _need_pdf(d)
    types = args.get("types") or ["email", "phone", "iban", "card"]
    units, _ = a.units(d)
    found = pii.scan(units)
    n = sum(c for k, c in found["counts"].items() if k in types)
    if n == 0:
        return Out("I found nothing of that kind to redact in " + d.name + ". " + found["note"], found)
    literals = sorted({i["text"] for i in found["items"] if i["type"] in types}, key=len, reverse=True)
    query = "|".join(re.escape(x) for x in literals)
    return _pdf_proposal(a, "redact_pii", "Redact personal data", d, {"op": "redact", "find": {"query": query, "regex": True}, "sanitize": True},
                         f"Permanently remove {n} item(s) ({', '.join(types)}) from {d.name}.",
                         ["Pages with redactions are converted to images, so the removed text cannot be recovered.",
                          "The result is verified before it replaces the current version; you can undo it in the PDF Editor.",
                          "Document properties, bookmarks and annotations containing the removed text are cleaned."])
