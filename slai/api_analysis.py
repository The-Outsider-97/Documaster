"""HTTP routes for document analysis, agent consent and the assistant. Registered by importing this module."""
from __future__ import annotations

from typing import Any

from .analysis import agent_calls, insights, pii
from .analysis.compare import compare as compare_docs
from .analysis.model import full_text, units_of
from .app import App
from .assistant.engine import TOOLS, Assistant
from .errors import ValidationFailed
from .router import Request, route
from . import textstats

CHECKS = ("outline", "structure", "keywords", "readability", "pii", "duplicates", "sections", "summary")


def assistant_of(app: App) -> Assistant:
    a = getattr(app, "assistant", None)
    if a is None:
        a = app.assistant = Assistant(app)  # type: ignore[attr-defined]
    return a


def _units(app: App, doc_id: str) -> tuple[Any, list[Any], Any]:
    doc = app.ws.get(doc_id)
    app.ws.require_open(doc)
    res = app.ws.ir(doc)
    return doc, units_of(doc, res), res


# ---------------------------------------------------------------------------------------------- analysis
@route("POST", "/api/analysis/run", mutating=False)
def analysis_run(app: App, req: Request) -> Any:
    b = req.json()
    doc_id = b.get("doc")
    if not isinstance(doc_id, str):
        raise ValidationFailed("Choose a document")
    checks = b.get("checks") or list(CHECKS)
    if not isinstance(checks, list) or any(c not in CHECKS for c in checks):
        raise ValidationFailed("Unknown check", details={"checks": CHECKS})
    doc, units, res = _units(app, doc_id)
    lang_info = textstats.detect_language(full_text(units)[:100000])
    lang = lang_info["code"]
    out: dict[str, Any] = {"document": doc.public(), "language": lang_info, "backend": res.backend, "warnings": list(res.warnings)}
    if "outline" in checks:
        out["outline"] = insights.outline(units)
    if "structure" in checks:
        if app.settings.agents != "none" and doc.format == "pdf":       # statistics only; no document text goes to the Quality agent
            out["agents"] = [agent_calls.quality_review(app.svc, agent_calls.page_records(units, doc.pages), dataset_id=f"documaster:{doc.id}").public()]
        empty = [] if doc.format != "pdf" else [p for p in range(1, (doc.pages or 0) + 1) if p not in {u.page for u in units}]
        out["structure"] = insights.structure_checks(res.package, units, empty)
    if "keywords" in checks:
        out["keywords"] = insights.keywords(units, lang, int(b.get("top", 20)))
    if "readability" in checks:
        out["readability"] = insights.readability(full_text(units), lang)
    if "pii" in checks:
        out["pii"] = pii.scan(units)
    if "duplicates" in checks:
        out["duplicates"] = insights.duplicates(units)
    if "sections" in checks:
        preset = b.get("preset", "report")
        required = b.get("required") or insights.PRESETS.get(preset)
        if not required:
            raise ValidationFailed("Unknown preset", details={"presets": list(insights.PRESETS)})
        out["sections"] = insights.checklist(units, [str(x)[:80] for x in required][:40])
    if "summary" in checks:
        out["summary"] = insights.summary(units, lang, int(b.get("sentences", 5)))
    return out


@route("GET", "/api/analysis/presets")
def analysis_presets(app: App, req: Request) -> Any:
    return {"presets": insights.PRESETS, "checks": list(CHECKS), "piiTypes": {k: pii.LABELS[k] for k in pii.PATTERNS}}


@route("POST", "/api/analysis/search", mutating=False)
def analysis_search(app: App, req: Request) -> Any:
    b = req.json()
    ids = b.get("docs")
    query = b.get("query")
    if not isinstance(ids, list) or not ids or not isinstance(query, str) or not query:
        raise ValidationFailed("Choose documents and enter what to look for")
    results = []
    for did in ids[:20]:
        doc, units, _ = _units(app, str(did))
        try:
            hits = insights.search(units, query, regex=bool(b.get("regex")), case=bool(b.get("case")))
        except Exception as exc:  # noqa: BLE001 - bad regex
            raise ValidationFailed(f"That is not a valid pattern: {exc}") from exc
        results.append({"doc": doc.id, "name": doc.name, "count": len(hits), "hits": hits[:200]})
    return {"query": query, "results": results, "total": sum(r["count"] for r in results)}


