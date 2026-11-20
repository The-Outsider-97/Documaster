# D. Prioritised issue register

Severity: **P0** broken core workflow / data integrity / security hole · **P1** significant functional, robustness or security gap · **P2** quality, hygiene,
roadmap. "Pre-existing" = present at `main` 7d41eed (the audited HEAD) before any change here; "new" = introduced by this audit's own changes (none were found by the final suites).
Every fixed item has a test that **fails without the fix and passes with it** unless stated.

## Fixed in this audit

| ID | Sev | Origin | Finding | Evidence | Fix | Test |
|---|---|---|---|---|---|---|
| D-001 | P0 | pre-existing | **Every DOC and RTF read failed with HTTP 500** (`WD_PARAGRAPH_ALIGNMENT has no XML mapping for 'start'`): LibreOffice writes `w:jc="start"/"end"`, python-docx raises on them. 30 of 330 conversion pairs failed; any strict-mode DOCX would also fail. | `tools/probe_conversions.py` before/after | `_alignment()` in `read_docx.py` reads the raw `w:jc` and maps start/end → left/right | `test_office_input` (16 sub-tests failed before) |
| D-002 | P0 | pre-existing (upload commit) | `convert.py` had silently lost three features in an "Add files via upload" commit: image routes, same-format retention (`retain_same_format`, byte-exact copy + SHA-256), the `sniff` import. PNG/JPEG conversion and "retain" failed. | 40 failing Python tests on a fresh run → 7 after refreshing the SLAI mirror → 2 after restoring | restored from 25da986 | existing `test_batch_api`, `test_convert_merge` |
| D-003 | P0 | pre-existing | Merging a PDF with a DOCX failed: "SLDC 1.1 features require an explicit upgrade of package.version" | `test_convert_merge` | merged package takes the newest SLDC version of the units actually used (`newest_version`) | `test_convert_merge` 24/24 |
| D-004 | P1 | pre-existing | **Archive sniffing inflated whole members**: a 0.4 MB upload with a `mimetype` member that inflates to 400 MB used 816 MB RSS (4 GB would exhaust memory). | measured: 816 MB → 16 MB | bounded `_read_head()`; DOCX XML parts over 256 MiB refused before parsing | `ArchiveSniffTests`, `test_docx_with_an_oversized_xml_part…` |
| D-005 | P1 | pre-existing | **Timeout did not stop LibreOffice/Tesseract workers**: `subprocess.run(timeout=)` kills only the launcher; `soffice.bin` kept running (demonstrated). | `test_plain_subprocess_run_does_leave_the_worker` | `slai/procs.run_bounded`: own process group (POSIX) / `taskkill /T` (Windows); never via a shell | `test_procs` (POSIX). **Windows branch not executed here** |
| D-006 | P1 | pre-existing | 11 client-input mistakes returned **HTTP 500 + traceback** (`?w=abc`, `pages/abc/image`, `top`, `goals`, `margins_mm`, `splitHeadings`, revision number, non-string ids in `docs`) | fuzz run | `coerce_int`, `id_list`, typed option checks → 400 with a message | `test_input_validation` (7) |
| D-007 | P1 | pre-existing | HTML→DOCX and HTML→RTF through LibreOffice: "no export filter" | matrix | `--infilter="HTML (StarWriter)"` | `test_html_through_the_libreoffice_engine` |
| D-008 | P1 | pre-existing | Windows device names passed `safe_filename` unchanged (`con.txt.pdf`, `COM1.tar.gz`, `AUX .txt`, `COM¹`) — Windows treats these as devices | direct probe | check the part before the *first* dot; add superscript COM/LPT and CONIN$/CONOUT$ | `FileNameTests` (test_security) |
| D-009 | P1 | pre-existing | Word counter split words at soft hyphens (U+00AD), over-counting PDF-copied text (`exam­ple` = 2) | probe | ignored in `tokenize`; rule documented | `CountingFixtureTests` |
| D-010 | P1 | pre-existing | CI was a copy of the Node/Webpack template (`npx webpack`; there is no webpack) and a vector workflow that fails without SLAI (`test_document_reader_returns_editable_shape_carriers` had no skip guard) | workflow files; run without `SLAI_ROOT`: 1 error | real CI (typecheck, tsx, compile, Python 3.10/3.12/3.13), guard added | `python -m unittest discover` with `SLAI_ROOT` unset: OK, 133 of 247 skipped (skips are not counted as passes) |
| D-011 | P1 | pre-existing | **Failing test `test_all_supported_word_geometry_presets[line]`** (earlier finding): the test expected text on a straight line; both writers deliberately drop text for `line` (first commit e290786) and Word has no text body for a line. The *test* was wrong. | root-cause trace | assertion now states the policy: a reimported line has empty text | same test |
| D-012 | P2 | pre-existing | Unlabelled `<select>` ("Add an open document…") — screen readers announce nothing useful | `run_a11y.py` failed 7/7 before | `aria-label` | `run_a11y.py` |
| D-013 | P2 | pre-existing | `tests_e2e/run_batch_text.py` could never pass: `page.wait_for_function('…')` evaluates a string, which the app's own CSP (`script-src 'self'`) forbids. This is the real cause of the earlier "browser testing blocked" note — **the environment was not blocking; the script was.** | traceback | Playwright `expect(...).to_have_count` | the script now passes |
| D-014 | P2 | pre-existing | `.gitignore` (generic Python template) ignored `dist/`, which is committed on purpose; no `node_modules`/`tests_e2e/shots` rules | file | `dist/` un-ignored, rules added | – |

