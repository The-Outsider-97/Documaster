#!/usr/bin/env python3
"""Generate the Documaster reports (docs/report-A ... report-H) from the running code, not from memory.

    python3 tools/make_reports.py [--slai-root PATH] [--run-tests] [--out docs]

* A-C, E are read from the live capability matrix, the operation registry, the assistant's tool list and the security constants.
* B merges the live agent gateway state (agents are NOT started here) with ``docs/agent_verification.json``, the record of the
  real-agent probes made with ``tools/probe_agents.py``.
* F is produced by running the test suites when ``--run-tests`` is given, otherwise it keeps the last recorded run.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "tests_py"))


def table(headers: list[str], rows: list[list[str]]) -> str:
    esc = lambda s: str(s).replace("|", "\\|").replace("\n", " ")        # noqa: E731
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def yn(v: bool) -> str:
    return "yes" if v else "no"


def header(title: str, stamp: str, intro: str) -> str:
    return f"# {title}\n\n_Generated {stamp} by `tools/make_reports.py` from the running code._\n\n{intro}\n\n"


# ------------------------------------------------------------------------------------------------------------------ A
def report_a(h, stamp: str) -> str:
    from slai.assistant.engine import TOOLS
    from slai.pdfops.ops import OPS
    from slai.api_analysis import CHECKS
    caps = h.call("GET", "/api/capabilities")
    tools = caps["tools"]
    s = header("Report A - Capability matrix", stamp,
               "What each workspace can do on **this** machine. Availability is computed from what is installed; nothing is promised that the "
               "machine cannot do. A feature is listed with the code that does it and the test that exercises it.")
    s += "## Environment\n\n" + table(["Component", "State"], [
        ["LibreOffice (layout-preserving Office conversions, RTF/DOC reading)", tools["libreoffice"] or "not found"],
        ["Tesseract OCR", (tools["tesseract"] or "not found") + (f" (languages: {', '.join(tools['tesseractLanguages'])})" if tools["tesseract"] else "")],
        ["qpdf", tools["qpdf"] or "not found"]] + [[f"Python module `{m}`", yn(v)] for m, v in tools["modules"].items()]) + "\n\n"
    s += "## Workspaces\n\n" + table(["Workspace", "Features", "Evidence"], [
        ["Word Counter", "Words, characters, sentences, paragraphs, reading/speaking time, language guess (labelled as a guess), top terms, goals; typed text or files; OCR for scanned PDFs when Tesseract is present; CJK counted per character, counting rules shown", "tests_py/test_units.py (CountingTests); tests_e2e"],
        ["Converter", "Any supported source to any supported target through the SLDC document model or LibreOffice; every output re-opened and compared; kept/lost lists per file; encrypted SLDC; nothing overwritten", "tests_py/test_convert_merge.py (ConvertTests)"],
        ["Merger & Page Organizer", "Merge PDF/Word/ODT/RTF/HTML/Markdown/text/SLDC/PNG/JPEG; page cards with drag-and-drop, keyboard moves, exclude, rotate, duplicate, blank pages; PDF pages copied unchanged; merge into other formats through logical pages", "tests_py/test_convert_merge.py (MergeTests); tests/organizer.test.ts; tests_e2e"],
        ["PDF Editor", "View, thumbnails, page operations, annotations, text/image stamps, watermark, page numbers, forms (fill, flatten), bookmarks, metadata, password, crop, compress, OCR, true redaction; per-document undo/redo; save a copy", "tests_py/test_pdf_ops.py, test_redaction.py, test_forms_ocr.py; tests_e2e"],
        ["Reader & Analysis", "Outline, structure checks, keywords, readability, repeated text, required sections (presets), personal-data scan, comparison of two documents, search with evidence, extractive summary", "tests_py/test_units.py (PiiTests); tests_e2e"],
        ["AI Assistant", "Read-only requests answered immediately with evidence; every change is a confirmed, single-use, expiring proposal", "tests_py/test_assistant.py; tests_e2e"]]) + "\n\n"
    s += "## PDF operations (from the operation registry)\n\n" + table(["Operation", "Permission it needs when the PDF is protected"],
                                                                       [[f"`{k}`", v[1] or "-"] for k, v in sorted(OPS.items())]) + "\n\n"
    s += "## Assistant tools\n\n" + table(["Tool", "Changes files?", "What it does"],
                                          [[f"`{t.name}`", "asks first" if t.changes else "no (immediate)", t.description] for t in TOOLS.values()]) + "\n\n"
    s += "## Analysis checks\n\n" + ", ".join(f"`{c}`" for c in CHECKS) + "\n"
    return s


# ------------------------------------------------------------------------------------------------------------------ B
def report_b(h, stamp: str) -> str:
    rec = json.loads((APP / "docs" / "agent_verification.json").read_text(encoding="utf-8"))
    live = h.call("GET", "/api/agents")
    s = header("Report B - Agent matrix", stamp,
               "Which SLAI agents Documaster uses, for what, and **what has actually been verified against the real agent**. "
               "'Verified with the real agent' means the call was made through SLAI's own `AgentFactory` and answered (see the record below); "
               "'tested with stand-ins' means Documaster's handling of the answer is tested with fakes only.")
    use = {
        "privacy": ("Gate: consent is registered; document text goes to other agents only if the Privacy agent allows it. 'modify' is treated as 'do not forward'. Without the agent, text containing personal data stays local.", "tests_py/test_agents.py (GateTests)"),
        "language": ("Optional: tie-break for ambiguous requests, passage re-ranking, an abstractive summary that is labelled as machine-written. Used when agents may read the document (default on) and the Privacy gate passes; a summary whose words are not in the document is discarded.", "tests_py/test_agents.py (AnswerHandlingTests)"),
        "safety": ("Reviews untrusted document text before it may be shown to a language model; a block stops the text going further.", "tests_py/test_agents.py"),
        "quality": ("Reviews per-page extraction statistics of a PDF (Structure tab); no text is sent. Result shown as the agent returned it.", "tests_py/test_agents.py (gateway, self-test); real call: System page \"Test agents now\""),
        "observability": ("Fire-and-forget job telemetry. Off by default (`--agent-telemetry`).", "tests_py/test_agents.py"),
        "handler": ("Normalises a failed job into the canonical failure schema. Off by default (`--agent-telemetry`).", "tests_py/test_agents.py"),
        "reader": ("Documaster uses the Reader ENGINES directly: parser, detector, integrity (verifies every written file), compression. The ReaderAgent itself is not called.", "live engine status below"),
        "planning": ("Not used. Documaster's steps have fixed dependencies; the interface says so instead of pretending to plan.", "-")}
    rows = []
    for role, (what, tests) in use.items():
        r = rec["roles"][role]
        real = "yes - " + r["result"] if r["loaded"] else "NO - " + r["result"]
        rows.append([role, what, real, tests, r["note"]])
    s += table(["Role", "How Documaster uses it", "Verified with the real agent?", "Stand-in tests", "Note"], rows) + "\n\n"
    s += f"## Record of real-agent probes\n\nRecorded {rec['recorded']}. {rec['how']}\n\n" + "\n".join(f"* **{k}**: {v}" for k, v in rec["machines"].items()) + "\n\n"
    s += "## Live gateway state when this report was generated\n\n" + f"Agent mode `{live['mode']}` (the report generator never starts agents).\n\n"
    s += table(["Role", "State", "Reason"], [[k, v["state"], v["error"] or ""] for k, v in live["agents"].items()]) + "\n\n"
    s += "Reader engines: " + ", ".join(f"{k}: {'available' if v['available'] else 'unavailable'}" for k, v in h.app.svc.reader.status()["engines"].items()) + "\n\n"
    s += ("## Rules that apply to every agent call\n\n* A call has a time budget; an overrunning call is abandoned.\n* Three failures in a row pause the role for a cool-down, and the interface says so.\n"
          "* An unavailable agent is reported with its reason and the deterministic result is used; no answer is invented.\n* Every agent contribution is labelled with the agent's name and role; machine-written text is marked as such.\n"
          "* `--agents none` switches all of it off; `--agents strict` turns a missing agent into an error.\n")
    return s


# ------------------------------------------------------------------------------------------------------------------ C
def report_c(h, stamp: str) -> str:
    caps = h.call("GET", "/api/capabilities")
    s = header("Report C - Format matrix", stamp, "Reading and writing routes, with the backend that does the work and what each route cannot do.")
    s += "## Reading\n\n" + table(["Format", "Available", "Backend", "Limits"], [[r["label"], yn(r["available"]), r["backend"], "; ".join(r["limits"])] for r in caps["read"].values()]) + "\n\n"
    s += "## Writing\n\n" + table(["Format", "Available", "Backend", "Limits"], [[r["label"], yn(r["available"]), r.get("backend", ""), "; ".join(r["limits"])] for r in caps["write"].values()]) + "\n\n"
    s += ("## How outputs are checked\n\n1. SLAI Reader `IntegrityEngine.verify_output` where the Reader knows the format.\n2. The file is re-opened with a real parser for its format.\n"
          "3. The words of the re-opened file are compared with the words of the document model that was written (coverage and order).\n\n"
          "Layout, fonts and appearance are not judged and the reports say so. A failed check deletes the output.\n")
    return s


# ------------------------------------------------------------------------------------------------------------------ D (static, checked by tests)
def report_d(h, stamp: str) -> str:
    return header("Report D - SLDC integration", stamp, "SLDC (`src/utils/sldc.py`) is the authoritative document model. Documaster defines no competing schema.") + """\
