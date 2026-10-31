"""Writers: document model -> file. Each returns a :class:`WriteResult` that states what was kept and what was not."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class WriteResult:
    path: Path
    format: str
    backend: str
    extras: list[Path] = field(default_factory=list)      # additional files written by the converter
    extra_data: list[tuple[str, bytes]] = field(default_factory=list)   # (relative name, bytes) the writer wants beside the file
    warnings: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    lost: list[str] = field(default_factory=list)
    pages: int | None = None
    # Optional measured layout from the same renderer that produced the file.
    # Block IDs are stable native document references; no geometry is guessed.
    layout: dict[str, Any] | None = None


@dataclass
class WriteOptions:
    """User-facing options. Every field has a documented default."""
    title: str | None = None
    page_size: str | None = None          # A4, Letter ... overrides section pages when set
    margins_mm: float | None = None
    font_size_pt: float = 11.0
    font_family: str | None = None        # for PDF/DOCX body text (a name found on this machine)
    page_numbers: bool = True
    author: str | None = None
    include_notes: bool = True
    line_ending: str = "\n"               # txt/md/html/xml
    stem: str = "document"                # final file stem (Markdown image folder is named after it)


Writer = Callable[[Any, Path, WriteOptions], WriteResult]


def get_writer(fmt: str) -> Writer:
    """The writer for ``fmt`` (imported lazily so optional dependencies are only needed when used)."""
    if fmt == "pdf":
        from .write_pdf import write_pdf as fn
    elif fmt == "docx":
        from .write_docx import write_docx as fn
    elif fmt == "odt":
        from .write_odt import write_odt as fn
    elif fmt == "rtf":
        from .write_rtf import write_rtf as fn
    elif fmt == "html":
        from .text_writers import write_html as fn
    elif fmt == "md":
        from .text_writers import write_md as fn
    elif fmt == "txt":
        from .text_writers import write_txt as fn
    elif fmt == "xml":
        from .text_writers import write_xml as fn
    elif fmt == "sldc":
        from .text_writers import write_sldc as fn
    else:
        from ...errors import UnsupportedOperation
        raise UnsupportedOperation(f"Documaster cannot write {fmt} files", details={"format": fmt})
    return fn
