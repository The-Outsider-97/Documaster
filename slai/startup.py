"""Starting Documaster: the environment check behind ``--check``, the client build behind ``--build`` / ``--dev``, and the comparison of a
built client with the sources it was built from. It follows the start-up pattern of SPADRA and SLAI Writer.

What this module never does is change the interpreter. Documaster runs on the Python that started it. If that Python is too old, or a
package is missing, the check says so and names the command that fixes it; it does not look for another Python, create a virtual
environment or install anything.

Every finding reports what was *observed* (a version read from the installed distribution, a program found on the search path), not what
``requirements.txt`` says should be there.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Sequence

from .bridge import find_binary
from .engine import Bundle
from .errors import DocumasterError

HERE = Path(__file__).resolve().parent.parent          # the Documaster folder (the one with package.json)
Status = Literal["ok", "warn", "error"]
#: True while running from the installed desktop app (set by ``__main__``); only changes the wording of fixes, which then say "reinstall".
BUNDLED = False
MIN_PYTHON = (3, 10)
BUILD_RECORD = "build.json"
Requirement = tuple[str, str, str, str]                  # (distribution, import name, minimum version, what it is needed for)

#: Without these Documaster cannot do its core work, so start-up stops.
REQUIRED: tuple[Requirement, ...] = (
    ("pypdf", "pypdf", "5.9", "reading, editing, merging and encrypting PDFs"),
    ("pypdfium2", "pypdfium2", "5", "rendering pages for the viewer, thumbnails and redaction"),
    ("reportlab", "reportlab", "4", "writing PDFs, stamps and redaction output"),
    ("python-docx", "docx", "1.1", "reading and writing Word files"),
    ("Pillow", "PIL", "10", "images"),
    ("cryptography", "cryptography", "42", "AES-256 for PDF passwords and encrypted SLDC"),
)
#: Without these Documaster still starts; only the named feature is unavailable (and reports that when used).
OPTIONAL: tuple[Requirement, ...] = (
    ("regex", "regex", "2024.9", "bounded-time matching of user-supplied Reader search patterns (without it such patterns are refused)"),
    ("PyYAML", "yaml", "6.0", "SLAI's configuration repository, used by the SLAI agents"),
)
#: Optional programs: (name, programs to look for, what they enable).
PROGRAMS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("LibreOffice", ("soffice", "libreoffice"), "layout-preserving Office conversion and DOC / RTF / ODT / PPT reading"),
    ("Tesseract OCR", ("tesseract",), "OCR of scanned PDFs"),
    ("qpdf", ("qpdf",), "extra PDF structure checking"),
)
_EXTRA_DIRS = ("C:/Program Files/LibreOffice/program", "C:/Program Files (x86)/LibreOffice/program",
               "C:/Program Files/Tesseract-OCR", "C:/Program Files (x86)/Tesseract-OCR",
               "/Applications/LibreOffice.app/Contents/MacOS", "/usr/lib/libreoffice/program")


class StartupError(DocumasterError):
    """Documaster cannot start; ``hint`` says what to do about it."""
    code, status = "startup_failed", 500

    def __init__(self, message: str, *, hint: str | None = None, details: dict | None = None) -> None:
        super().__init__(message, details=details)
        self.hint = hint


@dataclass(frozen=True)
class Finding:
    name: str
    status: Status
    detail: str
    fix: str = ""


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", text)[:3])


def declared_python(root: Path | None) -> tuple[tuple[int, ...], tuple[int, ...]] | None:
    """``(minimum, exclusive maximum)`` from ``requires-python`` in the SLAI checkout's ``pyproject.toml``, when it states both."""
    if root is None or not (root / "pyproject.toml").is_file():
        return None
    try:
        text = (root / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return None
    found = re.search(r'requires-python\s*=\s*"([^"]+)"', text)
    if not found:
        return None
    low, high = re.search(r">=\s*([\d.]+)", found.group(1)), re.search(r"<\s*([\d.]+)", found.group(1))
    return (_version_tuple(low.group(1)), _version_tuple(high.group(1))) if low and high else None


def check_python(root: Path | None, version: tuple[int, ...] | None = None) -> Finding:
    v = version or tuple(sys.version_info[:3])
    here = ".".join(map(str, v))
    need = ".".join(map(str, MIN_PYTHON))
    if v[:2] < MIN_PYTHON:
        return Finding("Python", "error", f"{here} ({sys.executable}) is older than the {need} Documaster needs",
                       f"Start Documaster with Python {need} or newer (the one `pip install -r requirements.txt` was run with).")
    declared = declared_python(root)
    if declared is None:
        return Finding("Python", "ok", f"{here} ({sys.executable})")
    low, high = declared
    span = f">={'.'.join(map(str, low))},<{'.'.join(map(str, high))}"
    if v[: len(low)] < low:
        return Finding("Python", "warn", f"{here} is older than SLAI's declared {span}; SLAI's own packages were not qualified on it",
                       "Use the SLAI environment's Python if the agents misbehave.")
    if v[: len(high)] >= high:
        return Finding("Python", "warn", f"{here} is newer than SLAI's declared {span}; SLAI's own packages were not qualified on it",
                       "Use the SLAI environment's Python if the agents misbehave.")
    return Finding("Python", "ok", f"{here} ({sys.executable}), inside SLAI's {span}")


def _installed(distribution: str) -> str | None:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None


def check_dependency(distribution: str, module: str, minimum: str, why: str, *, required: bool,
                     installed: Callable[[str], str | None] = _installed,
                     importable: Callable[[str], bool] | None = None) -> Finding:
    can_import = importable or (lambda name: importlib.util.find_spec(name) is not None)
    version = installed(distribution)
    install = f"{Path(sys.executable).name} -m pip install -r {HERE / 'requirements.txt'}"
    bad: Status = "error" if required else "warn"
    if version is None or not can_import(module):
        if BUNDLED:
            return Finding(distribution, bad, f"missing from this installation; needed for {why}",
                           "Reinstall SLAI Documaster: the installed copy is incomplete.")
        return Finding(distribution, bad, f"not installed; needed for {why}",
                       install if required else f"{install}   (without it Documaster still starts)")
    if _version_tuple(version) < _version_tuple(minimum):
        return Finding(distribution, bad, f"{version} is older than the {minimum} Documaster was tested from; needed for {why}",
                       "Reinstall SLAI Documaster." if BUNDLED else f"{Path(sys.executable).name} -m pip install --upgrade \"{distribution}>={minimum}\"")
    return Finding(distribution, "ok", version)


def check_program(name: str, candidates: Sequence[str], why: str, *,
                  find: Callable[..., str | None] = find_binary) -> Finding:
    path = find(*candidates, extra_dirs=_EXTRA_DIRS)
    if path:
        return Finding(name, "ok", path)
    return Finding(name, "warn", f"not found; needed for {why}", f"Install {name} (Documaster still starts; the feature reports that it is unavailable).")


def check_slai(root: Path | None) -> Finding:
    if root is None:
        return Finding("SLAI checkout", "error", "not found, so SLDC, the Reader engines and the SLAI agents are unavailable",
                       "Set SLAI_ROOT to the SLAI folder, or pass --slai-root.")
    return Finding("SLAI checkout", "ok", str(root))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_bundle(bundle: Bundle, *, deep: bool = False) -> Finding:
    """The installed copy is complete: every file listed in SLAI's ``slai_bundle.json`` exists with the recorded size (names and sizes
    only - hashing the 330 MB model on every start would cost seconds; ``python -m slai --check --deep`` hashes everything)."""
    path = bundle.slai_root / "slai_bundle.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        entries = manifest["entries"]
    except (OSError, ValueError, KeyError) as exc:
        return Finding("Installation", "error", f"{path} cannot be read ({type(exc).__name__})", "Reinstall SLAI Documaster.")
    bad: list[str] = []
    for item in entries:
        target = bundle.slai_root / item["path"]
        try:
            if target.stat().st_size != item["bytes"]:
                bad.append(f"{item['path']} (size differs)")
            elif deep and item.get("sha256") and _sha256(target) != item["sha256"]:
                bad.append(f"{item['path']} (content differs)")
        except OSError:
            bad.append(f"{item['path']} (missing)")
    if bad:
        shown = "; ".join(bad[:3]) + (f"; and {len(bad) - 3} more" if len(bad) > 3 else "")
        return Finding("Installation", "error", f"{len(bad)} of {len(entries)} SLAI file(s) are missing or changed: {shown}",
                       "Reinstall SLAI Documaster.")
    models = [m["path"].rsplit("/", 1)[-1] for m in manifest.get("models", [])]
    return Finding("Installation", "ok", f"{len(entries)} SLAI files" + (f", model {', '.join(models)}" if models else ", no model"))


def check_slai_writable(root: Path) -> Finding:
    """SLAI's agents write logs and caches relative to the SLAI root. If the install folder is read-only they degrade; say so."""
    probe = root / "logs" / ".documaster-write-check"
    try:
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as exc:
        return Finding("SLAI folder writable", "warn", f"{root} is read-only ({exc.strerror or exc}); SLAI agents cannot write their logs there",
                       "Install for the current user only (the default), not into Program Files.")
    return Finding("SLAI folder writable", "ok", str(root))


def check_data_dir(path: Path) -> Finding:
    """Whether the working folder can be created and written to (checked by really writing a file)."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".documaster-write-check"
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as exc:
        return Finding("Working folder", "error", f"{path} is not writable: {exc.strerror or exc}", "Pass --data-dir with a folder you can write to.")
    return Finding("Working folder", "ok", str(path))


# ------------------------------------------------------------------------------------------------------ build record
#: Folders and files the client build reads (relative to the Documaster folder).
INPUT_DIRS = ("src", "typings", "assets")
INPUT_FILES = ("tsconfig.json", "tsconfig.build.json")


def _input_files(app_dir: Path) -> list[Path]:
    files: list[Path] = []
    for name in INPUT_DIRS:
        place = app_dir / name
        if place.is_dir():
            files += [f for f in place.rglob("*") if f.is_file() and "node_modules" not in f.parts]
    files += [app_dir / n for n in INPUT_FILES if (app_dir / n).is_file()]
    return sorted(files, key=lambda f: f.relative_to(app_dir).as_posix())


def file_digest(path: Path) -> str:
    """SHA-256 of the file's bytes, with line endings normalised so a checkout with CRLF (Windows, autocrlf) hashes like one with LF."""
    data = path.read_bytes()
    if b"\0" not in data[:8192]:
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def client_inputs(app_dir: Path = HERE) -> dict[str, str]:
    """Path (relative to the Documaster folder, with ``/``) -> digest, for every file the build reads. ``tools/record-build.mjs`` writes the
    same map to ``dist/build.json``; the two are compared by content, so file times cannot make a fresh build look stale."""
    return {f.relative_to(app_dir).as_posix(): file_digest(f) for f in _input_files(app_dir)}


def client_changes(app_dir: Path, client_dir: Path) -> list[str] | None:
    """Source files whose content differs from what the build was made from (changed, added or removed); ``None`` when the build has no record."""
    try:
        built = json.loads((client_dir / BUILD_RECORD).read_text(encoding="utf-8"))["files"]
        if not isinstance(built, dict):
            return None
    except (OSError, ValueError, KeyError):
        return None
    now = client_inputs(app_dir)
    return sorted({k for k in now if built.get(k) != now[k]} | {k for k in built if k not in now})


def client_missing_outputs(app_dir: Path, client_dir: Path) -> list[str]:
    """Compiled modules that should exist in ``dist/js`` for the sources there are (a build that stopped half way, or deleted output)."""
    src = app_dir / "src"
    out = []
    if src.is_dir():
        for ts in src.rglob("*.ts"):
            if ts.name.endswith(".d.ts"):
                continue
            js = client_dir / "js" / ts.relative_to(src).with_suffix(".js")
            if not js.is_file():
                out.append(js.relative_to(client_dir).as_posix())
    return sorted(out)


def check_client(app_dir: Path, client_dir: Path) -> Finding:
    if not (client_dir / "index.html").is_file() or not (client_dir / "js").is_dir():
        return Finding("Web client", "error", f"not built ({client_dir / 'index.html'} is missing)",
                       f"In {app_dir}: npm install, then npm run build (or start with --dev).")
    if not (client_dir / "startup" / "loader.js").is_file():
        return Finding("Web client", "error", "the start-up loader (startup/loader.js) is missing from the build",
                       f"Set SLAI_ROOT, then run npm run build in {app_dir}.")
    if not (app_dir / "src").is_dir():
        return Finding("Web client", "ok", str(client_dir))            # a distribution without sources: nothing to compare with
    missing = client_missing_outputs(app_dir, client_dir)
    if missing:
        return Finding("Web client", "error", f"the build is incomplete: {len(missing)} compiled file(s) are missing, for example {missing[0]}",
                       f"In {app_dir}: npm run build (or start with --dev).")
    changed = client_changes(app_dir, client_dir)
    if changed is None:
        return Finding("Web client", "warn", "built, but the build has no record (dist/build.json), so it cannot be compared with its sources",
                       f"In {app_dir}: npm run build (or start with --dev).")
    if changed:
        shown = ", ".join(changed[:3]) + (f" and {len(changed) - 3} more" if len(changed) > 3 else "")
        return Finding("Web client", "warn", f"built, but {len(changed)} source file(s) changed since the build: {shown}",
                       f"In {app_dir}: npm run build (or start with --dev).")
    return Finding("Web client", "ok", str(client_dir))


def check_environment(root: Path | None, client_dir: Path, *, app_dir: Path = HERE, data_dir: Path | None = None,
                      bundle: Bundle | None = None) -> list[Finding]:
    found = [check_python(root), check_slai(root)]
    if bundle is not None:
        found += [check_bundle(bundle), check_slai_writable(bundle.slai_root)]
    found += [check_dependency(*r, required=True) for r in REQUIRED] + [check_dependency(*r, required=False) for r in OPTIONAL]
    found += [check_program(*p) for p in PROGRAMS]
    if data_dir is not None:
        found.append(check_data_dir(data_dir))
    found.append(check_client(app_dir, client_dir))
    return found


def blocking(findings: Sequence[Finding]) -> list[Finding]:
    return [f for f in findings if f.status == "error"]


def render(findings: Sequence[Finding]) -> str:
    mark = {"ok": "ok   ", "warn": "WARN ", "error": "ERROR"}
    lines = []
    for f in findings:
        lines.append(f"  [{mark[f.status]}] {f.name}: {f.detail}")
        if f.fix and f.status != "ok":
            lines.append(f"          -> {f.fix}")
    bad, warn = len(blocking(findings)), sum(f.status == "warn" for f in findings)
    lines.append(f"{'Cannot start' if bad else 'Ready'}: {bad} blocking problem(s), {warn} warning(s).")
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------------------------ the build
def npm_command() -> str | None:
    """``npm.cmd`` on Windows (a bare ``npm`` is a script there), ``npm`` elsewhere. ``None`` when Node is not installed."""
    return shutil.which("npm.cmd" if sys.platform == "win32" else "npm")


def build_client(app_dir: Path = HERE, *, root: Path | None = None,
                 run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> None:
    """``npm run build`` in the Documaster folder (TypeScript compile, static assets, start-up loader, build record)."""
    npm = npm_command()
    if npm is None:
        raise StartupError("The web client has to be built, but Node.js (npm) was not found.",
                           hint=f"Install Node.js 20 or newer, then run `npm install` and `npm run build` in {app_dir}")
    if not (app_dir / "node_modules").is_dir():
        raise StartupError(f"The web client has to be built, but its tools are not installed ({app_dir / 'node_modules'} is missing).",
                           hint=f"Run `npm install` in {app_dir} first.")
    env = dict(os.environ)
    if root is not None:
        env["SLAI_ROOT"] = str(root)                      # the build publishes the shared start-up loader from the SLAI checkout
    done = run([npm, "run", "build"], cwd=app_dir, capture_output=True, text=True, env=env)
    if done.returncode != 0:
        tail = "\n".join((done.stdout + done.stderr).strip().splitlines()[-25:])
        raise StartupError("`npm run build` failed, so the web client was not rebuilt.",
                           hint=f"Fix the errors below, then run `npm run build` in {app_dir}.", details={"output": tail, "exit": done.returncode})
