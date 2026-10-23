"""Concrete uses of the SLAI agents. Each function returns an :class:`AgentNote` that says what happened - used, skipped (and why),
unavailable or failed - and carries only what the agent actually returned. Nothing is invented when an agent is missing:
the caller continues with the deterministic result and shows the note.

Roles (see the agent matrix in the docs):

* Privacy       gate: document text is passed to other agents only if the Privacy agent allows it (and the person consented)
* Safety        reviews untrusted document text before it is shown to a language model
* Language      intent classification, passage re-ranking, optional abstractive summary (labelled as machine-written)
* Quality       reviews per-page / per-record extraction quality
* Observability receives job telemetry (latency, outcome)
* Handler       normalises job failures into the canonical failure schema (category, severity, retryable)
* Reader        its engines do the parsing / optimising (see bridge.reader); status shown in the matrix
* Planning      not used: Documaster plans its own fixed-dependency steps (stated in the matrix)
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ..errors import AgentUnavailable, DocumasterError
from ..textstats import tokenize

SUBJECT = "local-user"
PURPOSE = "document_processing"


@dataclass
class AgentNote:
    role: str
    status: str                      # used | skipped | unavailable | failed
    reason: str | None = None
    output: Any = None
    ms: int = 0
    agent: str | None = None

    def public(self) -> dict[str, Any]:
        return {"role": self.role, "agent": self.agent, "status": self.status, "reason": self.reason, "output": self.output, "ms": self.ms}


def _run(svc: Any, role: str, fn: Any, budget: float | None = None) -> AgentNote:
    t0 = time.perf_counter()
    gw = svc.agents
    if svc.settings.agents == "none":
        return AgentNote(role, "skipped", "Agents are switched off (--agents none)")
    try:
        out = gw.call(role, fn, budget)
    except AgentUnavailable as exc:
        if svc.settings.agents == "strict":
            raise
        return AgentNote(role, "unavailable", exc.details.get("reason") or exc.message, ms=round((time.perf_counter() - t0) * 1000))
    except DocumasterError as exc:
        return AgentNote(role, "failed", exc.message, ms=round((time.perf_counter() - t0) * 1000))
    except Exception as exc:  # noqa: BLE001 - an agent failing must not break the feature
        return AgentNote(role, "failed", f"{type(exc).__name__}: {str(exc)[:240]}", ms=round((time.perf_counter() - t0) * 1000))
    slot = gw.slots[role]
    return AgentNote(role, "used", output=out, ms=round((time.perf_counter() - t0) * 1000), agent=slot.cls)


def _brief(value: Any, limit: int = 1200) -> Any:
    try:
        text = json.dumps(value, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return value if len(text) <= limit else text[:limit] + "…"


# ---------------------------------------------------------------------------------------------- privacy
class Consent:
    """Whether SLAI agents may read a document's text. The agents run on this computer and the Privacy agent still decides on every use.

    ``default`` is the global setting (on unless Documaster was started with ``--ask-before-agents`` or agents are off); a person's explicit
    choice for one document overrides it. The permission is recorded with the Privacy agent the first time a document is used.
    """

    def __init__(self, default: bool = False) -> None:
        self.default = default
        self.explicit: dict[str, bool] = {}
        self.registered: dict[str, float] = {}
        self.lock = threading.Lock()

    def is_granted(self, doc_id: str) -> bool:
        with self.lock:
            return self.explicit.get(doc_id, self.default)

    def is_explicit(self, doc_id: str) -> bool:
        with self.lock:
            return doc_id in self.explicit

    def register(self, svc: Any, doc_id: str) -> AgentNote:
        """Record the permission with the Privacy agent (it refuses to evaluate text without one)."""
        stamp = time.time()
        note = _run(svc, "privacy", lambda a: {
            "consent": a.register_consent(subject_id=SUBJECT, purpose=PURPOSE, status="granted",
                                          artifact_ref=f"documaster-ui:{doc_id}:{int(stamp)}", legal_basis="consent",
                                          allowed_contexts=["local"], allowed_processors=["documaster"]),
            "purpose": a.bind_purpose(subject_id=SUBJECT, purpose=PURPOSE, source_context="local", allowed_contexts=["local"],
                                      allowed_actions=["process", "analyze"])})
        if note.status == "used":
            with self.lock:
                self.registered[doc_id] = stamp
            note.output = {"recorded": True}
        return note

    def grant(self, svc: Any, doc_id: str) -> AgentNote:
        with self.lock:
            self.explicit[doc_id] = True
        return self.register(svc, doc_id)

    def revoke(self, svc: Any, doc_id: str) -> None:
        with self.lock:
            self.explicit[doc_id] = False
            self.registered.pop(doc_id, None)


def privacy_gate(svc: Any, consent: Consent, doc_id: str, text: str, pii_count: int) -> dict[str, Any]:
    """Decide whether ``text`` may be handed to other agents. Returns ``{forward, decision, reason, note}``."""
    if not consent.is_granted(doc_id):
        why = ("You turned off agents for this document." if consent.is_explicit(doc_id)
               else "Agents are not allowed to read documents (System page, or Documaster was started with --ask-before-agents / --agents none).")
        return {"forward": False, "decision": "no-consent", "reason": why + " Turn on 'Let SLAI agents read this document' to use agent features.",
                "note": None}
    if doc_id not in consent.registered:
        consent.register(svc, doc_id)
    sample = text[:20000]
    note = _run(svc, "privacy", lambda a: a.evaluate_privacy(
        {"content": sample}, subject_id=SUBJECT, purpose=PURPOSE, action="process", source_context="local",
        destination_context="local", required_processor="documaster"), 20)
    if note.status == "used" and isinstance(note.output, dict):
        decision = str(note.output.get("decision", ""))
        ident = (note.output.get("stages") or {}).get("identification") or {}
        info = {"decision": decision, "sensitivity": ident.get("sensitivity_score"), "entities": ident.get("entity_count"),
                "summary": note.output.get("summary")}
        note.output = info
        if decision == "allow":
            return {"forward": True, "decision": "allow", "reason": None, "note": note}
        if decision == "modify":
            # Verified against the real Privacy agent: it answers "modify" for text with personal data and returns a sanitised payload in
            # which the content is replaced by a token. That cannot be summarised or searched, so the original text is not forwarded.
            return {"forward": False, "decision": "modify",
                    "reason": "The Privacy agent only allows this text to be processed in a sanitised form, in which the content is replaced "
                              f"by a token (sensitivity {ident.get('sensitivity_score')}, {ident.get('entity_count')} entities). "
                              "A tokenised text cannot be summarised, so nothing is sent to other agents.", "note": note}
        return {"forward": False, "decision": decision or "unknown",
                "reason": f"The Privacy agent decided '{decision}': this text contains data it protects "
                          f"(sensitivity {ident.get('sensitivity_score')}, {ident.get('entity_count')} entities). It is not sent to other agents.",
                "note": note}
    # Privacy agent not usable: the local detector decides, conservatively.
    if pii_count:
        return {"forward": False, "decision": "blocked-locally", "reason": f"The Privacy agent is not available ({note.reason}) and the text "
                f"contains {pii_count} item(s) that look like personal data, so it is not sent to other agents.", "note": note}
    return {"forward": True, "decision": "allow-locally", "reason": None,
            "note": AgentNote("privacy", note.status, f"{note.reason}. Allowed because the local detector found no personal data patterns.")}


# ---------------------------------------------------------------------------------------------- safety
def safety_review(svc: Any, text: str) -> AgentNote:
    note = _run(svc, "safety", lambda a: a.perform_task(text[:20000], {"context_type": "document_content"}), 30)
    if note.status == "used" and isinstance(note.output, dict):
        keep = {k: note.output[k] for k in ("decision", "verdict", "status", "risk_level", "overall_risk", "overall_score", "blockers",
                                             "warnings", "summary", "assessment_id") if k in note.output}
        note.output = keep or _brief(note.output, 600)
    return note


def safety_blocks(note: AgentNote) -> bool:
    if note.status != "used" or not isinstance(note.output, dict):
        return False
    blockers = note.output.get("blockers")
    decision = str(note.output.get("decision") or note.output.get("verdict") or "").lower()
    return bool(blockers) or decision in ("block", "deny", "denied", "unsafe", "reject", "rejected")


# ---------------------------------------------------------------------------------------------- language
def classify_intent(svc: Any, text: str, labels: list[str]) -> AgentNote:
    note = _run(svc, "language", lambda a: a.classify_text(text, labels=labels), 25)
    if note.status != "used":
        return note
    label, score = _extract_label(note.output, labels)
    if label is None:
        return AgentNote("language", "failed", "The language agent's answer was not in a recognised form", ms=note.ms, agent=note.agent)
    note.output = {"label": label, "score": score, "raw": _brief(note.output, 400)}
    return note


def _extract_label(out: Any, labels: list[str]) -> tuple[str | None, float | None]:
    if isinstance(out, str) and out in labels:
        return out, None
    if not isinstance(out, dict):
        return None, None
    for key in ("label", "predicted_label", "top_label", "class", "prediction"):
        if out.get(key) in labels:
            score = out.get("score", out.get("confidence", out.get("probability")))
            return out[key], float(score) if isinstance(score, (int, float)) else None
    scores = out.get("scores") or out.get("probabilities") or out.get("labels")
    if isinstance(scores, dict):
        pairs = [(k, float(v)) for k, v in scores.items() if k in labels and isinstance(v, (int, float))]
        if pairs:
            k, v = max(pairs, key=lambda kv: kv[1])
            return k, v
    if isinstance(scores, list):
        best = None
        for item in scores:
            if isinstance(item, dict):
                lab = item.get("label")
                sc = item.get("score")
                if lab in labels and isinstance(sc, (int, float)) and (best is None or sc > best[1]):
                    best = (lab, float(sc))
        if best:
            return best
    return None, None


def rerank(svc: Any, query: str, candidates: list[str], top_k: int) -> tuple[AgentNote, list[int] | None]:
    note = _run(svc, "language", lambda a: a.rerank_texts(query, candidates, top_k=top_k), 30)
    if note.status != "used":
        return note, None
    order = _extract_order(note.output, len(candidates), candidates)
    if order is None:
        return AgentNote("language", "failed", f"The re-ranking answer was not in a recognised form: {_brief(note.output, 300)}",
                         ms=note.ms, agent=note.agent), None
    note.output = {"order": order[:top_k]}
    return note, order


_INDEX_KEYS = ("index", "idx", "id", "position", "candidate_index", "original_index", "source_index", "doc_index", "document_index")
_TEXT_KEYS = ("text", "candidate", "document", "passage", "content")


def _item_index(it: Any, n: int, candidates: list[str] | None) -> int | None:
    if isinstance(it, bool):
        return None
    if isinstance(it, int):
        return it if 0 <= it < n else None
    if isinstance(it, dict):
        for key in _INDEX_KEYS:
            v = it.get(key)
            if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < n:
                return v
        if candidates:
            for key in _TEXT_KEYS:
                v = it.get(key)
                if isinstance(v, str) and v in candidates:
                    return candidates.index(v)
        return None
    if isinstance(it, (list, tuple)) and it:
        return _item_index(it[0], n, candidates)
    if isinstance(it, str) and candidates and it in candidates:
        return candidates.index(it)
    return None


def _extract_order(out: Any, n: int, candidates: list[str] | None = None) -> list[int] | None:
    """Best-effort reading of a re-ranking answer: a ranked list of indices / records / texts, or one score per candidate."""
    lists: list[list[Any]] = []
    if isinstance(out, list):
        lists.append(out)
    elif isinstance(out, dict):
        for key in ("ranked", "results", "ranking", "ranked_indices", "indices", "order", "items", "hits", "matches", "candidates"):
            if isinstance(out.get(key), list):
                lists.append(out[key])
        lists.extend(v for v in out.values() if isinstance(v, list) and v not in lists)
    for items in lists:
        if not items:
            continue
        if len(items) == n and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in items) and not all(
                isinstance(x, int) and 0 <= x < n for x in items):
            return sorted(range(n), key=lambda i: -float(items[i]))          # one score per candidate, in candidate order
        order = [_item_index(it, n, candidates) for it in items]
        if all(i is not None for i in order) and order:
            return [int(i) for i in order if i is not None]
    return None


def abstract_summary(svc: Any, text: str, max_words: int) -> AgentNote:
    note = _run(svc, "language", lambda a: a.summarize_text(text[:12000], max_length=max_words), 60)
    if note.status != "used":
        return note
    out = note.output if isinstance(note.output, str) else (note.output or {}).get("summary") if isinstance(note.output, dict) else None
    if not isinstance(out, str) or len(tokenize(out)) < 5:
        return AgentNote("language", "failed", "The language agent returned no usable summary", ms=note.ms, agent=note.agent)
    if len(tokenize(out)) >= len(tokenize(text[:12000])):
        return AgentNote("language", "failed", "The language agent's summary was not shorter than the text", ms=note.ms, agent=note.agent)
    content = [w for w in tokenize(out) if len(w) > 3]
    source = set(tokenize(text[:12000]))
    grounded = (sum(1 for w in content if w in source) / len(content)) if content else 0.0
    if grounded < 0.6:
        return AgentNote("language", "failed", f"The language agent's summary was discarded: only {grounded * 100:.0f}% of its words appear in the "
                         f"document, so it cannot be trusted as a summary. It said: \"{out.strip()[:240]}\"", ms=note.ms, agent=note.agent)
    note.output = {"text": out.strip(), "grounded": round(grounded, 2)}
    return note


# ---------------------------------------------------------------------------------------------- quality / observability / handler
def quality_review(svc: Any, records: list[dict[str, Any]], dataset_id: str = "documaster") -> AgentNote:
    note = _run(svc, "quality", lambda a: a.perform_task({"operation": "evaluate_batch", "dataset_id": dataset_id, "batch_id": f"{dataset_id}:{int(time.time())}",
                                                           "source": "documaster", "records": records}), 40)
    if note.status == "used":
        note.output = _brief(note.output, 1500)
    return note


def observe(svc: Any, operation: str, duration_ms: float, ok: bool, message: str = "") -> None:
    """Fire-and-forget telemetry to the Observability agent (never blocks or fails a job)."""
    if svc.settings.agents == "none":
        return

    def go() -> None:
        _run(svc, "observability", lambda a: a.perform_task({
            "task_name": f"documaster.{operation}", "agent_name": "documaster", "operation_name": operation, "service": "documaster",
            "latencies": [{"name": operation, "value_ms": float(duration_ms), "subject": "documaster"}],
            "events": [{"name": "job.finished", "status": "ok" if ok else "error", "message": message[:200]}]}), 10)

    threading.Thread(target=go, daemon=True, name="dm-observe").start()


def normalise_failure(svc: Any, exc: BaseException, operation: str) -> dict[str, Any] | None:
    """Handler agent's canonical description of a failure (category, severity, retryable); ``None`` if unavailable."""
    note = _run(svc, "handler", lambda a: a.failure_normalization(error=exc, context={"agent": "documaster", "operation": operation}), 10)
    if note.status != "used" or not isinstance(note.output, dict):
        return None
    return {k: note.output.get(k) for k in ("schema", "type", "category", "severity", "retryable", "policy_action") if k in note.output}


