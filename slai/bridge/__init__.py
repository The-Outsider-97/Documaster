"""Connection to the SLAI checkout: locate it, put it on ``sys.path``, expose shared modules.

Documaster consumes SLAI through its public modules only (``src.utils.sldc``, ``src.agents.reader``
engines, ``AgentFactory``). It never copies or re-implements them.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import shutil
import sys
from pathlib import Path
from types import ModuleType

from ..errors import DependencyMissing

_ROOT: Path | None = None


def looks_like_slai_root(path: Path) -> bool:
    return (path / "src" / "utils" / "sldc.py").is_file()


def find_slai_root(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    if os.environ.get("SLAI_ROOT"):
        candidates.append(Path(os.environ["SLAI_ROOT"]))
    here = Path(__file__).resolve()
    candidates.extend(here.parents)
    for candidate in candidates:
        try:
            if looks_like_slai_root(candidate):
                return candidate.resolve()
        except OSError:
            continue
    return None


def activate(root: Path | None) -> Path | None:
    """Make ``import src...`` work. The SLAI root goes *after* this app so ``slai`` stays ours."""
    global _ROOT
    if root is None:
        return None
    text = str(root)
    if text not in sys.path:
        sys.path.append(text)
    _ROOT = root
    return root


def slai_root() -> Path | None:
    return _ROOT


def sldc() -> ModuleType:
    """The authoritative SLDC implementation (``src/utils/sldc.py``)."""
    try:
        return importlib.import_module("src.utils.sldc")
    except ImportError as exc:  # pragma: no cover - exercised only without a checkout
        raise DependencyMissing(
            "The SLDC module (src/utils/sldc.py) was not found. Set SLAI_ROOT or pass --slai-root "
            "to the SLAI checkout.", details={"import_error": str(exc)}) from exc


def have_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def find_binary(*names: str, extra_dirs: tuple[str, ...] = ()) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
        for directory in extra_dirs:
            candidate = Path(directory) / name
            for suffix in ("", ".exe"):
                probe = candidate.with_name(candidate.name + suffix)
                if probe.is_file():
                    return str(probe)
    return None
