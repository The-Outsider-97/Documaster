"""The objects every feature module shares: settings, the Reader bridge, the agent gateway, tool discovery."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .bridge import find_binary, have_module
from .bridge.agents import AgentGateway
from .bridge.reader import ReaderBridge
from .config import Settings
from .errors import Cancelled

ProgressFn = Callable[[float, str], None]


class JobControl:
    """Progress + cooperative cancellation handed to long operations."""

    def __init__(self, progress: ProgressFn | None = None) -> None:
        self._progress = progress
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    def check(self) -> None:
        if self.cancelled:
            raise Cancelled("The operation was cancelled")

    def report(self, fraction: float, message: str = "") -> None:
        self.check()
        if self._progress:
            self._progress(max(0.0, min(1.0, fraction)), message)


NO_CONTROL = JobControl()


@dataclass
class Tools:
    """External programs and optional Python packages, discovered once and reported by the capability matrix."""
    soffice: str | None = None
    tesseract: str | None = None
    qpdf: str | None = None
    modules: dict[str, bool] = field(default_factory=dict)
    tesseract_langs: list[str] = field(default_factory=list)

    @classmethod
    def discover(cls) -> "Tools":
        extra = ("C:/Program Files/LibreOffice/program", "C:/Program Files (x86)/LibreOffice/program",
                 "C:/Program Files/Tesseract-OCR", "C:/Program Files (x86)/Tesseract-OCR",
                 "/Applications/LibreOffice.app/Contents/MacOS", "/usr/lib/libreoffice/program")
        tools = cls(
            soffice=find_binary("soffice", "libreoffice", extra_dirs=extra),
            tesseract=find_binary("tesseract", extra_dirs=extra),
            qpdf=find_binary("qpdf"),
            modules={name: have_module(name) for name in
                     ("pypdf", "pypdfium2", "reportlab", "docx", "PIL", "cryptography", "yaml")},
        )
        if tools.tesseract:
            try:
                out = subprocess.run([tools.tesseract, "--list-langs"], capture_output=True, text=True, timeout=15)
                tools.tesseract_langs = [x.strip() for x in out.stdout.splitlines()[1:] if x.strip()]
            except (OSError, subprocess.SubprocessError):
                tools.tesseract_langs = []
        return tools

    def has(self, module: str) -> bool:
        return bool(self.modules.get(module))


@dataclass
class Services:
    settings: Settings
    reader: ReaderBridge
    agents: AgentGateway
    tools: Tools

    @classmethod
    def create(cls, settings: Settings) -> "Services":
        return cls(settings=settings, reader=ReaderBridge(),
                   agents=AgentGateway(mode=settings.agents, timeout_s=settings.agent_timeout_s),
                   tools=Tools.discover())

    def work_dir(self, name: str) -> Path:
        path = self.settings.data_dir / name
        path.mkdir(parents=True, exist_ok=True)
        return path
