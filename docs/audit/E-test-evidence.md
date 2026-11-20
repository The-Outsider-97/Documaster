# E. Test evidence

All commands were run in this session from the repository root on 2026-11-19. Nothing below is copied from an earlier run; numbers are from the final runs after the last code change.
Environment: Linux x86-64 (2 vCPU Xeon 2.1 GHz, 8 GB), Python 3.13.16, Node/tsc/tsx global (no npm registry access, so `npm ci` could not run — see "Not run"),
LibreOffice 24.2.7.2, Tesseract 5.3.4, qpdf 11.9.0, pypdf 5.9.0, ReportLab 5.0.1, Pillow 12.3.0, SLAI checkout = copy of the current device repo (`SLAI_ROOT=/mnt/user-data/uploads/SLAI`).

## E1. Results

| Suite | Command | Result |
|---|---|---|
| Python (unit + HTTP integration) | `SLAI_ROOT=… python3 -m unittest discover -s tests_py` | **247 run, 0 failures, 0 errors, 1 skipped** (58 s) |
| Python without a SLAI checkout (CI condition) | `python3 -m unittest discover -s tests_py` | 247 run, OK, **133 skipped** (skips are reported as skips, never as passes) |
| TypeScript type check | `tsc -p tsconfig.json --noEmit` | exit 0 |
| TypeScript unit tests | `tsx --test tests/*.test.ts` | 40 tests, 40 pass, 0 fail |
| Client build | `tsc -p tsconfig.build.json && node tools/build-static.mjs` | OK; `dist/build.json` equals the Python-computed record (test `test_node_and_python_write_the_same_record…`) |
| Browser e2e: workspaces, converter, merger, editor, reader, assistant | `python3 tests_e2e/run_browser.py` | 33 PASS, 0 FAIL, exit 0 (includes "no console errors or warnings") |
| Browser e2e: batch + text boxes | `python3 tests_e2e/run_batch_text.py` | 2 PASS (mixed per-file batch + ZIP + single download; text placement, drag, saved-object drag, PDF export) |
| Browser e2e: layout-only files | `python3 tests_e2e/run_layout_only.py` | 3 PASS |
| Browser accessibility floor | `python3 tests_e2e/run_a11y.py` | 9 PASS (7 workspaces + Tab reaches a control + all 7 nav links keyboard-reachable) |
| Conversion matrix | `python3 tools/probe_conversions.py` | 330 pairs: 131 ok, 199 clean refusals, **0 lost-content, 0 failed** |
| Benchmarks | `python3 tools/benchmark.py --repeat 3` | table E3 |

Skipped test (the one): `test_refinements_unit.ReaderSearchTests.test_bounded_regex` — "optional regex dependency not installed". `pip install regex` fails here (no network). It was **not** run and is **not** counted as passed.

### Count history (same machine, same SLAI copy)

| Point | Python tests | Not passing |
|---|---|---|
| `main` 7d41eed against a *stale* SLAI mirror | – | 40 failures (environment: SLDC 1.1 features, missing `sldc_sheets`) |
| after refreshing the SLAI mirror from the device | 197 | 7 failing/erroring |
| after restoring `convert.py` and fixing the merge version | 197 | 2 failing + 1 skipped |
| after fixing the `line` shape test | 197 | 0 failing + 1 skipped |
| **final** | **247** (+50 new) | **0 failing + 1 skipped** |

The 50 new tests: start-up 20, `run_bounded` 5, layout-only 7, DOC/RTF/HTML-LO 3, input validation 7, counter fixtures 4, security 4 (docx part bomb, sniff bomb, 2 file-name groups).

### Tests that were shown to fail before their fix
D-001 (16 sub-tests failed), D-004 (2 failed), D-005 (baseline behaviour demonstrated), D-007 (2), D-006 (11 requests → 500), D-012 (7/7 workspaces), D-013 (script crashed). D-001, D-004 and D-007 were re-run against the pre-fix code by stashing the fix; D-006, D-008, D-009, D-012, D-013 were observed failing by direct probe or run before the change.

## E2. The three earlier findings, re-checked

1. **Word DrawingML `line` shape test** — root cause: wrong test expectation (D-011). Fixed with a stated policy; the full set of 10 presets now passes.
2. **Skipped Python test** — the optional `regex` package is absent in this environment; unchanged, documented, not passed (O-011).
3. **"Browser interaction testing blocked by the environment"** — **not true here.** Chromium + Playwright run fine; 47 browser checks executed. The one script that crashed (`run_batch_text.py`) was crashing on a string `wait_for_function`, which the app's own CSP forbids (D-013).

## E3. Performance baselines (`docs/audit/performance.json`)

2 vCPU / 8 GB Linux, 3 runs each, wall-clock seconds through the real HTTP server, `peak RSS` is the process high-water mark (server in-process).

| Case | median s | min | max | peak RSS MB |
|---|---|---|---|---|
| upload 10-page PDF | 0.003 | 0.003 | 0.056 | 70 |
| upload 300-page PDF (0.2 MB) | 0.031 | 0.025 | 0.035 | 75 |
| count words, 2M-word text (/api/count/text) | 4.454 | 4.349 | 4.586 | 240 |
| count words, 300-page PDF | 1.071 | 1.033 | 2.021 | 240 |
| count words, 2000-paragraph DOCX | 0.326 | 0.312 | 6.455 | 240 |
| convert 2000-paragraph DOCX -> PDF (model engine) | 2.202 | 2.165 | 2.439 | 240 |
| convert 2000-paragraph DOCX -> PDF (LibreOffice engine) | 8.358 | 8.35 | 8.51 | 240 |
| convert 300-page PDF -> TXT | 0.184 | 0.18 | 0.195 | 240 |
| merge 20 PDFs x 50 pages -> PDF (1000 pages) | 1.171 | 1.141 | 1.236 | 240 |
| page image (w=1200) of a 300-page PDF, page 150 | 0.084 | 0.078 | 0.092 | 240 |
| rotate 1 page of a 10-page PDF (op + new revision) | 0.006 | 0.005 | 0.011 | 240 |

Reading: the model engine converts a 2000-paragraph DOCX in 2.2 s and LibreOffice in 8.4 s; merging 1000 pages takes 1.2 s; counting scales at ≈ 0.45 M words/s. Page rendering for a viewer thumbnail is milliseconds. These are baselines from one machine class, not guarantees; no regression threshold is enforced in CI.

## E4. Not run (stated, not hidden)

- `npm ci` / `npm run build` end-to-end (no registry access): the same steps were run with the global `tsc`, `tsx` and `node tools/build-static.mjs`. `--build`'s own error path (missing `node_modules`) is tested; its success path through `npm` was not executed.
- Anything on **Windows or macOS**.
- Real-agent probes (`tools/probe_agents.py`) against SLAI — only the mirror-based `test_agents` ran.
- Load/soak tests beyond the benchmark cases; multi-user (the app is single-user by design).
- OCR accuracy (OCR plumbing is tested, not recognition quality).
- Manual visual review of every screen; only the automated a11y floor and e2e screenshots (`tests_e2e/shots`, git-ignored).
