"""Documaster's view of the SLAI Reader subsystem (``src/agents/reader``).

The Reader engines are used as they are:

* ``ParserEngine.parse``      text of txt/md/html/xml/csv/json/docx/pdf (no OCR, no encrypted PDF)
* ``DetectionEngine.detect``  what a file really is (signature based, not just its extension)
* ``IntegrityEngine``         ``verify_output`` for every file Documaster writes
* ``CompressionEngine``       lossless ``optimize`` for PDF

Nothing is re-implemented here. When an engine cannot be imported the corresponding method raises
:class:`~slai.errors.DependencyMissing` and ``status()`` says so; callers degrade, they never fake a result.
"""
from __future__ import annotations

import importlib
import threading
from pathlib import Path
from typing import Any

from ..errors import DependencyMissing, DocumasterError, FileRejected
from . import slai_root

_ENGINES = {
    "parser": ("src.agents.reader.parser_engine", "ParserEngine"),
    "detector": ("src.agents.reader.detection_engine", "DetectionEngine"),
    "integrity": ("src.agents.reader.integrity_engine", "IntegrityEngine"),
    "compression": ("src.agents.reader.compression_engine", "CompressionEngine"),
}


class ReaderBridge:
    """Lazy, thread-safe holder of the real Reader engines."""

    def __init__(self) -> None:
        self._engines: dict[str, Any] = {}
        self._errors: dict[str, str] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ engines
    def engine(self, name: str) -> Any:
        with self._lock:
            if name in self._engines:
                return self._engines[name]
            if name in self._errors:
                raise DependencyMissing(f"Reader {name} engine is unavailable: {self._errors[name]}")
            module_name, class_name = _ENGINES[name]
            try:
                module = importlib.import_module(module_name)
                cls = getattr(module, class_name)
                if name == "integrity":
                    instance = cls()
                elif name == "compression":
                    instance = cls(integrity=self._engines.get("integrity") or self._build("integrity"))
                else:
                    instance = cls()
            except Exception as exc:  # noqa: BLE001 - any import/constructor problem means "not available"
                self._errors[name] = f"{type(exc).__name__}: {str(exc)[:300]}"
                raise DependencyMissing(f"Reader {name} engine is unavailable: {self._errors[name]}") from exc
            self._engines[name] = instance
            return instance

    def _build(self, name: str) -> Any:
        module_name, class_name = _ENGINES[name]
        instance = getattr(importlib.import_module(module_name), class_name)()
        self._engines[name] = instance
        return instance

    def available(self, name: str) -> bool:
        try:
            self.engine(name)
            return True
        except DependencyMissing:
            return False

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {"root": str(slai_root()) if slai_root() else None, "engines": {}}
        for name in _ENGINES:
            ok = self.available(name)
            out["engines"][name] = {"available": ok, "error": None if ok else self._errors.get(name)}
        return out

    # ------------------------------------------------------------------ operations
    def parse(self, path: str | Path) -> dict[str, Any]:
        """Reader ``parse`` result (``content``, ``metadata``, ``warnings`` ...). Raises FileRejected on Reader errors."""
        parser = self.engine("parser")
        try:
            return parser.parse(str(path))
        except DocumasterError:
            raise
        except Exception as exc:  # noqa: BLE001 - ReaderError subclasses carry a readable message
            raise FileRejected(f"Reader could not parse this file: {exc}",
                               details={"reader_error": type(exc).__name__}) from exc

    def parser_capabilities(self) -> dict[str, Any]:
        try:
            return dict(self.engine("parser").capabilities())
        except DependencyMissing:
            return {}

    def detect(self, path: str | Path) -> dict[str, Any]:
        """The ``payload`` of Reader's detection: ``format_id``, ``family``, ``encrypted``, ``damaged`` ..."""
        detector = self.engine("detector")
        try:
            result = detector.detect(str(path))
        except Exception as exc:  # noqa: BLE001
            raise FileRejected(f"Reader could not inspect this file: {exc}") from exc
        payload = result.get("payload") if isinstance(result, dict) else None
        return dict(payload or {})

    def verify_output(self, path: str | Path, fmt: str) -> dict[str, Any]:
        """Run Reader's integrity check on a written file; never raises for a failed check.

        Returns ``{"ran": bool, "valid": bool|None, "failed": [check names], "detail": str|None}``. ``ran`` is False
        when Reader has no engine or does not know the format; the caller reports that rather than claiming a pass.
        """
        try:
            integrity = self.engine("integrity")
        except DependencyMissing as exc:
            return {"ran": False, "valid": None, "failed": [], "detail": str(exc)}
        try:
            result = integrity.verify_output(str(path), fmt)
        except Exception as exc:  # noqa: BLE001 - e.g. an unknown format id
            return {"ran": False, "valid": None, "failed": [], "detail": f"{type(exc).__name__}: {str(exc)[:200]}"}
        payload = (result or {}).get("payload", {})
        failed = [c.get("name") for c in payload.get("checks", []) if not c.get("ok")]
        return {"ran": True, "valid": bool(payload.get("valid")), "failed": failed, "detail": None,
                "sha256": (payload.get("hashes") or {}).get("sha256")}

    def optimize_pdf(self, path: str | Path, out_dir: str | Path) -> dict[str, Any]:
        """Lossless PDF optimisation through Reader. ``outcome`` is ``optimized`` or ``no_gain``."""
        engine = self.engine("compression")
        try:
            result = engine.optimize(str(path), str(out_dir), lossy=False)
        except Exception as exc:  # noqa: BLE001
            raise FileRejected(f"Reader could not optimise this PDF: {exc}") from exc
        payload = result.get("payload", {}) if isinstance(result, dict) else {}
        report = payload.get("report", payload)
        return dict(report)
