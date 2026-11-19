# H. Release verdict

## **Conditionally ready** — for local, single-user use on a machine that has a SLAI checkout. **Not production-ready** as a standalone, installable desktop product.

### What the evidence supports
- All executable checks pass after the fixes: Python 247/247 run (0 fail, 1 documented skip), TypeScript 40/40 plus type check, 47 browser checks (0 fail), and a 330-pair conversion matrix with 0 lost-content and 0 failed pairs (E).
- The security boundary of the local server is well built and tested; four real weaknesses were found and fixed (D-004, D-005, D-006, D-008).
- Conversions are verified before delivery, redaction is verified against the saved file, and the assistant cannot be steered by document text. No fake AI was found.
- The product states its limits honestly (no in-place PDF text editing, no signatures).

### Why it is not unconditionally ready
1. **At the start of this audit, core workflows were broken**: every DOC/RTF input failed, PDF+DOCX merge failed, image conversion and same-format retention had been reverted by a web upload, CI tested nothing real. These are fixed, but they show that the release process had no gate that would have caught them. *Condition:* run the CI added here (plus the e2e scripts with a SLAI checkout) on every merge, and stop using "Add files via upload" for code.
2. **Hard dependency on a SLAI checkout** (O-001) — fine for the SLAI workspace, a blocker for a standalone installer.
3. **Windows was not exercised** (O-002). The primary desktop target is untested; several fixes (process-tree kill, file names) have a Windows branch that did not run.
4. **Real-agent behaviour was not re-probed** (O-003), and OCR quality was not measured.
5. Unverified-by-human areas: manual UX/visual review, screen-reader pass, contrast in context.

### Conditions to move to "ready"
| # | Condition | Owner action |
|---|---|---|
| 1 | Run `python run_documaster.py --check`, the Python suite, `run_browser.py`, `run_batch_text.py`, `run_layout_only.py`, `run_a11y.py` on **Windows 11** with LibreOffice and Tesseract installed | any Windows machine |
| 2 | Re-run `tools/probe_agents.py` against the release SLAI | SLAI owner |
| 3 | Decide and implement the SLAI dependency strategy for distribution | architecture decision |
| 4 | Install `regex` and run the skipped test | release machine |
| 5 | Merge the branch through a reviewed pull request with CI green | maintainer |

### Residual risk (accepted for conditional release)
Legacy XLS/PPT, TIFF and SVG are unsupported (clear refusals, not failures); the layout-only routes are verified for structure, not content; Word-count parity differs on a few token shapes (documented).
