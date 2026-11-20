"""What a host program (the desktop shell, a test, a launcher) needs from Documaster, kept apart from the HTTP server.

Two things live here.

**The launch protocol** (``--ready-line``, ``--exit-on-stdin-close``, ``--port-fallback``). The shell starts Documaster as a child process
with its stdin held open and reads its stdout:

* one line ``DOCUMASTER_READY {"url": "...", "port": 47870, "version": "...", "bundled": true}`` when the server is listening. ``url``
  carries the per-launch access token; the shell opens it and must not write it to a log;
* or one line ``DOCUMASTER_ERROR <one line of text>`` when Documaster cannot start, followed by a non-zero exit;
* closing the child's stdin means "quit": the server stops and the process exits. The shell needs no signal or ``taskkill`` for that,
  and nothing is left behind if the shell itself dies (the operating system closes its pipes).

**The bundled layout.** In the installed desktop app Documaster, its Python, its packages, SLAI and the models sit side by side in one
folder that contains ``bundle.json`` (written by ``tools/build_bundle.py``). When that file is found Documaster runs in *bundled mode*:
SLAI root and web client are taken from the bundle, helper programs (LibreOffice, Tesseract, qpdf) are looked up in ``helpers/`` first,
and SLAI's model hub is forced to local, offline use - documents and model requests never reach the network.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable, Mapping, MutableMapping

READY = "DOCUMASTER_READY"
ERROR = "DOCUMASTER_ERROR"
BUNDLE_FILE = "bundle.json"
MAX_ERROR_CHARS = 600


# ------------------------------------------------------------------------------------------------------------------ protocol lines
def ready_line(url: str, *, port: int, version: str, bundled: bool = False) -> str:
    payload = {"url": url, "port": int(port), "version": version, "bundled": bool(bundled)}
    return f"{READY} {json.dumps(payload, separators=(',', ':'), ensure_ascii=True)}"


def error_line(message: str) -> str:
    """``message`` on one line (the shell reads line by line), without control characters, and not unboundedly long."""
    text = " ".join("".join(c if c.isprintable() or c in " \t\r\n" else " " for c in str(message)).split())
    return f"{ERROR} {text[:MAX_ERROR_CHARS] or 'Documaster could not start.'}"


def parse_line(line: str) -> tuple[str, Any] | None:
    """The reading side, as the shell implements it: ``("ready", {...})``, ``("error", "text")`` or ``None`` for any other line."""
    line = line.strip()
    if line.startswith(READY + " "):
        try:
            data = json.loads(line[len(READY) + 1:])
        except ValueError:
            return None
        return ("ready", data) if isinstance(data, dict) and isinstance(data.get("url"), str) else None
    if line.startswith(ERROR) and (len(line) == len(ERROR) or line[len(ERROR)] == " "):
        return ("error", line[len(ERROR):].strip())
    return None


# ------------------------------------------------------------------------------------------------------------------ stdin = lifeline
def watch_stdin(on_close: Callable[[], None], stream: BinaryIO | None = None) -> threading.Thread:
    """Call ``on_close`` once ``stream`` (default: stdin) reaches end of file or becomes unreadable, on a daemon thread.

    Anything the host writes is read and discarded; only the close matters.
    """
    import sys

    source = stream if stream is not None else getattr(sys.stdin, "buffer", None)

    def run() -> None:
        try:
            if source is not None:
                while source.read(4096):
                    pass
        except (OSError, ValueError):
            pass
        finally:
            on_close()

    thread = threading.Thread(target=run, name="dm-stdin-watch", daemon=True)
    thread.start()
    return thread


# ------------------------------------------------------------------------------------------------------------------ the bundle
@dataclass(frozen=True)
class Bundle:
    root: Path

    @property
    def slai_root(self) -> Path:
        return self.root / "slai-root"

    @property
    def client_dir(self) -> Path:
        return self.root / "client"

    @property
    def helpers(self) -> Path:
        return self.root / "helpers"

    def manifest(self) -> dict[str, Any]:
        try:
            data = json.loads((self.root / BUNDLE_FILE).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def helper_dirs(self) -> list[str]:
        """Folders inside ``helpers/`` that can hold a program: each helper's own folder, ``bin`` and ``program`` (LibreOffice)."""
        found: list[str] = []
        if self.helpers.is_dir():
            for helper in sorted(p for p in self.helpers.iterdir() if p.is_dir()):
                for sub in ("", "bin", "program"):
                    folder = helper / sub if sub else helper
                    if folder.is_dir():
                        found.append(str(folder))
        return found


def find_bundle(app_dir: Path, environ: Mapping[str, str] | None = None) -> Bundle | None:
    """The bundle Documaster is running from, or ``None`` when it runs from a checkout.

    ``app_dir`` is the folder that holds the ``slai`` package; in the installed app that is ``<bundle>/app``. ``DOCUMASTER_BUNDLE_DIR``
    names a bundle explicitly (tests, or a bundle staged but not yet installed).
    """
    env = os.environ if environ is None else environ
    candidates = [Path(env["DOCUMASTER_BUNDLE_DIR"])] if env.get("DOCUMASTER_BUNDLE_DIR") else [app_dir.parent]
    for folder in candidates:
        if (folder / BUNDLE_FILE).is_file():
            return Bundle(folder.resolve())
    return None


#: Set unconditionally in bundled mode: whatever the user's environment says, a document-processing app must not fetch models or phone home.
OFFLINE_ENV = {
    "SLAI_MODEL_SOURCE": "local",       # slai model hub: never download a model; use the file in the bundle
    "SLAI_MODEL_OFFLINE": "1",
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "DO_NOT_TRACK": "1",
}


def bundled_environment(bundle: Bundle, data_dir: Path) -> dict[str, str]:
    """Environment variables for bundled mode. Offline settings always win; the rest only fill what is not set."""
    cache = data_dir / "cache"
    env = dict(OFFLINE_ENV)
    env.update({
        "PYTHONDONTWRITEBYTECODE": "1",             # the install folder is not written to; the build pre-compiles
        "PYTHONUTF8": "1",
        "PYTHONUNBUFFERED": "1",
        "MPLCONFIGDIR": str(cache / "matplotlib"),   # matplotlib, numba and joblib would otherwise write next to their sources / into $HOME
        "NUMBA_CACHE_DIR": str(cache / "numba"),
        "JOBLIB_TEMP_FOLDER": str(cache / "joblib"),
        "TORCHINDUCTOR_CACHE_DIR": str(cache / "torch"),
        "SLAI_ROOT": str(bundle.slai_root),
    })
    tessdata = bundle.helpers / "tesseract" / "tessdata"
    if tessdata.is_dir():
        env["TESSDATA_PREFIX"] = str(tessdata)
    return env


_ALWAYS = set(OFFLINE_ENV) | {"SLAI_ROOT"}


def apply_environment(updates: Mapping[str, str], environ: MutableMapping[str, str] | None = None) -> None:
    target = os.environ if environ is None else environ
    for key, value in updates.items():
        if key in _ALWAYS:
            target[key] = value
        else:
            target.setdefault(key, value)


def bundle_summary(bundle: Bundle) -> dict[str, Any]:
    """The few facts about the bundle the System page shows (the full manifest stays on disk)."""
    m = bundle.manifest()
    slai = m.get("slai") if isinstance(m.get("slai"), dict) else {}
    return {"version": m.get("documasterVersion"), "built": m.get("builtAt"), "python": m.get("python"),
            "slaiRevision": slai.get("revision"), "models": slai.get("models", []), "helpers": sorted(m.get("helpers", {}) or {})}