## Pipeline

`read` -> `IRResult(SLDCPackage)` -> `writer` -> `verify_written` -> atomic commit (a file appears in the output folder only after it passed).

* `slai/docmodel/ir.py` builds and reads SLDC structures through the documented fields only (`blocks`, `spans`, `styles`, `sections`, `references`, ...).
* `slai/bridge/__init__.py` imports `src.utils.sldc` from the SLAI checkout; if it is missing Documaster refuses to start and says so.
* SLDC is both a source and a target of the converter; an encrypted SLDC needs its password; the encryption is SLDC's own.

## Merging and page arrangement

SLDC has no fixed pages. A *logical page* is a run of blocks starting at the document start, a block marked `page_break_before` (PDF imports mark every source page), a section change, or - when asked - a heading.
The merge follows the format's rules: colliding block ids are renamed and `xref` spans follow; identical styles are shared, different ones renamed; sections stay contiguous (cloned when an arrangement splits them);
notes, comments and images travel only with the blocks that use them, so an excluded page leaves nothing behind; provenance is kept and a `documaster.merge` entry records every input with its SHA-256.

## Signatures

Signatures sign one revision of one document. A merge or edit creates a new revision, so signatures are **not carried over**, and Documaster **never signs or re-signs** anything: there is no such operation (`tests_py/test_pdf_ops.py::test_documents_are_not_re_signed`) and signature form fields are refused (`test_forms_ocr.py`).