## Added capability (not a defect)

| ID | Description | Evidence |
|---|---|---|
| F-001 | XLSX / PPTX / ODP → PDF through LibreOffice, engine forced, other targets refused; client offers only PDF for them | `test_layout_only` (7), `run_layout_only.py` |
| F-002 | SPADRA / SLAI-Writer-style start-up: `run_documaster.py`, `--check`, `--build`, `--dev`, content-hash `dist/build.json` (Node and Python agree byte-for-byte on the real tree) | `test_startup` (20) |

## Open (not fixed; ordered)

| ID | Sev | Finding | Why open / recommendation |
|---|---|---|---|
| O-001 | P1 | **Runtime and tests depend on a full SLAI checkout** (`SLAI_ROOT`, `src.utils.sldc`, agents). 133 of 247 tests skip without it. | Architectural; blocks a standalone desktop build. Needs either a vendored/pinned SLDC package or a published SLAI library. |
| O-002 | P1 | **Nothing was executed on Windows** (path handling, `taskkill` branch, `replace_file` fallback, LibreOffice discovery in `C:\Program Files`). | No Windows host available. Run `python run_documaster.py --check` and the suites there before release. |
| O-003 | P1 | Real SLAI agents were not re-probed in this session (agents are exercised by `test_agents` against the mirror; `docs/agent_verification.json` is from an earlier session). | Re-run `tools/probe_agents.py` against a copy of SLAI on the target machine. |
| O-004 | P2 | Layout-only conversions are verified structurally (opens, page count ≥ 1) but their content is not compared to the source. | Acceptable for "render with LibreOffice"; could compare cell text via openpyxl. |
| O-005 | P2 | Legacy XLS/PPT, TIFF, SVG, CSV-as-target are unsupported. | See C3. TIFF is the cheapest next route (Pillow). |
| O-006 | P2 | Word counter's token rule differs from Word's whitespace rule for `TCP/IP`, `10:30`, `v2.0.1`, emoji. Documented, but a product decision. | Offer a "Word-compatible" mode if parity matters. |
| O-007 | P2 | Mixed CRLF/LF across the repo (upload commits rewrote endings); no `.gitattributes`. Hashes in `build.json` normalise CRLF so builds are stable, but diffs are noisy. | Add `.gitattributes` and run one dedicated renormalisation commit. |
| O-008 | P2 | `--slai-root PATH` that is not an SLAI checkout silently falls back to `SLAI_ROOT`/auto-discovery. | Fail loudly when an explicit path is wrong. |
| O-009 | P2 | Browser e2e (4 scripts) are not in CI (need Chromium + SLAI). | Add a job that checks out SLAI when available. |
| O-010 | P3 | First count of a DOCX showed a 6.4 s cold-start outlier (median 0.33 s). | Warm-up on first use; not investigated further. |
| O-011 | P3 | `regex` package is optional here and not installed in this environment (`pip install` has no network): 1 test skipped. Search with user regexes is refused without it (by design). | Install `regex` on the release machine and run the test. |

## Not a defect (checked)

- Server boundary: Host check, per-launch token (constant-time), CSRF marker, Origin check, CSP, `nosniff`, no CORS, static traversal, loopback-only bind — all covered by `test_security` (19 tests).
- Zip handling: ODT/DOCX declared sizes are enforced by `zipfile` on read; no extraction to disk, so no zip-slip. Output ZIP is built from registered outputs only; `../../x` → 404.
- Subprocess arguments are lists, never shells; OCR language is validated.
- The assistant cannot be steered by document text (e2e: an instruction inside a document creates no proposal; changes are proposals needing confirmation).
