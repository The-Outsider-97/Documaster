# Report F - Testing

_Generated 2026-10-22 23:57 by `tools/make_reports.py` from the running code._

Results of the last recorded run (2026-10-22 23:57). Re-run with `python3 tools/make_reports.py --run-tests`.

| Suite | What it covers | Result |
|---|---|---|
| Python (`tests_py`, stdlib `unittest`) | security, redaction, conversion, merging, PDF operations, forms, compression, OCR, assistant, agent handling, counting, personal-data patterns, files, jobs, request understanding; real HTTP server, real files | 166 tests, all passed (28.192 s) |
| TypeScript (`tests/*.test.ts`, `tsx --test`) | page organiser engine, API client with a mock fetch, formatting, geometry | 39 tests, 39 passed, 0 failed |
| Type check (`tsc --noEmit`, strict) | the whole client | clean |
| Browser (`tests_e2e/run_browser.py`, Chromium) | every workspace, both themes, merge, convert, editor (rotate, undo, highlight, redact + download), reader, assistant incl. hostile document, theme | 30 checks passed, 0 failed |

## What was found by testing and fixed

* A permission warning was shown on unprotected PDFs (panel built before a document was open).
* The assistant read file names like `pii.pdf` as requests, and converted *every* open document with the same name; it now resolves names to one document or asks.
* On some Windows set-ups the final file rename was refused ("cannot move the file to a different drive") and every upload failed; it now falls back to copy-and-remove (`files.replace_file`), and the flush after that copy is best effort because Windows refuses `fsync` on a read-only handle. My first fix missed the second error; the test now covers both.
* Redaction failed when a match ran past the page edge.
* Markdown and CSV uploads were read as plain text; the stored neutral file name lost the extension.
* Sentence counting ignored `e.g.`/`p.m.`; numbers like `3.14` counted as two words; a 16-digit test card was rejected; US-style phone numbers were missed.
* The personal-data scan did not say when pages had no text layer.
* The merger layout did not stack on narrow screens.

## What was not tested

See Report G.
