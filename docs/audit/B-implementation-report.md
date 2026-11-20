# B. Implementation report

Phases follow the assignment: investigate → assess → P0 → P1 → P2 → integrate → test → release assessment. Item ids refer to D.

## Phase 1–2 — Investigation and assessment
Cloned `main` (7d41eed) read-only; ran the whole suite against a refreshed copy of the SLAI checkout (never against the live SLAI repo); built the conversion probe
(`tools/probe_conversions.py`), the benchmark (`tools/benchmark.py`), an API fuzz, archive/subprocess probes and the browser scripts. The baseline was 40 failures against a stale SLAI mirror,
7 after refreshing it, 2 failing + 1 skipped after the first two fixes.

## Phase 3 — P0 (broken core workflows, regressions)
| Item | Change | Files |
|---|---|---|
| D-002 | restore the three features lost in the upload commit | `slai/docmodel/convert.py` |
| D-003 | merged package takes the newest SLDC version among the units used | `slai/docmodel/sldc_merge.py` |
| D-001 | tolerate every `w:jc` value (`start`/`end`) — DOC/RTF input works again | `slai/docmodel/read_docx.py` |

## Phase 4 — P1 (security, robustness, functional completeness)
| Item | Change | Files |
|---|---|---|
| D-004 | bounded member reads while sniffing; 256 MiB cap per XML part of a DOCX | `slai/formats.py`, `slai/workspace.py` |
| D-005 | `run_bounded`: whole-tree termination; used for LibreOffice and Tesseract | new `slai/procs.py`; `docmodel/readers.py`, `docmodel/ocr.py` |
| D-006 | shared validators → 4xx instead of 500 | `slai/router.py` (`coerce_int`, `id_list`, `Request.query_int/param_int`), `api.py`, `merger.py`, `docmodel/convert.py` |
| D-007 | HTML opens in Writer for DOCX/RTF export | `docmodel/readers.py`, `docmodel/convert.py` |
| D-008 | Windows device-name defence | `slai/files.py` |
| D-009 | soft hyphen never splits a word | `slai/textstats.py` |
| F-001 | XLSX/PPTX/ODP → PDF: formats, sniffing, upload gating, forced engine, route registry (`engineOnly`), client (`routeAvailable`, accept list) | `formats.py`, `workspace.py`, `capabilities.py`, `api.py`, `docmodel/convert.py`, `src/client/{components,types}.ts`, `ws/converter.ts` |
| F-002 | **Start-up (SPADRA / SLAI-Writer pattern):** `Finding`s for Python (against SLAI's declared range), 6 required + 2 optional packages (observed versions), LibreOffice/Tesseract/qpdf, SLAI checkout, writable working folder, web-client freshness by **content hash** (CRLF-normalised) and completeness; `--check` (exit 1 on a blocking problem), `--build`, `--dev`; `StartupError` with hint; port-conflict message; stale-client note in the banner; `run_documaster.py`; the Node build writes the same `dist/build.json` | new `slai/startup.py`, `run_documaster.py`, `tools/record-build.mjs`; `slai/__main__.py`, `tools/build-static.mjs` |

## Phase 5 — P2 (hygiene)
D-010 real CI (`.github/workflows/ci.yml`: typecheck, tsx, compile, Python 3.10/3.12/3.13) replacing the Webpack template; D-011 the `line` shape test states its policy; D-012 accessible name for the document picker; D-013 CSP-safe e2e waits; D-014 `.gitignore`.

## Phase 6 — Integration
- Frontend ↔ backend: the client now consumes `engineOnly` routes; the conversion registry the UI uses is the same object the probe verified.
- Engines: LibreOffice runs through one bounded runner; Tesseract likewise.
- SLAI: the merge fix uses SLAI's own `newest_version`; start-up reads SLAI's `requires-python`; the shared loader (`dist/startup`) is unchanged.

## What was deliberately not done
- Packaging / installer / signing (assignment: assessment only).
- Arbitrary editing of existing PDF text (the engine cannot; the app states that).
- Routes for XLS, PPT, TIFF, SVG (not verified, so not offered — C3).
- A repository-wide line-ending renormalisation (would bury this change in noise; O-007).
- Any push or force-push; nothing outside the local clone and the SLAI *copy* was modified.
