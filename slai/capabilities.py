"""The capability matrix, computed from what is actually installed on this machine.

Nothing here is a promise: each route says which backend does the work, what it needs, whether it is available right now, what it
keeps and what it loses. The interface greys out routes that are unavailable and explains why, and the reports in ``docs/`` are
generated from the same data.
"""
from __future__ import annotations

from typing import Any

from . import __version__
from .formats import FORMATS, TARGETS, label_for
from .services import Services

SOURCES = ("pdf", "sldc", "docx", "doc", "odt", "rtf", "html", "md", "txt", "xml", "csv")


def _read_route(fmt: str, svc: Services) -> dict[str, Any]:
    t = svc.tools
    has = t.has
    r: dict[str, Any] = {"format": fmt, "label": label_for(fmt), "available": True, "backend": "", "needs": [], "limits": []}
    if fmt == "pdf":
        r.update(backend="pypdf (text layer, page by page)", needs=["pypdf"], available=has("pypdf"),
                 limits=["No layout reconstruction: headings are inferred from text shape, tables and images are not extracted",
                         "Scanned pages need OCR (Tesseract)" + ("" if t.tesseract else " - not installed")])
    elif fmt == "sldc":
        r.update(backend="SLAI sldc.py (authoritative)", needs=["SLAI checkout"], available=True,
                 limits=["Encrypted documents need their password"])
    elif fmt == "docx":
        r.update(backend="python-docx" if has("docx") else "SLAI Reader parser (text only)", needs=["python-docx (recommended)"],
                 available=True, limits=["Text boxes, equations and tracked changes are not read"] if has("docx") else
                 ["Without python-docx only text and headings are read"])
    elif fmt == "odt":
        r.update(backend="Documaster ODF reader", available=True, limits=["Equations, drawings and tracked changes are not read"])
    elif fmt == "html":
        r.update(backend="Documaster HTML reader (scripts ignored, never executed)", available=True,
                 limits=["Only embedded PNG/JPEG data-URI images are kept; CSS layout is not applied"])
    elif fmt in ("md", "txt", "xml"):
        r.update(backend="SLAI Reader ParserEngine", available=True,
                 limits=["Markdown: headings, lists, pipe tables and inline emphasis are read" if fmt == "md" else "Structure follows the text"])
    elif fmt == "csv":
        r.update(backend="Documaster CSV reader", available=True, limits=["Becomes one table"])
    elif fmt in ("rtf", "doc"):
        r.update(backend="LibreOffice (headless) -> DOCX -> python-docx", needs=["LibreOffice"], available=bool(t.soffice),
                 limits=["Requires LibreOffice to be installed" + ("" if t.soffice else " - not found on this machine")])
    return r


def _write_route(fmt: str, svc: Services) -> dict[str, Any]:
    t = svc.tools
    has = t.has
    base: dict[str, Any] = {"format": fmt, "label": label_for(fmt), "available": True, "needs": [], "limits": []}
    if fmt == "pdf":
        base.update(backend="ReportLab (embedded TrueType fonts)", needs=["reportlab"], available=has("reportlab"),
                    limits=["No tagged PDF / alt text", "Right-to-left text is not shaped",
                            "Footnotes appear as a notes list", "Fixed layout: re-importing a PDF returns text only"])
    elif fmt == "docx":
        base.update(backend="python-docx", needs=["python-docx"], available=has("docx"),
                    limits=["Footnotes appear as a notes list", "Comments and revision history are not written"])
    elif fmt == "odt":
        base.update(backend="Documaster ODF writer", limits=["Comments and revision history are not written"])
    elif fmt == "rtf":
        base.update(backend="Documaster RTF writer", limits=["Fonts are named, not embedded"])
    elif fmt == "html":
        base.update(backend="Documaster HTML writer (self-contained, no scripts)", limits=["Page layout is not reproduced"])
    elif fmt == "md":
        base.update(backend="SLDC to_markdown", limits=["Images are written next to the file in <name>_files/", "No page layout"])
    elif fmt == "txt":
        base.update(backend="SLDC extract_text", limits=["All formatting is lost; tables become aligned text"])
    elif fmt == "xml":
        base.update(backend="Documaster XML schema urn:slai:documaster:document:1", limits=["A plain structural export"])
    elif fmt == "sldc":
        base.update(backend="SLAI sldc.py dumps_package", limits=["Password encryption needs the 'cryptography' package"
                                                                   + ("" if has("cryptography") else " - not installed")])
    return base


