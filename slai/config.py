"""Runtime settings. Everything has a safe default; environment variables and CLI flags override."""
from __future__ import annotations

import os
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path

MIB = 1024 * 1024


def _default_data_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "SLAI" / "Documaster"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "slai" / "documaster"


def _default_output_dir() -> Path:
    docs = Path.home() / "Documents"
    return (docs if docs.exists() else Path.home()) / "Documaster"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8766
    token: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    slai_root: Path | None = None
    data_dir: Path = field(default_factory=_default_data_dir)
    output_dir: Path = field(default_factory=_default_output_dir)
    max_upload_bytes: int = field(default_factory=lambda: _env_int("DOCUMASTER_MAX_UPLOAD_MB", 200) * MIB)
    max_pdf_pages: int = field(default_factory=lambda: _env_int("DOCUMASTER_MAX_PDF_PAGES", 5000))
    max_text_chars: int = field(default_factory=lambda: _env_int("DOCUMASTER_MAX_TEXT_CHARS", 20_000_000))
    max_sources_per_merge: int = 200
    max_plan_items: int = 20_000
    job_timeout_s: int = field(default_factory=lambda: _env_int("DOCUMASTER_JOB_TIMEOUT_S", 900))
    subprocess_timeout_s: int = 120
    session_ttl_s: int = 12 * 3600
    max_revisions: int = 30
    agent_timeout_s: float = field(default_factory=lambda: float(_env_int("DOCUMASTER_AGENT_TIMEOUT_S", 60)))
    agents: str = "auto"           # auto | none | strict
    agent_read_default: bool = True   # may SLAI agents read document text (they run on this computer; Privacy agent still gates)
    agent_telemetry: bool = False  # send job telemetry to the Observability agent / failures to the Handler agent (creates those agents)
    # Documents never leave this machine unless the user allows a remote path (none is built in).
    allow_remote_processing: bool = False
    persist_recent: bool = False   # recent-file names are kept only if the user opts in
    open_browser: bool = True

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def is_windows(self) -> bool:
        return sys.platform.startswith("win")

    def public(self) -> dict:
        return {"output_dir": str(self.output_dir), "max_upload_mb": self.max_upload_bytes // MIB,
                "max_pdf_pages": self.max_pdf_pages, "agents": self.agents,
                "allow_remote_processing": self.allow_remote_processing, "agent_read_default": self.agent_read_default, "agent_telemetry": self.agent_telemetry,
                "persist_recent": self.persist_recent, "job_timeout_s": self.job_timeout_s}