## Evidence

`tests_py/test_convert_merge.py`: SLDC round trip, encrypted SLDC, markdown headings surviving as headings.
"""


# ------------------------------------------------------------------------------------------------------------------ E
def report_e(h, stamp: str) -> str:
    from slai.server import CSP
    return header("Report E - Security", stamp, "Documents are untrusted input. The server is local and private. Each control below has a test.") + f"""\
## Server

| Control | How | Test |
|---|---|---|
| Loopback only | binds 127.0.0.1; any other `--host` is refused at start | `test_non_loopback_host_is_refused_at_startup` |
| Access token | random per launch; accepted as header, bearer token or the HttpOnly SameSite=Strict cookie set by the launch link; never accepted in an API query string | `test_api_needs_the_token`, `test_token_in_query_is_not_accepted_for_the_api`, `test_landing_link_sets_a_strict_cookie` |
| DNS rebinding | `Host` must be a loopback name with our port | `test_host_header_must_be_loopback_with_our_port` |
| CSRF | state-changing requests need `X-Documaster: 1`; a foreign `Origin` is refused; no CORS | `test_changes_need_the_request_marker`, `test_foreign_origin_is_refused`, `test_cross_origin_preflight_is_refused` |
| Content policy | `{CSP}`; `nosniff`; `no-referrer` | `test_security_headers_on_every_response` |
| Path handling | static files cannot leave the client folder; uploads are stored under a neutral name; names are sanitised; outputs are downloaded by id only | `test_static_files_cannot_escape_the_client_folder`, `test_upload_name_cannot_carry_a_path`, `test_unknown_output_cannot_be_downloaded` |
| Bad input | malformed JSON, non-object bodies, wrong content for the claimed type: clean 4xx errors | `test_malformed_json_is_a_clean_400`, `test_upload_rejects_a_file_that_is_not_what_its_name_says` |