def matrix(svc: Services) -> dict[str, Any]:
    t = svc.tools
    reads = {f: _read_route(f, svc) for f in SOURCES}
    writes = {f: _write_route(f, svc) for f in TARGETS}
    conversions: list[dict[str, Any]] = []
    for s in SOURCES:
        for d in TARGETS:
            if s == d:
                continue
            model_ok = reads[s]["available"] and writes[d]["available"]
            lo_pair = s in ("docx", "doc", "odt", "rtf", "html", "txt", "md") and d in ("pdf", "docx", "odt", "rtf", "html", "txt")
            conversions.append({"from": s, "to": d, "model": {"available": model_ok, "layoutPreserved": False},
                                "libreoffice": {"available": bool(t.soffice), "layoutPreserved": True} if lo_pair else None})
    workspaces = [
        {"id": "counter", "label": "Word Counter", "available": True,
         "does": ["words, characters, sentences, paragraphs, reading and speaking time", "language detection", "per page / per part breakdown",
                  "top terms", "goals (minimum / maximum)", "batch of documents with totals"],
         "limits": ["Counting rules are listed in the user guide; PDFs without a text layer count 0 words unless OCR is on"]},
        {"id": "converter", "label": "Converter", "available": True,
         "does": ["PDF, SLDC, DOCX, DOC, ODT, RTF, HTML, Markdown, text, XML, CSV to PDF, DOCX, ODT, RTF, HTML, Markdown, text, XML, SLDC",
                  "every output re-opened and checked before it is delivered", "optional layout-preserving engine (LibreOffice)",
                  "OCR for scanned PDFs (Tesseract)" if t.tesseract else "OCR needs Tesseract (not installed)"],
         "limits": ["Conversions go through the SLDC document model: text, structure, tables, links and images are kept, page layout is not"]},
        {"id": "merger", "label": "Merger & Page Organizer", "available": has_all(svc, "pypdf"),
         "does": ["merge PDFs, images and documents of other types", "reorder, rotate, delete, duplicate, insert blank pages",
                  "PDF pages copied unchanged; SLDC merged with id/style/section/notes remapping and provenance",
                  "output as PDF or any model format"],
         "limits": ["SLDC pages are logical pages (breaks, sections, headings), not printed pages", "Signatures are not carried into a merged document"]},
        {"id": "editor", "label": "PDF Editor", "available": has_all(svc, "pypdf", "pypdfium2", "reportlab"),
         "does": ["page operations", "highlight, underline, strikeout, notes, shapes, drawing", "text, image, page-number and watermark stamps",
                  "form filling and flattening", "true redaction (pages are rasterised, result verified)", "metadata, bookmarks, passwords",
                  "lossless compression through SLAI Reader", "OCR" if t.tesseract else "OCR (needs Tesseract)", "undo / redo with revision history"],
         "limits": ["No editing of existing text in place", "Digital signatures are never added or re-applied"]},
        {"id": "reader", "label": "Reader & Analysis", "available": True,
         "does": ["outline, structure checks, keywords, readability, duplicates", "extractive summary with source evidence",
                  "comparison of two documents (SLDC diff)", "checklist of required sections"],
         "limits": ["Deterministic analysis; agent-written summaries appear only when an agent is available and are labelled"]},
        {"id": "assistant", "label": "AI Assistant", "available": True,
         "does": ["answers about the open documents with citations", "runs read-only tools directly", "proposes edits and asks for confirmation"],
         "limits": ["Without a working SLAI language agent the assistant offers the same actions as buttons and says so"]},
    ]
    return {
        "app": {"name": "SLAI Documaster", "version": __version__},
        "tools": {"libreoffice": t.soffice, "tesseract": t.tesseract, "tesseractLanguages": t.tesseract_langs, "qpdf": t.qpdf,
                  "modules": t.modules},
        "read": reads, "write": writes, "conversions": conversions, "workspaces": workspaces,
        "agents": svc.agents.status(False), "reader": svc.reader.status(),
        "limits": svc.settings.public(),
    }


def has_all(svc: Services, *mods: str) -> bool:
    return all(svc.tools.has(m) for m in mods)
