# SLAI Documaster as a desktop app (Tauri 2, Windows)

Documaster is packaged as a Windows desktop app the same way SciCal is: a small Tauri 2 shell owns the window and starts the Python engine
as a child process. The difference from SciCal's *linked mode* is that Documaster's installer is **self-contained**. SLAI's source, the
agents, the language model, Python and every package travel inside it; the machine needs nothing installed.

> **Read "What was not built or run" first.** This was written in a Linux container without access to crates.io, PyPI, npm or python.org,
> so Tauri could not be compiled, no Windows build or installer was produced, and the agents and the language model were never loaded
> from a bundle. What *was* run is listed there too.

## 1. Architecture

```
SLAI Documaster.exe  (Tauri 2 shell, Rust, WebView2)
 |  shows splash/index.html while the engine starts
 |  spawns:  resources/engine/python/python.exe  resources/engine/app/run_documaster.py
 |              --no-browser --ready-line --exit-on-stdin-close --port-fallback --port 47870
 |  reads:   DOCUMASTER_READY {"url":"http://127.0.0.1:47870/?token=...","port":...,"version":"...","bundled":true}
 |  then:    window.location.replace(url)         <- the web client, served by the engine, inside the window
 |  quit:    closes the engine's stdin            <- the engine stops itself; after 4 s the shell kills the process tree
```

* **Launch protocol** (`slai/engine.py`, `src-tauri/src/protocol.rs`). One READY line (it carries the per-launch access token and is never
  written to the log) or one `DOCUMASTER_ERROR <text>` line. Closing the engine's stdin means "quit", so nothing is left running if the
  shell crashes. `--port-fallback` takes any free port when 47870 is busy. On Windows the server binds with `SO_EXCLUSIVEADDRUSE`, because
  the default socket options there let a second process share the port.
* **Shell** (`src-tauri/`). `desktop.rs` starts, watches and stops the engine and writes `%LOCALAPPDATA%\SLAI\Documaster\engine.log`
  (previous run kept as `engine.log.1`); `protocol.rs` is std-only logic (line parsing, loopback-URL validation, argument list, quoting)
  that is unit-tested without Tauri. If the engine dies while the window is open, a message covers the window instead of leaving a page of
  connection errors. The window has no Tauri permissions (`capabilities/default.json` is empty): the page the engine serves cannot call
  into the shell, and the splash has a strict CSP.
* **Bundled mode** (`slai/engine.py`). The engine detects `bundle.json` next to its `app/` folder. In that mode the SLAI root and web client
  come from the bundle (an `SLAI_ROOT` left in the user's environment is ignored), helper programs are looked up in `helpers/` before
  `PATH`, and the offline settings below are forced.

## 2. What is inside the installer

```
engine/
  bundle.json            written last; its absence means "the build did not finish"
  python/                python.org embeddable CPython 3.13 (python313._pth points at ../site-packages and ../app; no `site`)
  site-packages/         pinned wheels (tools/bundle-requirements*.txt)
  app/                   Documaster: slai/, run_documaster.py, byte-compiled
  client/                the built web client (dist/)
  slai-root/             the part of SLAI Documaster imports, plus the model, with slai_bundle.json (a SHA-256 for every file)
  helpers/               optional: libreoffice/, tesseract/, qpdf/ (not included unless you pass --helper)
```

### 2.1 SLAI: which parts, and how that was decided
`tools/stage_slai.py` does a static import analysis (AST, no code is executed) from the modules Documaster loads: `src.utils.sldc`,
`sldc_sheets`, the four Reader engines, `agent_factory`, `shared_memory`, the eight role agents and `lantra_runtime`. It follows plain,
relative, lazy (inside a function) and `importlib.import_module("literal")` imports, skips `if TYPE_CHECKING:` blocks, and copies what it
reaches plus the data those modules read: `configs/` folders, small sibling `*.yaml`/`*.json`, the language resources/library/templates, and the
tokenizer files `data/bpe_200d_50k_*.json`. It never carries tests, virtual environments, caches, raw datasets, other checkpoints or
`applications/`. Every copied file is listed with its SHA-256 in `slai_bundle.json`; `--verify` re-checks a staged tree and
`python tools/stage_slai.py --slai-root <SLAI> --dry-run` prints what would be carried and the largest files.

SLAI is therefore bundled **as a pruned copy of its own source, unmodified** (byte-identical; the test suite checks this), not as a
frozen library and not as a link to a checkout. Updating SLAI means rebuilding the installer.

### 2.2 Which agents and which model "the app needs to operate optimally"
* Agents: Privacy, Safety, Language, Quality (the four Documaster calls), the Reader engines (parser, detection, integrity, compression),
  and the rest of the eight role agents because `agent_factory` imports them. Planning/Observability/Handler are carried for the same reason.
* Model: the checkpoint named by `src/agents/language/configs/language_config.yaml` (`lantra_runtime.checkpoint_path`): **LANTRA
  `lantra_chosen.pt`, 322 MiB**. It is the only model the Language agent loads. `--models none` builds without it (the Language agent then
  runs without its model); the builder records `modelsRequired` and `--verify` fails if a bundle that needs it lacks it.
* Offline guarantee (bundled mode, set unconditionally): `SLAI_MODEL_SOURCE=local`, `SLAI_MODEL_OFFLINE=1`, `HF_HUB_OFFLINE=1`,
  `TRANSFORMERS_OFFLINE=1`, `HF_HUB_DISABLE_TELEMETRY=1`, `DO_NOT_TRACK=1`. Documents and model requests never leave the machine; the
  Hugging Face packages are not in the bundle at all.

### 2.3 Why torch is in the bundle
`agents_config.yaml` says most agents are `torch_required: false`, but the static chain analysis (`stage_slai.py --why torch`) shows that
seven of the eight role-agent modules import torch, torchvision, librosa, einops or `rotary_embedding_torch` unconditionally at module level,
so the agents cannot even be imported without them. That is why the runtime is a full Python environment (pinned to the versions of the
SLAI virtual environment) and not a slim one. A CPU wheel is not forced: the pins are the versions SLAI itself runs with, and the build
installs what PyPI offers for `win_amd64`. If you want a smaller installer, the place to cut is the torch wheel (a CPU-only build), which needs
the pin changed and the agents re-tested; that was not attempted.

### 2.4 Python version
SLAI's `pyproject.toml` declares `>=3.10,<3.13`; the bundled Python is 3.13 because that is what Documaster was developed and tested on and the pins follow SLAI's environment versions. Documaster's `--check` reports this as a warning only. If you want to match the declaration, change `tools/bundle.lock.json` (URL,
`pip_python_version`, `pip_abi`) and the pins together.