## Documents

* Uploads are streamed to a private session folder, identified from their bytes, never executed. HTML scripts are ignored, never run. Limits on upload size, page count and text length apply.
* Passwords live in memory only; they are not written to disk, logs or reports. The assistant never takes a password from the chat text: it asks for it in the confirmation box.
* **Text inside a document is data.** The assistant quotes it with its location and never follows it (`test_instructions_inside_a_document_are_quoted_not_obeyed`; browser test with a hostile document).
* Every change by the assistant is a proposal that is single use, expires after 15 minutes and is void if the document changed meanwhile (`test_assistant.py`). The client builds all text with `textContent`; there is no `innerHTML`.
* Agents see document text only while agents are allowed to read documents (default on, switchable per document and globally, or `--agents none`) and the Privacy agent allows it on that use.

## Redaction

Pages with redactions are rebuilt as images, then checked: no text remains, the marked areas are solid black, and metadata is clean. A failed check deletes the output. Text that lies outside the visible page is removed with the page. (`test_redaction.py`.)

## Not covered

* Documaster does not defend against a hostile user on the same computer account.
* Malformed-file robustness relies on pypdf, pypdfium2, python-docx and LibreOffice; a crafted file may make a parser slow or fail. Jobs have a deadline and can be cancelled.
* No fuzzing campaign has been run.
"""


# ------------------------------------------------------------------------------------------------------------------ F
def run(cmd: list[str], env: dict[str, str]) -> tuple[int, str]:
    p = subprocess.run(cmd, cwd=APP, capture_output=True, text=True, env=env, timeout=900)
    return p.returncode, (p.stdout + p.stderr)


def report_f(stamp: str, run_tests: bool, root: str | None) -> str:
    cache = APP / "docs" / "test-results.json"
    if run_tests:
        env = {**os.environ, "SLAI_ROOT": root or os.environ.get("SLAI_ROOT", ""), "PYTHONDONTWRITEBYTECODE": "1"}
        res: dict[str, object] = {"when": stamp}
        rc, out = run([sys.executable, "-W", "ignore", "-m", "unittest", "discover", "-s", "tests_py"], env)
        m = re.search(r"Ran (\d+) tests? in ([\d.]+)s", out)
        res["python"] = {"ok": rc == 0, "tests": int(m.group(1)) if m else None, "seconds": float(m.group(2)) if m else None, "tail": out.strip().splitlines()[-1] if out.strip() else ""}
        rc, out = run(["npx", "tsx", "--test", "tests/*.test.ts"], env)
        g = lambda k: int(re.search(rf"^# {k} (\d+)", out, re.M).group(1)) if re.search(rf"^# {k} (\d+)", out, re.M) else None   # noqa: E731
        res["typescript"] = {"ok": rc == 0, "tests": g("tests"), "pass": g("pass"), "fail": g("fail")}
        rc, out = run(["npx", "tsc", "-p", "tsconfig.json", "--noEmit"], env)
        res["typecheck"] = {"ok": rc == 0, "output": out.strip()[:300]}
        rc, out = run([sys.executable, "-W", "ignore", "tests_e2e/run_browser.py"], env)
        res["browser"] = {"ok": rc == 0, "passes": len(re.findall(r"^PASS ", out, re.M)), "fails": len(re.findall(r"^FAIL ", out, re.M)), "skipped": out.startswith("SKIP")}
        cache.write_text(json.dumps(res, indent=2), encoding="utf-8")
    res = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
    s = header("Report F - Testing", stamp, f"Results of the last recorded run ({res.get('when', 'none recorded')}). Re-run with `python3 tools/make_reports.py --run-tests`.")
    py, ts, tc, br = res.get("python", {}), res.get("typescript", {}), res.get("typecheck", {}), res.get("browser", {})
    s += table(["Suite", "What it covers", "Result"], [
        ["Python (`tests_py`, stdlib `unittest`)", "security, redaction, conversion, merging, PDF operations, forms, compression, OCR, assistant, agent handling, counting, personal-data patterns, files, jobs, request understanding; real HTTP server, real files", f"{py.get('tests')} tests, {'all passed' if py.get('ok') else 'FAILURES'} ({py.get('seconds')} s)"],
        ["TypeScript (`tests/*.test.ts`, `tsx --test`)", "page organiser engine, API client with a mock fetch, formatting, geometry", f"{ts.get('tests')} tests, {ts.get('pass')} passed, {ts.get('fail')} failed"],
        ["Type check (`tsc --noEmit`, strict)", "the whole client", "clean" if tc.get("ok") else tc.get("output", "not run")],
        ["Browser (`tests_e2e/run_browser.py`, Chromium)", "every workspace, both themes, merge, convert, editor (rotate, undo, highlight, redact + download), reader, assistant incl. hostile document, theme", f"{br.get('passes')} checks passed, {br.get('fails')} failed" + (" (skipped: Playwright missing)" if br.get("skipped") else "")]]) + "\n\n"
    s += ("## What was found by testing and fixed\n\n"
          "* A permission warning was shown on unprotected PDFs (panel built before a document was open).\n"
          "* The assistant read file names like `pii.pdf` as requests, and converted *every* open document with the same name; it now resolves names to one document or asks.\n"
          "* On some Windows set-ups the final file rename was refused (\"cannot move the file to a different drive\") and every upload failed; it now falls back to copy-and-remove (`files.replace_file`), and the flush after that copy is best effort because Windows refuses `fsync` on a read-only handle. My first fix missed the second error; the test now covers both.\n* Redaction failed when a match ran past the page edge.\n* Markdown and CSV uploads were read as plain text; the stored neutral file name lost the extension.\n"
          "* Sentence counting ignored `e.g.`/`p.m.`; numbers like `3.14` counted as two words; a 16-digit test card was rejected; US-style phone numbers were missed.\n"
          "* The personal-data scan did not say when pages had no text layer.\n* The merger layout did not stack on narrow screens.\n\n"
          "## What was not tested\n\nSee Report G.\n")
    return s


# ------------------------------------------------------------------------------------------------------------------ G
def report_g(stamp: str) -> str:
    return header("Report G - Honest status and known limits", stamp, "What is done, what is verified, what is not.") + """\
