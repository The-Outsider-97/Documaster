# Documaster

Documents on your own computer: **count** words, **convert** between formats, **merge** files and arrange their pages, **edit** PDFs (including real redaction), **read and analyse** documents, and **ask an assistant** about them or tell it what to do. Part of SLAI v2.4.

| Layer | Language | What it does |
|---|---|---|
| Client | **TypeScript** (strict, ES modules, DOM) | Six workspaces, built with `textContent` only (no `innerHTML`), shipped pre-built in `dist/` |
| Service | **Python 3**, standard-library server (`slai/`) | Local HTTP API on `127.0.0.1`; documents are handled with pypdf, pypdfium2, reportlab, python-docx, LibreOffice and Tesseract where installed |
| **SLAI** | Python | **SLDC** (`src/utils/sldc.py`) is the document model - Documaster defines no competing schema. The Reader engines verify written files. SLAI agents are used where they help (they run on this computer, the Privacy agent gates every use, and you can turn them off per document or globally) and are always labelled |

Nothing leaves your computer. There is no remote service and no fake answer: a feature that needs something missing says so and is greyed out.

## Run it

Documaster lives in `<SLAI>/applications/Documaster`. With the pre-built `dist/`, from that folder, with the Python environment of the SLAI repo:

```powershell
cd G:\GAIA\AI\SLAI\applications\Documaster
& G:\GAIA\AI\SLAI\venv\Scripts\python.exe -m pip install -r requirements.txt   # once
& G:\GAIA\AI\SLAI\venv\Scripts\python.exe -m slai                               # starts the server and opens your browser
```

The address printed at start-up contains a private access token; open that link (it sets a session cookie). The SLAI checkout is found automatically, or via `--slai-root` / `$SLAI_ROOT`.

```bash
python3 -m slai --agents none          # never use SLAI agents (privacy / offline)
python3 -m slai --agents strict        # a missing agent is an error instead of a graceful fallback
python3 -m slai --ask-before-agents    # agents may read a document only after you allow it for that document
python3 -m slai --agent-telemetry      # send job timings/failures to the Observability and Handler agents (off by default)
python3 -m slai --output-dir D:\Documents\Out --port 8766 --no-browser
```

Results are written to the output folder (default: a `Documaster` folder under your documents); existing files are never overwritten.

To rebuild the client (needs Node.js and TypeScript: `npm i`):

```bash
npm run build        # tsc -p tsconfig.build.json && node tools/build-static.mjs  -> dist/
npm start            # build + start the server
```

## The workspaces

* **Word Counter** - type or drop files. Words, characters, sentences, paragraphs, reading time, language guess (labelled as a guess), top terms, length goals. Scanned PDFs can be read with OCR. The counting rules are shown.
* **Converter** - PDF, Word, OpenDocument, RTF, HTML, Markdown, text, XML, CSV (read) and SLDC to PDF, DOCX, ODT, RTF, HTML, Markdown, TXT, XML and SLDC. Each result is re-opened and compared with the source; the card lists what was kept and what was not carried over. Optionally LibreOffice does the conversion to keep page layout.
* **Merger & Page Organizer** - drop several files, see every page as a card, drag with a pointer (mouse/pen/touch) or use Alt+arrows to reorder, including multi-page selections; edge scrolling and an insertion marker clarify the move. Exclude, rotate, duplicate or insert blank pages, then **Merge Files**. PDF pages are copied unchanged; other formats merge through SLDC logical pages. **Edit Files** opens the PDF in the editor.
* **PDF Editor** - page strip and viewer; rotate, move, delete, duplicate, extract, split, insert, crop; highlight, underline, strike-out, comments, shapes, freehand; editable sticky-note comment text; watermark, page numbers, text, text boxes, images; fill and flatten forms; bookmarks and properties; password; compress; OCR; **redaction that removes the content** (pages with redactions become images, then the saved result is checked for leftover text and metadata; a failed check deletes it). Every change is a revision you can undo and redo. Documaster never signs or re-signs a document.
* **Reader & Analysis** - expandable outline, structure checks, keywords, document statistics, readability, repeated text, configurable extractive-summary length, required sections (proposal, report, contract, research paper, or your own list), personal data with page positions, document comparison, whole-word/case/regex search with page/part filters. Every finding points to its page. "Redact these in the PDF Editor" hands personal data to the editor.
* **AI Assistant** - ask in English or Dutch. Questions are answered from the document with the quoted passage and its page, or the assistant says it is not there. Anything that changes a file (convert, merge, rotate, delete pages, watermark, page numbers, password, compress, OCR, redact personal data) is a **proposal** you confirm: single use, valid 15 minutes, void if the document changed. Passwords are asked for in the confirmation box, never read from the chat. Instructions inside a document are quoted, never followed.

## SLAI agents - what is real

See `docs/report-B-agents.md`. In short: the Privacy agent gates what text may be passed on (it answers "modify" for text with personal data, so such text is not forwarded); Language, Safety and Quality are used when agents may read documents (the default; `--ask-before-agents` or the System page changes that) and only if they load; Observability and Handler get telemetry only when you switch it on; Planning is not used. **The agents are loaded in the background at start-up. Language, Safety and Quality could not be loaded on the machine where I probed them (missing `torch` / Quality memory file), so my own tests use stand-ins for them. Press "Test agents now" on the System page: it makes one real call to each agent and shows what came back (passed or failed, and why).** The interface shows, per answer, which agent contributed and which did not and why.

## Security

Loopback only; per-launch token; Host, Origin and request-marker checks; strict content-security-policy; uploads treated as hostile (identified from their bytes, never executed, size/page limits); passwords only in memory. Details and tests: `docs/report-E-security.md`.

## Verification

```bash
npm run typecheck        # strict TypeScript
npm test                 # page organiser engine, API client (mock fetch), formatting, geometry
npm run test:py          # real server and files: security, redaction, conversion, merging, PDF operations, forms, OCR, assistant, agents (stand-ins), counting, personal data, Reader search
python3 tests_e2e/run_browser.py   # Chromium interactions, including pointer drag (needs: pip install playwright && playwright install chromium)
python3 tools/make_reports.py --run-tests   # regenerates docs/report-A ... H from the running code
python3 tools/probe_agents.py --root <COPY of SLAI> privacy   # try one real agent (use a copy of the checkout)
```

The Python tests need the SLAI checkout (`SLAI_ROOT`); they skip, not fake, without it.

## Layout

```
slai/                 Python service: server, API, workspace, jobs, SLDC bridge, document model, PDF operations, analysis, assistant
  docmodel/           readers, writers, SLDC merge, verification
  pdfops/             edit operations, redaction, annotations, forms, stamps, page arrangement
  analysis/           structure checks, personal data, comparison, agent calls
  assistant/          request understanding and tools
src/client/           TypeScript client (ws/ = the six workspaces)
dist/                 built client (committed)
tests/  tests_py/  tests_e2e/    TypeScript, Python and browser tests
tools/                build-static.mjs, make_reports.py, probe_agents.py
docs/                 USER_GUIDE.md and reports A-H
```

## Reports

| | |
|---|---|
| A | `docs/report-A-capabilities.md` - what each workspace can do on this machine |
| B | `docs/report-B-agents.md` - agent matrix, with what was verified against the real agents |
| C | `docs/report-C-formats.md` - format matrix |
| D | `docs/report-D-sldc.md` - SLDC integration |
| E | `docs/report-E-security.md` - security |
| F | `docs/report-F-testing.md` - testing |
| G | `docs/report-G-status.md` - honest status and known limits |
| H | `docs/report-H-traceability.md` - requirements and where they are met |