# ---------------------------------------------------------------------------------------------- quality on extraction
def page_records(units: list[Any], pages: int | None) -> list[dict[str, Any]]:
    """One record per page: how much text was extracted. Statistics only - no document text is sent to the Quality agent."""
    per: dict[int, list[int]] = {}
    for u in units:
        page = getattr(u, "page", None) or 1
        text = getattr(u, "text", "") or ""
        row = per.setdefault(int(page), [0, 0])
        row[0] += len(text)
        row[1] += len(tokenize(text))
    count = pages or (max(per) if per else 0)
    return [{"page": n, "characters": per.get(n, [0, 0])[0], "words": per.get(n, [0, 0])[1], "empty": per.get(n, [0, 0])[1] == 0}
            for n in range(1, min(count, 500) + 1)]


# ---------------------------------------------------------------------------------------------- self-test
SAMPLE = ("The committee met on Tuesday to review the annual budget. Revenue grew by eight percent while costs stayed flat. "
          "The board approved a new hiring plan for the engineering team and asked for a progress report in March. "
          "Several members raised concerns about supplier delays, and the finance director promised a detailed risk analysis "
          "before the next meeting.")


PII_SAMPLE = "Contact Jan de Vries at jan.devries@example.com or +31 6 12345678. Pay to IBAN NL91ABNA0417164300 before Friday."


