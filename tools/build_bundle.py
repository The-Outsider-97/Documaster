#!/usr/bin/env python3
"""Assemble the self-contained folder the desktop app ships as ``resources/engine``.

    python tools/build_bundle.py --slai-root G:\\GAIA\\AI\\SLAI                      # build everything into src-tauri/engine
    python tools/build_bundle.py --slai-root ... --trust-first-download            # first build: accept the Python download, record its hash
    python tools/build_bundle.py --slai-root ... --dry-run                         # check inputs and print the plan; change nothing
    python tools/build_bundle.py --verify src-tauri/engine                         # check a finished bundle
    python tools/build_bundle.py --slai-root ... --helper libreoffice=D:\\LibreOfficePortable\\App\\libreoffice

What goes into the folder (nothing is looked up on the user's computer at run time, nothing is downloaded at run time):

    python/           the embeddable CPython from python.org (checked against tools/bundle.lock.json)
    site-packages/    the packages in tools/bundle-requirements.txt (+ what they need), installed for Windows x64 / CPython 3.13
    app/              Documaster's own Python (the ``slai`` package, run_documaster.py)
    client/           the built web client (dist/)
    slai-root/        the part of SLAI Documaster uses, from tools/stage_slai.py, with slai_bundle.json (every file, size, SHA-256)
                      and the language agent's model checkpoint
    helpers/          optional: LibreOffice, Tesseract, qpdf, only if you pass --helper (they are large and have their own licences)
    bundle.json       written LAST: its presence is what makes Documaster run in "bundled mode", so a half-built folder never looks complete

Runs on Windows. Staging SLAI and the app also works elsewhere (useful for checking the plan), but the Python runtime and wheels are
Windows ones, so a folder built elsewhere is not an installable bundle. Stdlib only; ``pip`` is run as a subprocess.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Sequence

HERE = Path(__file__).resolve().parent
APP = HERE.parent
sys.path.insert(0, str(HERE))
import stage_slai  # noqa: E402  (a sibling script, not a package)

DEFAULT_OUT = APP / "src-tauri" / "engine"
LOCK = HERE / "bundle.lock.json"
REQUIREMENTS = HERE / "bundle-requirements.txt"
REQUIREMENTS_PURE = HERE / "bundle-requirements-pure.txt"
BUNDLE_FILE = "bundle.json"
BUNDLE_VERSION = 1
COMPILED_SUFFIXES = {".pyd", ".so", ".dll", ".dylib"}
APP_EXCLUDE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")
#: Packages whose absence makes a bundle useless; ``verify`` insists on them (distribution names, normalised).
MUST_HAVE = ("pypdf", "pypdfium2", "reportlab", "python-docx", "pillow", "cryptography", "pyyaml", "numpy", "torch", "torchvision",
             "scipy", "scikit-learn", "librosa", "einops", "rotary-embedding-torch", "matplotlib", "psutil")


class BuildError(RuntimeError):
    pass


def say(text: str) -> None:
    print(text, flush=True)


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def folder_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.is_dir() else 0


def documaster_version() -> str:
    found = re.search(r'__version__\s*=\s*"([^"]+)"', (APP / "slai" / "__init__.py").read_text(encoding="utf-8"))
    if not found:
        raise BuildError("cannot read the version from slai/__init__.py")
    return found.group(1)


# ------------------------------------------------------------------------------------------------------------------------- python
def read_lock(path: Path = LOCK) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def fetch_python(lock: dict, cache: Path, supplied: Path | None, trust_first: bool, lock_path: Path = LOCK,
                 download: Callable[[str, Path], None] | None = None) -> Path:
    """The embeddable-Python zip, verified. ``supplied`` (--python-embed) is used instead of a download but is checked the same way."""
    py = lock["python"]
    if supplied is not None:
        archive = supplied
    else:
        archive = cache / Path(py["url"]).name
        if not archive.is_file():
            cache.mkdir(parents=True, exist_ok=True)
            say(f"downloading {py['url']}")
            (download or _download)(py["url"], archive)
    digest = sha256_file(archive)
    expected = (py.get("sha256") or "").lower()
    if expected:
        if digest != expected:
            if supplied is None:
                archive.unlink(missing_ok=True)
            raise BuildError(f"{archive.name} has SHA-256 {digest}, but tools/bundle.lock.json says {expected}. Not using it.")
    else:
        if not trust_first:
            raise BuildError(
                f"tools/bundle.lock.json has no SHA-256 for {archive.name} yet. This download has {digest}.\n"
                f"Compare it with the digest python.org publishes for this file ({py['version']}), then run again with "
                f"--trust-first-download to accept it and record it in the lock file.")
        py["sha256"] = digest
        lock_path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
        say(f"recorded SHA-256 {digest} in {lock_path.name}")
    return archive


def _download(url: str, target: Path) -> None:
    tmp = target.with_suffix(target.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=60) as resp, tmp.open("wb") as out:
            shutil.copyfileobj(resp, out)
        tmp.replace(target)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise BuildError(f"could not download {url}: {exc}") from exc


def extract_python(archive: Path, dest: Path) -> str:
    """Unpack the embeddable zip into ``dest`` and point its search path at the bundle. Returns the DLL stem (``python313``)."""
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    with zipfile.ZipFile(archive) as zf:
        for member in zf.namelist():
            if member.startswith("/") or ".." in Path(member).parts:
                raise BuildError(f"unsafe path in {archive.name}: {member}")
        zf.extractall(dest)
    dlls = [p.stem for p in dest.glob("python3*.dll") if p.stem != "python3"]
    if not (dest / "python.exe").is_file() or len(dlls) != 1:
        raise BuildError(f"{archive.name} does not look like an embeddable CPython (python.exe / python3XX.dll missing)")
    write_pth(dest, dlls[0])
    return dlls[0]


def write_pth(python_dir: Path, stem: str) -> None:
    """The embeddable Python reads ``<stem>._pth`` instead of the environment: standard library zip, its own folder, then our folders.
    ``import site`` is left out on purpose: no user site-packages, no ``sitecustomize``, nothing from outside the bundle."""
    (python_dir / f"{stem}._pth").write_text(f"{stem}.zip\n.\n../site-packages\n../app\n", encoding="utf-8")


# ------------------------------------------------------------------------------------------------------------------------ packages
def pip_command(target: Path, requirements: Path, py: dict, *, pure: bool, python: str | None = None) -> list[str]:
    cmd = [python or sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--no-warn-script-location",
           "--target", str(target), "-r", str(requirements)]
    if pure:
        return cmd + ["--no-deps"]                         # source-only, pure-Python packages: built on this machine, checked afterwards
    return cmd + ["--only-binary=:all:", "--platform", py["pip_platform"], "--python-version", py["pip_python_version"],
                  "--implementation", "cp", "--abi", py["pip_abi"]]


def distributions(site: Path) -> dict[str, str]:
    """Installed distributions (normalised name -> version), read from the ``*.dist-info`` folders."""
    found: dict[str, str] = {}
    for info in site.glob("*.dist-info"):
        stem = info.name[: -len(".dist-info")]
        name, _, version = stem.rpartition("-")
        if name:
            found[normalise(name)] = version
    return found


def install_packages(site: Path, py: dict, run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> dict[str, dict[str, str]]:
    shutil.rmtree(site, ignore_errors=True)
    site.mkdir(parents=True)
    say("installing packages (torch alone is several hundred megabytes) ...")
    done = run(pip_command(site, REQUIREMENTS, py, pure=False))
    if done.returncode != 0:
        raise BuildError(f"pip could not install {REQUIREMENTS.name} (exit {done.returncode}); see its output above")
    main = distributions(site)
    before = set(p.relative_to(site).as_posix() for p in site.rglob("*") if p.is_file())
    done = run(pip_command(site, REQUIREMENTS_PURE, py, pure=True))
    if done.returncode != 0:
        say(f"warning: the optional pure-Python packages could not be installed ({REQUIREMENTS_PURE.name}); continuing without them")
        pure: dict[str, str] = {}
    else:
        added = [p for p in site.rglob("*") if p.is_file() and p.relative_to(site).as_posix() not in before]
        compiled = [p.name for p in added if p.suffix.lower() in COMPILED_SUFFIXES]
        if compiled:
            raise BuildError(f"{REQUIREMENTS_PURE.name} produced compiled files ({', '.join(compiled[:3])}); they were built for this machine, "
                             "not for the bundle's Python, so they are not allowed")
        pure = {k: v for k, v in distributions(site).items() if k not in main}
    for junk in ("bin", "Scripts"):                        # launchers for this machine's Python, useless in the bundle
        shutil.rmtree(site / junk, ignore_errors=True)
    for cache in site.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    return {"packages": main, "pure": pure}


# ----------------------------------------------------------------------------------------------------------------- app, SLAI, helpers
def stage_app(out: Path) -> None:
    app = out / "app"
    shutil.rmtree(app, ignore_errors=True)
    app.mkdir(parents=True)
    shutil.copytree(APP / "slai", app / "slai", ignore=APP_EXCLUDE)
    for name in ("run_documaster.py", "requirements.txt", "LICENSE"):
        if (APP / name).is_file():
            shutil.copy2(APP / name, app / name)
    client = out / "client"
    shutil.rmtree(client, ignore_errors=True)
    dist = APP / "dist"
    if not (dist / "index.html").is_file() or not (dist / "startup" / "loader.js").is_file():
        raise BuildError(f"the web client is not built ({dist}). Run `npm install` and `npm run build` first.")
    shutil.copytree(dist, client, ignore=APP_EXCLUDE)


def stage_helpers(out: Path, specs: Sequence[str]) -> dict[str, dict]:
    helpers = out / "helpers"
    shutil.rmtree(helpers, ignore_errors=True)
    result: dict[str, dict] = {}
    for spec in specs:
        name, sep, source = spec.partition("=")
        name = name.strip().lower()
        if not sep or not re.fullmatch(r"[a-z][a-z0-9_-]*", name) or not Path(source).is_dir():
            raise BuildError(f"--helper expects NAME=FOLDER with an existing folder (got {spec!r})")
        shutil.copytree(source, helpers / name)
        result[name] = {"source": str(Path(source).resolve()), "bytes": folder_bytes(helpers / name)}
        say(f"helper {name}: {result[name]['bytes'] / 1_048_576:.0f} MiB from {source}")
    return result


def precompile(out: Path) -> None:
    """Byte-compile everything with the bundle's own Python so the installed folder never has to be written to at run time."""
    python = out / "python" / "python.exe"
    if os.name != "nt" or not python.is_file():
        say("skipping byte-compilation (needs Windows and the bundled python.exe)")
        return
    say("byte-compiling ...")
    for folder in ("site-packages", "app", "slai-root"):
        subprocess.run([str(python), "-m", "compileall", "-q", "-j", "0", str(out / folder)], check=False)


