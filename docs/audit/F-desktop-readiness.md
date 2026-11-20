# F. Desktop readiness

> **Update (2026-11-19): packaging was then authorised and started.** The assessment below is the original audit finding and is kept as written. What changed: blocker 1 (SLAI as a runtime dependency) is resolved by bundling a pruned, byte-identical copy of SLAI and the LANTRA model inside the installer; the free-port start-up gap is closed (`--port-fallback`, `--ready-line`, `--exit-on-stdin-close`); a Tauri 2 shell, a bundle builder, a smoke test and a Windows build script were added. Blocker 2 (Windows unverified) and the signing/installer/update items **remain open**: nothing has been compiled, built or run on Windows. See [docs/DESKTOP_APP.md](../DESKTOP_APP.md), especially "What was not built or run".

# Original assessment (before packaging was started)

## Verdict
**Not ready to package as a standalone desktop app yet.** The local web app itself is sound for a desktop shell, but one architectural dependency and one verification gap block packaging.

## What is already desktop-friendly (verified)
| Area | State | Evidence |
|---|---|---|
| Local-only server | binds loopback only (refuses `0.0.0.0` at start-up), per-launch random token, Host/Origin/CSRF checks, strict CSP | `test_security` (19), live run |
| Start-up | `python run_documaster.py` / `python -m slai`; `--check` (exit 1 if blocking), `--build`, `--dev`; banner; port-in-use message; stale-client warning; `slai:startup-*` loader events in the browser (shared loader, committed `dist/startup`) | `test_startup` (20), manual `--check` |
| Runtime needs Node? | **No.** `dist/` is committed and content-hash-checked; Node only to rebuild | `test_committed_build_matches_its_sources` |
| Data locations | `%LOCALAPPDATA%\SLAI\Documaster` (Windows) / `$XDG_DATA_HOME/slai/documaster`; outputs in `~/Documents/Documaster`; both settable (`--data-dir`, `--output-dir`, UI setting with a write probe) | `config.py` |
| Atomic, collision-free output | exclusive reservation + temp file + `os.replace` with a copy fallback for Windows virus-scanner/sync races; nothing overwritten | `files.py`, tests in `test_units` |
| Privacy | documents never leave the machine (`allow_remote_processing=False`, no remote path built in); agents are local; Privacy-agent gate and consent default | `test_agents`, `test_assistant` |
| Resource limits | upload 200 MB, 5000 PDF pages, 20 M chars, job timeout 900 s, subprocess timeout 120 s, archive size/entry caps (this audit adds per-part cap) | `config.py`, D-004 |
| Process hygiene | external programs run as a process tree and are killed as a tree on timeout (this audit) | `test_procs` (POSIX) |

## Blockers
1. **SLAI checkout is a hard runtime dependency (O-001).** `src.utils.sldc` (the SLDC model), the Reader engines and the agents are imported from a SLAI repository on `sys.path`. A desktop installer would have to either ship the whole checkout, vendor a pinned subset (SLDC + the engines Documaster calls), or depend on a published SLAI library. This is the single biggest decision for packaging. SLAI's shared `tools/build/freeze_launcher.py` exists but was not evaluated here (instructed not to package).
2. **Windows is unverified (O-002).** Code paths exist (`taskkill /T`, `LOCALAPPDATA`, `Program Files` discovery for LibreOffice/Tesseract, `replace_file` fallback, device-name filenames) but none was executed on Windows.
3. **No signing / installer / update channel / crash reporting**, by design not started.

## Non-blocking gaps to decide before a desktop build
- Port: the default is 8766, but `--port 0` works (verified by launching `run_documaster.py --port 0`: it printed `127.0.0.1:42791` and the tokenised landing URL answered 200), so a desktop shell can pick a free port and open the printed URL itself.
- External programs (LibreOffice, Tesseract, qpdf) are optional and discovered on PATH + common folders; a desktop bundle must decide whether to ship or detect them. `--check` already reports each with a fix.
- `webbrowser.open` is how the UI is shown; a native window needs the shell to own that.
- Python range: SLAI declares `>=3.10,<3.13`; Documaster ran on 3.13 here and `--check` warns about it (a warning, not a stop).
- Single-user, single-process, in-memory session registry with a 12 h TTL; fine for desktop.

## Recommended order
(1) decide the SLAI dependency strategy, (2) run `--check` and the full suites on Windows, (3) free-port start-up, (4) only then bundle (freeze + external-tool detection + installer).