## Verified

* The six workspaces work end to end in a real browser against a real server (Report F).
* Redaction removes content: tested by re-reading the saved file for text, bytes and metadata.
* Conversions and merges are re-opened and compared; the results state what was kept and what was not carried over.
* Forms: fill and flatten verified on a real AcroForm. OCR: verified with Tesseract on a rendered scan.
* The Privacy, Handler, Observability agents and the Reader were loaded through SLAI's `AgentFactory` and answered (Report B).

## Not verified, or limited

* **Language, Safety, Quality agents:** could not be loaded on the machine where the probes ran (missing `torch`; missing Quality memory file). Documaster's use of them is tested with stand-ins only. **Planning** is not used.
* **Privacy agent behaviour:** for text with personal data the real agent answers 'modify' with a tokenised payload; Documaster treats that as 'do not forward', so agent summaries are not available for such text.
* **Windows:** developed and tested on Linux. Paths and the launcher were written to be portable but were not run on Windows by this work. The target folder is `G:\\GAIA\\AI\\SLAI\\applications\\Documaster`.
* **Layout fidelity:** conversions rebuild documents from the structure model (or LibreOffice when chosen); they do not reproduce a PDF's layout. Reports list this per format.
* **Redaction cost:** redacted pages become images (text on those pages is no longer selectable; OCR can restore it).
* **PDF text extraction:** no table or image extraction from PDFs; scanned pages need OCR.
* **Personal-data detection** is pattern based with checksums; it does not find names or addresses and says so in the interface. The BSN check passes 1 in 11 random 9-digit numbers and is marked as weak.
* **Right-to-left text** is not shaped in generated PDFs; no tagged PDF / alt text.
* **Drag-and-drop** of page cards is implemented with pointer and keyboard paths; the browser test drives the keyboard path and the toolbar, not a native drag.
* **Drawing tools:** the browser tests drive highlight, redaction, rotate/undo, merging and the assistant. Underline, strike-out, rectangle, comment, ellipse, freehand, crop, text boxes, text at a point and image placement are covered by tests of the operations the client sends (the saved PDF is re-opened and inspected), but their on-page drawing gestures were not each driven in a browser.
* **Load and very large files:** limits exist (upload size, pages, text, job deadline); no performance benchmark was run.
* **No fuzzing**, no accessibility audit with a screen reader (keyboard use and labels were built in, not audited).
"""


# ------------------------------------------------------------------------------------------------------------------ H
def report_h(stamp: str) -> str:
    rows = [
        ["SLDC is authoritative, no competing schema", "`slai/docmodel/ir.py`, `slai/bridge/__init__.py`", "Report D; ConvertTests (sldc round trip)"],
        ["Six workspaces: Word Counter, Converter, Merger & Page Organizer, PDF Editor, Reader & Analysis, AI Assistant", "`src/client/ws/*.ts`", "tests_e2e"],
        ["Multi-agent integration, honest status", "`slai/bridge/agents.py`, `slai/analysis/agent_calls.py`", "Report B; test_agents.py; probes"],
        ["Real functionality only: no dummy features, no fake AI answers", "assistant is rule-based + extractive; agent output is labelled, absent agents are reported", "test_assistant.py (not-in-document test); AnswerHandlingTests"],
        ["Documents are untrusted", "upload sniffing, no execution, CSP, quote-never-obey", "Report E; test_security.py; test_assistant.py"],
        ["Redaction really removes content", "`slai/pdfops/redact.py`", "test_redaction.py"],
        ["Never auto-re-sign", "no sign operation; signature fields refused; merge notes", "Report D; test_pdf_ops.py; test_forms_ocr.py"],
        ["Preserve the merge function names", "controller methods in `src/client/ws/merger.ts`: `_add_merge_files`, `_build_merge_plan`, `_move_merge_page_up`, `_move_merge_page_down`, `_toggle_selected_merge_pages`, `_merge_selected_pdfs`, `_clear_merge_files`", "tests_e2e (merger flow)"],
        ["UI as the reference screenshots: dark #111827 / sidebar #1f2937 / gold accent; light #f3f4f6 / indigo #6366f1; left sidebar with the four main tools plus theme toggle; dashed drop zones; 'Convert to:'; Merge Files / Edit Files", "`src/client/styles.css`, `ws/converter.ts`, `ws/merger.ts`", "screenshots in tests_e2e/shots after a run"],
        ["TypeScript client consistent with the other SLAI applications", "strict `tsc`, ES modules, committed `dist/`, `tsx --test`, `tests_py`, stdlib Python service", "package.json; `npm test`"],
        ["Security and tests", "Report E, F", "test suites"],
        ["Reports", "this folder, regenerated by `tools/make_reports.py`", "-"]]
    return header("Report H - Requirements traceability", stamp, "Each requirement, where it is implemented, and the evidence.") + table(["Requirement", "Implementation", "Evidence"], rows) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slai-root")
    ap.add_argument("--run-tests", action="store_true")
    ap.add_argument("--out", default=str(APP / "docs"))
    a = ap.parse_args()
    if a.slai_root:
        os.environ["SLAI_ROOT"] = a.slai_root
    from support import Harness, ROOT
    if ROOT is None:
        print("The SLAI checkout was not found; pass --slai-root", file=sys.stderr)
        return 2
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    h = Harness(agents="none")                       # agents are never started by the report generator
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    try:
        files = {"report-A-capabilities.md": report_a(h, stamp), "report-B-agents.md": report_b(h, stamp), "report-C-formats.md": report_c(h, stamp),
                 "report-D-sldc.md": report_d(h, stamp), "report-E-security.md": report_e(h, stamp), "report-G-status.md": report_g(stamp),
                 "report-H-traceability.md": report_h(stamp)}
    finally:
        h.close()
    files["report-F-testing.md"] = report_f(stamp, a.run_tests, a.slai_root)
    for name, text in sorted(files.items()):
        (out / name).write_text(text, encoding="utf-8")
        print("wrote", out / name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