# ------------------------------------------------------------------------------------------------------------------------- verify
def requirement_names(path: Path) -> list[str]:
    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            names.append(normalise(re.split(r"[=<>!~\[ ;]", line, maxsplit=1)[0]))
    return names


def verify(out: Path) -> list[str]:
    """Problems with a finished bundle; an empty list means it has everything the app needs. Reads files only, runs nothing."""
    problems: list[str] = []
    out = Path(out)
    manifest_path = out / BUNDLE_FILE
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"{BUNDLE_FILE} is missing or unreadable ({exc}); the bundle was not finished"]
    python = out / "python"
    if not any((python / n).is_file() for n in ("python.exe", "bin/python3")):
        problems.append("python/python.exe is missing")
    pth = list(python.glob("python3*._pth"))
    if not pth:
        problems.append("python/python3XX._pth is missing, so the bundled Python would read the user's environment")
    elif "../site-packages" not in pth[0].read_text(encoding="utf-8"):
        problems.append(f"python/{pth[0].name} does not list ../site-packages")
    for rel in ("app/run_documaster.py", "app/slai/__init__.py", "app/slai/engine.py", "client/index.html", "client/startup/loader.js"):
        if not (out / rel).is_file():
            problems.append(f"{rel} is missing")
    have = distributions(out / "site-packages")
    for name in MUST_HAVE:
        if name not in have:
            problems.append(f"site-packages has no {name}")
    for name in requirement_names(REQUIREMENTS):
        if name not in have:
            problems.append(f"site-packages has no {name} (listed in {REQUIREMENTS.name})")
    problems += [f"slai-root: {p}" for p in stage_slai.verify_manifest(out / "slai-root")]
    slai = manifest.get("slai", {})
    for model in slai.get("models", []):
        if not (out / "slai-root" / model).is_file():
            problems.append(f"the model {model} is missing")
    if not slai.get("models") and manifest.get("modelsRequired", True):
        problems.append("the bundle carries no model: the language agent's LANTRA runtime would be unavailable")
    for name in manifest.get("helpers", {}):
        if not (out / "helpers" / name).is_dir():
            problems.append(f"helpers/{name} is missing")
    return problems


