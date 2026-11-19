# A. Application audit

Target: `The-Outsider-97/Documaster`, branch `main`, HEAD `7d41eed`, package version 2.4.0 (independent of SLAI's version). Audit date 2026-11-19.
Method: read the code, ran it, wrote probes that exercise it through the real HTTP server and the real browser. Findings are in D; numbers are in E.

## A1. What the application is
A local, single-user document workstation: a standard-library Python HTTP server (`slai/`, 16.8 k lines) plus a TypeScript browser client (`src/`, 4.8 k lines,
UI-free logic in `src/core`) compiled to a committed `dist/`. It processes documents on the user's machine and uses a SLAI checkout for the SLDC document
model, Reader engines and optional local agents. 45 HTTP routes, 7 workspaces (Word Counter, Converter, Merger & Page Organizer, PDF Editor, Reader & Analysis,
AI Assistant, System). Tests: 3.1 k lines of Python, 5.4 k lines of TypeScript/e2e/fixtures.

## A2. Architecture assessment
| Aspect | Assessment |
|---|---|
| Layering | Good. `core/` (pure TS, unit-tested) → `client/` (DOM) → HTTP → `api.py` handlers → services (`workspace`, `merger`, `counter`, `docmodel`, `pdfops`, `analysis`, `assistant`). Handlers are thin and testable (`router.py` is separate from the server). |
| Document model | All non-PDF conversion goes read → SLDC (authoritative, owned by SLAI) → write → **verify** (every output is re-opened by an independent parser and its text compared; a failing output is discarded, not delivered). A strong design. |
| Engines | Two, honestly labelled: Documaster model engine (structure-preserving, checked) and LibreOffice (layout-preserving). The UI greys out routes that do not exist and says why. |
| Concurrency | Jobs with progress, cancel, timeout (`jobs.py`); PDFium is not thread-safe and every PDFium user is wrapped in `pdfium_lock`. |
| Coupling | **Hard runtime dependency on a SLAI checkout** (O-001) — the main architectural risk. |
| Repo hygiene (before) | Generic Python `.gitignore` that ignored the committed `dist/`; CI was a Webpack template for a project without Webpack; mixed CRLF/LF; "Add files via upload" commits that silently reverted code (D-002). |
| Dead code / dependencies | `requirements.txt` lists 7 packages, all used; the only optional one (`regex`) is guarded. No unused runtime dependency found. A static dead-code sweep was **not** performed. |

## A3. Feature inventory (verified unless marked)
- **Word counter** — words/chars/sentences/paragraphs, reading/speaking time, language guess, top terms, goals, per-page breakdown, batch totals. Rules are written down in `textstats.py` and now pinned by fixtures.
- **Converter** — see C. 11 targets, 16 source formats (3 added here), mixed batches with per-file target, ZIP or individual download, name collisions never overwrite (`name (1).pdf`), cancel/retry per job, partial success reported per file.
- **Merger / organizer** — PDF, images and documents of other types into one PDF or model format; reorder (pointer drag and move buttons), exclude, rotate, duplicate, blank pages; undo; PDF pages copied unchanged.
- **PDF editor** — highlights/underline/strikeout/notes/shapes/drawing; movable persistent text boxes and images; page-number/watermark stamps; form fill and flatten; true redaction (pages rasterised, saved file verified not to contain the word); metadata/bookmarks/passwords; compression; OCR (Tesseract); revision history with undo/redo. **It does not edit existing PDF text in place and the app says so** — correct, because the engine cannot do it reliably.
- **Reader** — outline, structure checks, keywords, readability, duplicates, evidence-linked extractive summary, comparison, checklists, PII listing.
- **AI assistant** — read-only tools run directly; edits are *proposals* that need confirmation; document text cannot create a proposal (tested); with no working language agent it says so and offers the same actions as buttons. **No fake AI was found:** nothing generates text unless a SLAI agent returns it, and agent text is labelled.

## A4. Security review (summary; details in D and E)
Strong: loopback-only, token, Host/Origin/CSRF/CSP, atomic writes, safe file names, DOCTYPE refusal in ODT, archive caps, no shell, list-argv subprocesses, OCR language validation, no zip extraction to disk (no zip-slip surface), output ZIP built only from registered outputs.
Weak (fixed here): unbounded member inflation during sniffing (D-004), orphaned LibreOffice workers on timeout (D-005), 500s on malformed input (D-006), Windows device file names (D-008).
Open: nothing tested on Windows (O-002).

## A5. UI/UX audit
Automated floor now passes in all 7 workspaces (names, labels, alt text, landmarks, language, title, keyboard reach); one real defect fixed (D-012). Pointer drag, undo of drag, theme switch and console cleanliness are covered by e2e. **Not done:** a manual visual/UX review and contrast measurement in context; a screen-reader pass; narrow-viewport checks.

## A6. Professional feature-gap analysis (against what a paid PDF/document tool offers)
| Capability | Documaster | Gap |
|---|---|---|
| Edit existing PDF text in place | no (stated) | Large. Needs a layout-aware PDF engine; do not promise it. |
| Digital signatures (add / validate) | never added | Medium. Validate-only would be a safe first step. |
| Tagged PDF / PDF/A / accessibility output | no | Medium |
| Right-to-left shaping in PDF output | no (stated) | Medium |
| OCR | PDFs only, Tesseract | Images have no OCR route (matrix) |
| Office formats | DOC/DOCX/ODT/RTF/HTML/MD/TXT/XML/CSV, XLSX/PPTX/ODP→PDF | XLS/PPT legacy, TIFF, SVG, CSV/XLSX as targets |
| Redaction | true, verified | — (a strength) |
| Compare documents | SLDC diff | PDF-to-PDF visual diff not offered |
| Batch | mixed formats, ZIP, retry | no folder watch / command line batch |
| Cloud/collaboration | deliberately none | by design (privacy) |