def self_test(svc: Any, roles: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """Make one real, harmless call per agent with a fixed sample text and record what came back. Nothing is faked: a failure is the result."""
    wanted = roles or ["privacy", "safety", "language", "quality"]
    results: dict[str, dict[str, Any]] = {}

    def finish(role: str, ok: bool | None, detail: str, ms: int) -> None:
        res = {"ok": ok, "detail": detail[:400], "ms": ms, "at": time.time()}
        results[role] = res
        svc.agents.slots[role].test = res

    for role in wanted:
        t0 = time.perf_counter()
        elapsed = lambda: round((time.perf_counter() - t0) * 1000)  # noqa: E731
        if svc.settings.agents == "none":
            finish(role, None, "agents are switched off (--agents none)", 0)
            continue
        if role == "privacy":
            c = Consent(default=True)
            c.register(svc, "self-test")
            plain = privacy_gate(svc, c, "self-test", SAMPLE, 0)
            personal = privacy_gate(svc, c, "self-test", PII_SAMPLE, 3)
            note = plain.get("note")
            used = note is not None and note.status == "used"
            sees = plain["decision"] == "allow" and personal["decision"] not in ("allow", "allow-locally")
            detail = (f"plain text -> '{plain['decision']}'; text with an e-mail address, phone number and IBAN -> '{personal['decision']}'"
                      if used else f"not usable: {(note.reason if note else None) or plain.get('reason')}")
            if used and not sees:
                detail += (" (expected: plain text allowed, personal data not)")
            finish(role, used and sees, detail, elapsed())
        elif role == "safety":
            note = safety_review(svc, SAMPLE)
            finish(role, note.status == "used", f"answered: {_brief(note.output, 200)}" if note.status == "used" else f"{note.status}: {note.reason}", elapsed())
        elif role == "language":
            parts: list[str] = []
            ok = True
            n1 = classify_intent(svc, "summarise this document", ["summarize", "convert", "merge"])
            ok &= n1.status == "used"
            parts.append(f"classify: {n1.output['label']}" if n1.status == "used" else f"classify {n1.status}: {n1.reason}")
            n2, order = rerank(svc, "budget", ["The weather was mild.", "Revenue and the annual budget were reviewed.", "A new hire."], 2)
            ok &= n2.status == "used"
            parts.append(f"rerank: {order}" if n2.status == "used" else f"rerank {n2.status}: {n2.reason}")
            n3 = abstract_summary(svc, SAMPLE, 40)
            ok &= n3.status == "used"
            parts.append("summary: ok" if n3.status == "used" else f"summary {n3.status}: {n3.reason}")
            finish(role, bool(ok), "; ".join(parts), elapsed())
        elif role == "quality":
            note = quality_review(svc, [{"page": 1, "characters": len(SAMPLE), "words": len(tokenize(SAMPLE)), "empty": False},
                                        {"page": 2, "characters": 0, "words": 0, "empty": True}])
            finish(role, note.status == "used", f"answered: {_brief(note.output, 200)}" if note.status == "used" else f"{note.status}: {note.reason}", elapsed())
        else:
            finish(role, None, "only used when job telemetry is switched on; not tested", 0)
    return results