## 3. Building it (Windows 10/11 x64)

Needs: Node.js 20+, Python 3 (only to run the build scripts), Rust (stable, MSVC toolchain), internet for the first run, and enough free disk
(a few GB: wheels, the unpacked bundle, the compiled app and the installer).

```
build-windows.cmd --trust-first-download
```

That runs, and stops at the first failure: `npm ci` + `npm run build` -> the builder's own tests -> `tools/build_bundle.py` (download
python.org's embeddable Python, `pip install --target` of the pinned wheels with `--only-binary=:all: --platform win_amd64`, stage SLAI, copy
the app and client, byte-compile, write `bundle.json`) -> `tools/smoke_engine.py --bundle src-tauri/engine --agents-test` (starts the bundled
Python exactly as the shell does, checks READY, the token cookie, `/api/health`, 401 without a token, one real call to each of the four
agents, and a clean exit when stdin closes) -> `tauri build --bundles nsis`. The installer lands in `src-tauri\target\release\bundle\nsis`.

* `SLAI_ROOT` (or `--slai-root`) names the SLAI checkout; by default the builder looks two folders above the app, where Documaster lives
  inside SLAI (`SLAI/applications/Documaster`).
* `--helper qpdf=C:\tools\qpdf` (repeatable: `libreoffice`, `tesseract`, `qpdf`) copies a program folder into `helpers/`; the engine finds it before
  `PATH`. Tesseract's `tessdata` is picked up automatically. None is included by default (see 5).
* First run only: the python.org zip has no recorded hash yet (`tools/bundle.lock.json`, `"sha256": ""`). `--trust-first-download` accepts it,
  prints its SHA-256 (compare it with the digest on python.org) and writes it into the lock file; commit that file so every later build is
  checked. This is trust-on-first-use; the hash was not looked up from here.
* Development without an installer: `npm run app:dev` (Tauri dev window over `python -m slai` from the checkout, with
  `DOCUMASTER_DEV_PYTHON` to choose the interpreter), and `python tools/smoke_engine.py --dev`.
* Other npm scripts: `app:bundle`, `app:smoke`, `app:build:windows`. `@tauri-apps/cli` is fetched by `npx` and deliberately not added to
  `devDependencies`, so `npm ci` and the existing lockfile are unchanged.

## 4. Size (estimates, nothing here was measured on a bundle)

| Part | Estimate |
|---|---|
| LANTRA checkpoint | 322 MiB (measured) |
| SLAI pruned source and data | up to ~44 MiB (the staged tree measured 366 MiB *including* the checkpoint) |
| torch wheel + libs | ~544 MiB (measured on the SLAI virtual environment) |
| other packages (torchvision, scipy, scikit-learn, librosa, matplotlib, ...) | a few hundred MiB |
| Python + Documaster + client | ~30-60 MiB |
| **Unpacked** | **about 1.1-1.4 GB** |

NSIS compresses to a smaller installer and has a ~2 GB limit, which this stays under. Expect a first start after install to be slow while
Windows Defender scans a gigabyte of new files (the shell waits up to 150 s for READY; the agents load afterwards in the background). Measure
with `python tools/build_bundle.py ... ` (it prints sizes per part and records them in `bundle.json`).