@route("POST", "/api/documents/{id}/pii", mutating=False)
def document_pii(app: App, req: Request) -> Any:
    """Personal data found in a PDF, with the page rectangles of every occurrence so they can be marked for redaction."""
    import re
    from .pdfops import redact
    doc, units, _ = _units(app, req.params["id"])
    if doc.format != "pdf":
        raise ValidationFailed("Marking personal data for redaction works on PDF documents")
    wanted = req.json().get("types")
    scan = pii.scan(units)
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for item in scan["items"]:
        if wanted and item["type"] not in wanted:
            continue
        g = groups.setdefault((item["type"], item["text"]), {"type": item["type"], "label": item["label"], "text": item["text"],
                                                           "confidence": item["confidence"], "count": 0, "hits": []})
        g["count"] += 1
    out = []
    for (kind, text), g in list(groups.items())[:150]:
        hits = redact.find_text(doc.path, doc.password, re.escape(text), regex=True, case=True)
        g["hits"] = [{"page": h["page"], "rects": h["rects"]} for h in hits]
        out.append(g)
    from .assistant.engine import _empty_pages
    empty = _empty_pages(units, doc)
    note = scan["note"]
    if empty:
        note = (f"{len(empty)} page(s) have no text layer (scanned or already redacted), so they could not be checked: "
                f"{', '.join(str(p) for p in empty[:10])}. " + note)
    return {"items": out, "counts": scan["counts"], "note": note, "noTextPages": empty, "truncated": len(groups) > 150}


@route("POST", "/api/analysis/summary", mutating=False)
def analysis_summary(app: App, req: Request) -> Any:
    """Extractive summary with evidence; when the person allowed agents for this document, also a labelled machine-written one."""
    b = req.json()
    if not isinstance(b.get("doc"), str):
        raise ValidationFailed("Choose a document")
    out = assistant_of(app).run_tool("summarize", {"doc": b["doc"], "sentences": int(b.get("sentences", 5))})
    return {"text": out.text, "summary": out.data, "agents": out.notes}


@route("POST", "/api/analysis/compare", mutating=False)
def analysis_compare(app: App, req: Request) -> Any:
    b = req.json()
    if not isinstance(b.get("a"), str) or not isinstance(b.get("b"), str):
        raise ValidationFailed("Choose two documents")
    da, ua, ra = _units(app, b["a"])
    db, ub, rb = _units(app, b["b"])
    return compare_docs(ra.package, ua, rb.package, ub, name_a=da.name, name_b=db.name)


# ---------------------------------------------------------------------------------------------- agent consent
@route("GET", "/api/documents/{id}/agents")
def agent_status(app: App, req: Request) -> Any:
    doc = app.ws.get(req.params["id"])
    return {"consent": app.consent.is_granted(doc.id), "mode": app.settings.agents,
            "purpose": agent_calls.PURPOSE,
            "explicit": app.consent.is_explicit(doc.id),
            "explanation": "When on, text from this document may be passed to SLAI agents (Privacy first, then Safety and Language) "
                           "for the features that use them. The agents run on this computer and nothing leaves it. "
                           "Turn it off here for a document you do not want any agent to read."}


@route("PUT", "/api/documents/{id}/agents")
def agent_consent(app: App, req: Request) -> Any:
    doc = app.ws.get(req.params["id"])
    allow = req.json().get("allow")
    if not isinstance(allow, bool):
        raise ValidationFailed("Say whether to allow or not")
    if allow:
        note = app.consent.grant(app.svc, doc.id)
        return {"consent": True, "privacy": note.public()}
    app.consent.revoke(app.svc, doc.id)
    return {"consent": False}


# ---------------------------------------------------------------------------------------------- assistant
@route("GET", "/api/assistant/tools")
def assistant_tools(app: App, req: Request) -> Any:
    return {"tools": [t.public() for t in TOOLS.values()]}


@route("POST", "/api/assistant/message")
def assistant_message(app: App, req: Request) -> Any:
    b = req.json()
    text = b.get("text")
    if not isinstance(text, str):
        raise ValidationFailed("Type a request")
    active = b.get("activeDoc")
    return assistant_of(app).message(text, active if isinstance(active, str) else None)


@route("POST", "/api/assistant/confirm")
def assistant_confirm(app: App, req: Request) -> Any:
    b = req.json()
    pid = b.get("id")
    if not isinstance(pid, str):
        raise ValidationFailed("Missing proposal")
    inputs = b.get("inputs")
    return assistant_of(app).confirm(pid, inputs if isinstance(inputs, dict) else None)


@route("POST", "/api/assistant/reject")
def assistant_reject(app: App, req: Request) -> Any:
    pid = req.json().get("id")
    if not isinstance(pid, str):
        raise ValidationFailed("Missing proposal")
    return assistant_of(app).reject(pid)
