"""The formats Documaster knows, how a file is recognised, and what may be read or written.

Recognition looks at the bytes first (magic numbers, ZIP members, SLDC manifest) and only then at the extension. A
mismatch is reported to the user instead of being silently trusted.
"""
from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Format:
    id: str
    label: str
    exts: tuple[str, ...]
    mime: str
    family: str          # pdf | office | text | sldc | image | spreadsheet
    paged: bool = False  # has a fixed page structure


FORMATS: dict[str, Format] = {f.id: f for f in (
    Format("pdf", "PDF", (".pdf",), "application/pdf", "pdf", paged=True),
    Format("sldc", "SLDC", (".sldc",), "application/x-slai-sldc+zip", "sldc"),
    Format("docx", "Word (DOCX)", (".docx",), "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "office"),
    Format("doc", "Word 97-2003 (DOC)", (".doc",), "application/msword", "office"),
    Format("odt", "OpenDocument Text", (".odt",), "application/vnd.oasis.opendocument.text", "office"),
    Format("rtf", "Rich Text", (".rtf",), "application/rtf", "office"),
    Format("html", "HTML", (".html", ".htm", ".xhtml"), "text/html", "text"),
    Format("md", "Markdown", (".md", ".markdown"), "text/markdown", "text"),
    Format("txt", "Plain text", (".txt", ".text", ".log"), "text/plain", "text"),
    Format("xml", "XML", (".xml",), "application/xml", "text"),
    Format("csv", "CSV", (".csv",), "text/csv", "text"),
    Format("png", "PNG image", (".png",), "image/png", "image"),
    Format("jpeg", "JPEG image", (".jpg", ".jpeg"), "image/jpeg", "image"),
)}

EXT_TO_FORMAT: dict[str, str] = {ext: f.id for f in FORMATS.values() for ext in f.exts}

# Formats Documaster can produce from its document model.
TARGETS = ("pdf", "docx", "odt", "rtf", "html", "md", "txt", "xml", "sldc")


def format_for_ext(name: str) -> str | None:
    return EXT_TO_FORMAT.get(Path(name).suffix.lower())


def ext_for(fmt: str) -> str:
    return FORMATS[fmt].exts[0]


def label_for(fmt: str) -> str:
    return FORMATS[fmt].label if fmt in FORMATS else fmt


def sniff(path: str | Path, name: str | None = None) -> dict:
    """Identify a file from its bytes. Returns ``{"format": id|None, "claimed": id|None, "mismatch": bool, "note": str}``.

    ``name`` is the name the person gave the file (uploads are stored under a neutral name); the extension is only a claim that the
    content is checked against, except for text formats (Markdown, CSV) that cannot be told apart from plain text by content."""
    p = Path(path)
    claimed = format_for_ext(name or p.name)
    try:
        with open(p, "rb") as handle:
            head = handle.read(4096)
    except OSError as exc:
        return {"format": None, "claimed": claimed, "mismatch": False, "note": f"unreadable: {exc}"}
    found: str | None = None
    note = ""
    if head.startswith(b"%PDF-"):
        found = "pdf"
    elif head.startswith(b"{\\rtf"):
        found = "rtf"
    elif head.startswith(b"\x89PNG\r\n\x1a\n"):
        found = "png"
    elif head.startswith(b"\xff\xd8\xff"):
        found = "jpeg"
    elif head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        found = "doc"  # OLE2 compound file; could be .xls/.ppt too, the extension decides below
        if claimed not in (None, "doc"):
            found, note = None, "OLE2 compound file (legacy Office) that is not a Word document"
    elif head.startswith(b"PK\x03\x04"):
        found = _sniff_zip(p)
    else:
        found = _sniff_text(head, claimed)
    mismatch = bool(found and claimed and found != claimed and not _compatible(found, claimed))
    return {"format": found, "claimed": claimed, "mismatch": mismatch, "note": note}


def _compatible(found: str, claimed: str) -> bool:
    text_like = {"txt", "md", "csv", "html", "xml"}
    return found in text_like and claimed in text_like


def _sniff_zip(path: Path) -> str | None:
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "manifest.json" in names and "mimetype" in names:
                try:
                    if archive.read("mimetype")[:64].startswith(b"application/x-slai-sldc"):
                        return "sldc"
                except (KeyError, RuntimeError):
                    pass
            if "word/document.xml" in names:
                return "docx"
            if "mimetype" in names:
                try:
                    if archive.read("mimetype").strip() == b"application/vnd.oasis.opendocument.text":
                        return "odt"
                except (KeyError, RuntimeError):
                    pass
    except (zipfile.BadZipFile, OSError):
        return None
    return None


def _sniff_text(head: bytes, claimed: str | None) -> str | None:
    if b"\x00" in head[:2048] and not head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return None
    if head.startswith(b"AT&TSLDC") or head.startswith(b"SLDC"):
        return None
    probe = head.lstrip(b"\xef\xbb\xbf \t\r\n")[:200].lower()
    if probe.startswith((b"<!doctype html", b"<html")):
        return "html"
    if probe.startswith(b"<?xml"):
        return "xml"
    if claimed in ("md", "csv", "html", "xml", "txt"):
        return claimed
    return "txt"