# --------------------------------------------------------------------------------------------------------------------------- build
def build(args: argparse.Namespace, run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> dict:
    out: Path = args.out.resolve()
    slai_root = Path(args.slai_root).resolve()
    if not stage_slai.module_file(slai_root, "src.utils.sldc"):
        raise BuildError(f"{slai_root} is not an SLAI checkout (src/utils/sldc.py is missing); pass --slai-root")
    lock = read_lock()
    say(f"bundle for SLAI Documaster {documaster_version()}, Python {lock['python']['version']}, into {out}")
    selection = stage_slai.selection(slai_root, args.models)          # fails now, not after twenty minutes of downloading
    say(f"SLAI: {len(selection['closure']['modules'])} modules, {len(selection['files'])} files, "
        f"models: {', '.join(m.name for m in selection['models']) or 'none'}")
    if args.dry_run:
        say("dry run: nothing was changed.")
        return {}
    if os.name != "nt" and not args.allow_non_windows:
        raise BuildError("The Python runtime and the wheels are for Windows; build on Windows (or pass --allow-non-windows to stage only).")
    out.mkdir(parents=True, exist_ok=True)
    (out / BUNDLE_FILE).unlink(missing_ok=True)                       # until the very end this is not a bundle
    skip = set(args.skip or [])
    py = lock["python"]
    if "python" not in skip:
        archive = fetch_python(lock, args.cache, args.python_embed, args.trust_first_download)
        stem = extract_python(archive, out / "python")
        say(f"Python runtime: {stem}")
    if "packages" not in skip:
        pkgs = install_packages(out / "site-packages", py, run)
    else:
        pkgs = {"packages": distributions(out / "site-packages"), "pure": {}}
    if "app" not in skip:
        stage_app(out)
    if "slai" not in skip:
        say("staging SLAI ...")
        args.cache.mkdir(parents=True, exist_ok=True)
        manifest = stage_slai.stage(slai_root, out / "slai-root", args.models, report=args.cache / "slai-report.json")
    else:
        manifest = json.loads((out / "slai-root" / stage_slai.MANIFEST).read_text(encoding="utf-8"))
    helpers = stage_helpers(out, args.helper or []) if "helpers" not in skip else {}
    if not args.no_compile:
        precompile(out)
    sizes = {name: folder_bytes(out / name) for name in ("python", "site-packages", "app", "client", "slai-root", "helpers")}
    sizes["total"] = sum(sizes.values())
    record = {
        "bundleVersion": BUNDLE_VERSION,
        "documasterVersion": documaster_version(),
        "builtAt": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "platform": py["pip_platform"],
        "python": py["version"],
        "pythonZipSha256": py.get("sha256"),
        "requirementsSha256": sha256_file(REQUIREMENTS),
        "packages": pkgs["packages"],
        "optionalPurePackages": pkgs["pure"],
        "slai": {"revision": manifest.get("slaiRevision"), "modules": manifest["modules"], "files": manifest["files"],
                 "models": [m["path"] for m in manifest["models"]], "thirdParty": sorted(manifest["thirdParty"])},
        "helpers": helpers,
        "modelsRequired": args.models != "none",
        "sizes": sizes,
    }
    (out / BUNDLE_FILE).write_text(json.dumps(record, indent=1), encoding="utf-8")      # last: now it is a bundle
    problems = verify(out)
    if problems:
        raise BuildError("the finished bundle does not pass its own check:\n  " + "\n  ".join(problems))
    say(f"done: {sizes['total'] / 1_048_576:.0f} MiB uncompressed ({', '.join(f'{k} {v / 1_048_576:.0f}' for k, v in sizes.items() if k != 'total')})")
    return record


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--slai-root", type=Path, default=Path(os.environ.get("SLAI_ROOT") or APP.parent.parent),
                    help="the SLAI checkout (default: $SLAI_ROOT, else two folders above this app, where it lives inside SLAI)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="where to build (default: src-tauri/engine)")
    ap.add_argument("--cache", type=Path, default=APP / ".bundle-cache", help="where downloads are kept")
    ap.add_argument("--models", choices=("lantra", "none"), default="lantra",
                    help="lantra: carry the language agent's checkpoint (default); none: a bundle without it (agents fall back)")
    ap.add_argument("--python-embed", type=Path, help="use this embeddable-Python zip instead of downloading one")
    ap.add_argument("--trust-first-download", action="store_true", help="accept the Python download when the lock file has no hash yet, and record it")
    ap.add_argument("--helper", action="append", metavar="NAME=FOLDER", help="copy a program folder into helpers/ (libreoffice, tesseract, qpdf); repeatable")
    ap.add_argument("--skip", action="append", choices=("python", "packages", "app", "slai", "helpers"), help="reuse that part of an earlier build; repeatable")
    ap.add_argument("--no-compile", action="store_true", help="do not byte-compile at the end")
    ap.add_argument("--dry-run", action="store_true", help="check inputs, analyse SLAI, print the plan; change nothing")
    ap.add_argument("--allow-non-windows", action="store_true", help="stage on another OS (the result is not an installable bundle)")
    ap.add_argument("--verify", type=Path, metavar="BUNDLE", help="check a finished bundle and exit")
    args = ap.parse_args(argv)
    try:
        if args.verify:
            problems = verify(args.verify)
            for p in problems:
                print(p)
            print("bundle is complete" if not problems else f"{len(problems)} problem(s)")
            return 1 if problems else 0
        build(args)
    except (BuildError, stage_slai.StageError) as exc:
        print(f"build_bundle: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