## 5. Decisions and honest limits

* **NSIS, per-user only.** No MSI: a per-machine install under Program Files is read-only for the user, and SLAI/agents write beside their
  own files (logs, caches). Per-user install (a folder under `%LOCALAPPDATA%`) keeps the install folder writable. The engine also redirects
  matplotlib/numba/joblib/torch caches to `%LOCALAPPDATA%\SLAI\Documaster\cache` and sets `PYTHONDONTWRITEBYTECODE`; whether *SLAI itself* writes
  into its root during a real run is **not verified** (`--check` reports whether the install folder is writable).
* **Helpers are not bundled by default.** LibreOffice (large, MPL/LGPL), Tesseract and qpdf are optional in Documaster; with none of them it
  still reads, edits and converts what its own code handles, and `--check`/the System page say what each missing one limits. Ship them with `--helper`
  after checking their licences for redistribution.
* **Licences.** The installer redistributes Python (PSF), the wheels (mostly BSD/MIT/Apache, torch BSD-3), SLAI and the LANTRA checkpoint. Confirm
  that you may redistribute the model checkpoint and any weights it was derived from before publishing the installer.
* **Pins.** Documaster's tested versions and SLAI's environment versions are pinned together; `pypdf` is the one that differs (Documaster's
  pin vs the 7.x in SLAI's venv) and whether SLAI's reader engines care is **not verified**; the Reader engines' status on the System page is where it would show (the smoke test itself checks the four agents, not the Reader).
* **Drag and drop.** The window sets `dragDropEnabled: false`, which Tauri on Windows needs for the page's own HTML5 drop zones; not verified
  in a real window. Downloads go through WebView2's normal download handling (not verified); Documaster also saves outputs to its output
  folder.
* **One instance.** Nothing stops a second copy from starting; the second takes another port (`--port-fallback`) and its own token. The data folder is shared.
* **No signing, updater or crash reporting.** The installer is unsigned: Windows SmartScreen will warn. `webviewInstallMode:
  downloadBootstrapper` needs internet on a machine without WebView2 (Windows 11 has it).
* **SLAI config note.** `agents_config.yaml` ships an `operations_notification` block with placeholder values; it is carried as-is and nothing
  in Documaster uses it, but you may want to review it before shipping.
* **Slim checkpoint idea.** The checkpoint may hold training state (unchecked) (optimizer, history) that inference does not need; stripping it could cut
  the size. Not tried: it would change a model file, so it needs your decision and a test.

## 6. What was not built or run

Not done, and not claimed:

* **Tauri was never compiled** (no crates.io access here): `desktop.rs`, `lib.rs`, `main.rs`, `tauri.conf.json` are unbuilt. `desktop.rs` was type-checked
  and run end to end against a small hand-written stand-in for the handful of Tauri calls it uses, not against Tauri.
* **No Windows build, no installer, no signing, no install/uninstall test**, no WebView2 run.
* **`tools/build_bundle.py` has never downloaded or installed anything**: it was run with a fake `pip` and a fake embeddable-Python zip.
  The python.org URL, the `python313._pth` handling on the real zip, and `pip install --platform win_amd64 ...` resolving every pin are unverified.
* **Real agents and the LANTRA model were not loaded from a bundle.** The container has no GPU or torch install of the SLAI environment and only a
  partial copy of SLAI; `smoke_engine.py --agents-test` is the check that does this, and it must be run on the Windows machine.
* Bundle size is estimated, not measured.

What *was* run (this container, Python 3.13): the engine protocol and bundled mode with real processes (port fallback, error line, stdin-close
exit, damaged bundle, offline environment, health), the Rust `protocol.rs` unit tests, a cross-language test that runs the Rust parser against the
Python protocol output, the SLAI staging analysis against the real SLAI source (closure, `--why torch`, data rules) and against a generated tree
(tamper detection), the builder against a fake pip and zip, plus the full existing Documaster suites (Python 312 tests, 2 skipped; TypeScript 40;
the four browser e2e scripts) with no regressions.

CI (`.github/workflows/ci.yml`, job `desktop-shell`) runs `cargo test` for the shell on a Windows runner: the first real compile of the Tauri code. It has not run yet; no `Cargo.lock` is committed because none could be generated here, so commit the one it produces.

## 7. Files added for the desktop app

`slai/engine.py`; changes in `slai/__main__.py`, `slai/bridge/__init__.py`, `slai/server.py`, `slai/startup.py`, `slai/api.py`;
`src-tauri/**`; `tools/stage_slai.py`, `tools/build_bundle.py`, `tools/smoke_engine.py`, `tools/bundle-requirements*.txt`, `tools/bundle.lock.json`;
`build-windows.cmd`; `tests_py/test_engine.py`, `test_shell_protocol.py` (+ `fixtures/protocol_cli.rs`), `test_bundle.py`.
