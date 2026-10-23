# Report B - Agent matrix

_Generated 2026-10-22 23:57 by `tools/make_reports.py` from the running code._

Which SLAI agents Documaster uses, for what, and **what has actually been verified against the real agent**. 'Verified with the real agent' means the call was made through SLAI's own `AgentFactory` and answered (see the record below); 'tested with stand-ins' means Documaster's handling of the answer is tested with fakes only.

| Role | How Documaster uses it | Verified with the real agent? | Stand-in tests | Note |
|---|---|---|---|---|
| privacy | Gate: consent is registered; document text goes to other agents only if the Privacy agent allows it. 'modify' is treated as 'do not forward'. Without the agent, text containing personal data stays local. | yes - register_consent and bind_purpose accepted; evaluate_privacy answered decision 'modify' for text containing an email address, phone number and IBAN, with a sanitised payload in which the whole content is replaced by a token; without a registered consent it raises ConsentArtifactMissingError (PRV-3007). | tests_py/test_agents.py (GateTests) | Documaster therefore registers the person's consent first, and treats 'modify' as: do not forward the text. |
| language | Optional: tie-break for ambiguous requests, passage re-ranking, an abstractive summary that is labelled as machine-written. Used when agents may read the document (default on) and the Privacy gate passes; a summary whose words are not in the document is discarded. | NO - Could not be loaded: ModuleNotFoundError: No module named 'torch'. | tests_py/test_agents.py (AnswerHandlingTests) | Documaster's use of it (classify_text, rerank_texts, summarize_text) is written against the agent's methods but has NOT been run against the real agent. |
| safety | Reviews untrusted document text before it may be shown to a language model; a block stops the text going further. | NO - Could not be loaded: ModuleNotFoundError: No module named 'torch'. | tests_py/test_agents.py | Documaster's use of it (perform_task on document text) has NOT been run against the real agent. |
| quality | Reviews per-page extraction statistics of a PDF (Structure tab); no text is sent. Result shown as the agent returned it. | NO - Could not be initialised: SQLite Quality Memory missing (quality_memory.sqlite3); its migration script was not in the checkout copy. | tests_py/test_agents.py (gateway, self-test); real call: System page "Test agents now" | Not exercised. |
| observability | Fire-and-forget job telemetry. Off by default (`--agent-telemetry`). | yes - perform_task accepted a latency/event record and returned a trace (status 'degraded', incident_level 'critical' for a single 120 ms sample: Documaster does not interpret this). | tests_py/test_agents.py | Only used when telemetry is switched on (off by default). |
| handler | Normalises a failed job into the canonical failure schema. Off by default (`--agent-telemetry`). | yes - failure_normalization answered schema handler.failure.v2 with type, category, severity, retryable, policy_action. | tests_py/test_agents.py | Only used when telemetry is switched on (off by default). |
| reader | Documaster uses the Reader ENGINES directly: parser, detector, integrity (verifies every written file), compression. The ReaderAgent itself is not called. | yes - capabilities() answered (modes: capabilities, compress, convert, decompress, describe, detect, jobs, merge, parse, pipeline, read, recover, resume, salvage, validate, ...) | live engine status below | Documaster calls the Reader ENGINES (parser, detector, integrity, compression) directly, not the ReaderAgent. |
| planning | Not used. Documaster's steps have fixed dependencies; the interface says so instead of pretending to plan. | NO - Could not be loaded: ModuleNotFoundError: No module named 'joblib'. | - | Not used by design: Documaster's steps have fixed dependencies, so there is nothing to plan. |

## Record of real-agent probes

Recorded 2026-10-22. tools/probe_agents.py <role> run against a COPY of the SLAI checkout (never the live repository), through SLAI's own AgentFactory.

* **device**: the person's computer (Linux VM of the desktop app): Python 3.10, no torch, no joblib
* **cloud**: development container: Python 3.13, SLAI checkout mirror; the agents were not probed there

## Live gateway state when this report was generated

Agent mode `none` (the report generator never starts agents).

| Role | State | Reason |
|---|---|---|
| language | disabled | agents are disabled (--agents none) |
| reader | ready |  |
| privacy | disabled | agents are disabled (--agents none) |
| safety | disabled | agents are disabled (--agents none) |
| quality | disabled | agents are disabled (--agents none) |
| observability | disabled | agents are disabled (--agents none) |
| handler | disabled | agents are disabled (--agents none) |
| planning | disabled | agents are disabled (--agents none) |

Reader engines: parser: available, detector: available, integrity: available, compression: available

## Rules that apply to every agent call

* A call has a time budget; an overrunning call is abandoned.
* Three failures in a row pause the role for a cool-down, and the interface says so.
* An unavailable agent is reported with its reason and the deterministic result is used; no answer is invented.
* Every agent contribution is labelled with the agent's name and role; machine-written text is marked as such.
* `--agents none` switches all of it off; `--agents strict` turns a missing agent into an error.
