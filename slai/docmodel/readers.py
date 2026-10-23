"""Read any supported file into the SLDC document model (the intermediate representation).

Which engine parses each format, and why:

====== ======================================================== =====================================================
format engine                                                   reason
====== ======================================================== =====================================================
sldc   ``src.utils.sldc.read_package``                          authoritative
pdf    pypdf, page by page                                      Reader's PDF parser returns one string (no pages, no
                                                                passwords); Documaster needs page-addressable text
docx   python-docx (Reader ``ParserEngine`` as fallback)        Reader keeps body text only; structure is needed
odt    Documaster (zipfile + XML)                               Reader has no ODT parser
html   Documaster (``html.parser``)                             Reader flattens HTML to text
md     Reader ``ParserEngine`` + SLDC markdown blocks           reuse
txt    Reader ``ParserEngine`` -> ``document_from_reader_result`` reuse
xml    Reader ``ParserEngine``                                  reuse
csv    Documaster (``csv``) -> table                            a table is the right shape
doc/rtf LibreOffice (``soffice``) -> DOCX, when installed       no Reader support; availability-gated
====== ======================================================== =====================================================
"""
from __future__ import annotations

import csv
import io
import re
import statistics
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from .. import textstats
from ..bridge import sldc
from ..errors import (DependencyMissing, FileRejected, LimitExceeded, PasswordRequired, UnsupportedOperation,
                      WrongPassword)
from ..files import sha256_file
from ..formats import FORMATS, format_for_ext, sniff
from ..services import NO_CONTROL, JobControl, Services
from . import ocr as ocrmod
from .ir import IRResult, build_package, clamp_title, import_provenance, parse_inline

_CJK = re.compile(r"[぀-ヿ㐀-鿿豈-﫿]")
# Only symbol bullets become list items. A leading "1." is ambiguous (list item or numbered heading), so numbered lines
# stay paragraphs with their number: Documaster does not invent structure the PDF does not state.
_BULLET = re.compile(r"^\s*[-•●○▪■◦*–—\uf000-\uf0ff]\s+(\S.*)$")
_SENT_END = ".!?:;。！？…"


def read_document(path: Path, fmt: str, svc: Services, *, password: str | None = None, name: str | None = None,
                  ocr: bool = False, ocr_lang: str = "eng", control: JobControl = NO_CONTROL,
                  title: str | None = None) -> IRResult:
    """Read ``path`` (already identified as ``fmt``) into the model. Raises typed errors; never returns a guess."""
    display = name or path.name
    sha = sha256_file(path)
    if fmt == "sldc":
        result = _read_sldc(path, password)
    elif fmt == "pdf":
        result = _read_pdf(path, svc, password, display, ocr, ocr_lang, control)
    elif fmt == "docx":
        result = _read_docx(path, svc, display)
    elif fmt == "odt":
        result = _read_odt(path, display)
    elif fmt in ("html",):
        result = _read_html(path, display)
    elif fmt == "md":
        result = _read_text_like(path, svc, display, markdown=True)
    elif fmt == "txt":
        result = _read_text_like(path, svc, display, markdown=False)
    elif fmt == "xml":
        result = _read_text_like(path, svc, display, markdown=False, xml=True)
    elif fmt == "csv":
        result = _read_csv(path, svc, display)
    elif fmt in ("doc", "rtf"):
        result = _read_via_soffice(path, fmt, svc, display, control)
    else:
        raise UnsupportedOperation(f"Documaster cannot read {FORMATS[fmt].label if fmt in FORMATS else fmt} files",
                                   details={"format": fmt})
    result.source_format = fmt
    pkg = result.package
    if title:
        pkg.document["title"] = clamp_title(title)
    if fmt != "sldc":
        pkg.document.setdefault("provenance", []).append(
            import_provenance(display, result.backend, sha, source_format=fmt, ocr_pages=result.ocr_pages))
    max_chars = svc.settings.max_text_chars
    if len(sldc().extract_text(pkg, validate=False)) > max_chars:
        raise LimitExceeded("This document has more text than the configured limit",
                            details={"limit_chars": max_chars})
    return result


# ------------------------------------------------------------------ SLDC
def _read_sldc(path: Path, password: str | None) -> IRResult:
    s = sldc()
    try:
        pkg = s.read_package(path, password=password)
    except s.SLDCDecryptionError as exc:
        if password:
            raise WrongPassword("The password does not unlock this SLDC document") from exc
        raise PasswordRequired("This SLDC document is encrypted; a password is required") from exc
    except s.SLDCDependencyError as exc:
        raise DependencyMissing("Reading encrypted SLDC needs the 'cryptography' package") from exc
    except s.SLDCError as exc:
        raise FileRejected(f"This is not a valid SLDC document: {exc}") from exc
    if pkg.version == s.LEGACY_VERSION:
        pkg, report = s.migrate_package(pkg)
        note = ["Legacy SLDC 0.1.0 was migrated to 1.0.0 in memory (the file is not changed)"]
    else:
        note = []
    return IRResult(pkg, "SLDC (src.utils.sldc)", warnings=note,
                    features=["blocks", "styles", "sections", "tables", "figures", "references", "comments",
                              "revisions"])


# ------------------------------------------------------------------ PDF
def line_width_reference(page_texts: list[str]) -> int:
    """Typical full-line length of the document: the 90th percentile of its line lengths (at least 30)."""
    lengths = sorted(len(ln.strip()) for t in page_texts for ln in t.replace("\r", "").split("\n") if ln.strip())
    if not lengths:
        return 30
    return max(30, lengths[int(0.9 * (len(lengths) - 1))])


def reflow(text: str, reference: int | None = None) -> list[str]:
    """Group the visual lines pypdf returns into paragraphs.

    A line ends a paragraph when it is clearly short (a wrapped line runs close to the page's text width), when it ends a
    sentence and the next line starts a capital, or when the next line starts a list item. Words are never re-spelled:
    line-end hyphens are kept.
    """
    lines = [ln.rstrip() for ln in text.replace("\r", "").split("\n")]
    if not any(ln.strip() for ln in lines):
        return []
    if reference is None:
        reference = line_width_reference([text])
    paragraphs: list[list[str]] = []
    current: list[str] = []

    def flush() -> None:
        nonlocal current
        if current:
            paragraphs.append(current)
        current = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        if current:
            previous = current[-1]
            short = len(previous) < 0.62 * reference
            bullet = bool(_BULLET.match(stripped))
            sentence_break = previous[-1:] in _SENT_END and stripped[:1].isupper() and len(previous) < 0.93 * reference
            if short or bullet or sentence_break:
                flush()
        current.append(stripped)
    flush()
    return [_join_lines(p) for p in paragraphs]


def _join_lines(lines: list[str]) -> str:
    out = lines[0]
    for nxt in lines[1:]:
        if _CJK.search(out[-1:]) and _CJK.search(nxt[:1]):
            out += nxt
        else:
            out += " " + nxt
    return out


def _paragraph_block(text: str, source_ref: str, first_on_page: bool) -> dict[str, Any]:
    match = _BULLET.match(text)
    attrs: dict[str, Any] = {}
    kind = "paragraph"
    if match and len(text) < 600:
        kind = "list_item"
        attrs["ordered"] = False
        text = match.group(1)
    if first_on_page:
        attrs["page_break_before"] = True
    return sldc().make_block(kind, text, source_ref=source_ref, attributes=attrs or None)


def _read_pdf(path: Path, svc: Services, password: str | None, name: str, ocr: bool, ocr_lang: str,
              control: JobControl) -> IRResult:
    try:
        import pypdf
        from pypdf.errors import PdfReadError, DependencyError  # type: ignore
    except ImportError as exc:
        raise DependencyMissing("pypdf is not installed", details={"package": "pypdf"}) from exc
    try:
        reader = pypdf.PdfReader(str(path), strict=False)
    except Exception as exc:  # noqa: BLE001
        raise FileRejected(f"This PDF could not be opened: {exc}") from exc
    if reader.is_encrypted:
        if not password:
            try:
                if reader.decrypt("") == 0:
                    raise PasswordRequired("This PDF is password-protected")
            except PasswordRequired:
                raise
            except DependencyError as exc:
                raise DependencyMissing("Encrypted PDFs need the 'cryptography' package") from exc
        else:
            try:
                ok = reader.decrypt(password)
            except DependencyError as exc:
                raise DependencyMissing("Encrypted PDFs need the 'cryptography' package") from exc
            if ok == 0:
                raise WrongPassword("The password does not open this PDF")
    try:
        count = len(reader.pages)
    except Exception as exc:  # noqa: BLE001
        raise FileRejected(f"This PDF's page tree is damaged: {exc}") from exc
    if count > svc.settings.max_pdf_pages:
        raise LimitExceeded(f"This PDF has {count} pages; the limit is {svc.settings.max_pdf_pages}",
                            details={"pages": count})
    do_ocr = ocr and ocrmod.available(svc.tools)
    lang = ocrmod.check_language(svc.tools, ocr_lang) if do_ocr else ocr_lang
    warnings: list[str] = []
    if ocr and not do_ocr:
        warnings.append("OCR was requested but is not available: " + ocrmod.requirement_message())
    blocks: list[dict[str, Any]] = []
    empty_pages: list[int] = []
    ocr_pages: list[int] = []
    failed: list[int] = []
    page_texts: list[str] = []
    for index in range(count):
        control.report(index / max(count, 1) * 0.8, f"Reading page {index + 1} of {count}")
        try:
            text = reader.pages[index].extract_text() or ""
        except Exception:  # noqa: BLE001 - one bad page must not lose the rest
            text, failed = "", failed + [index + 1]
        if do_ocr and not text.strip():
            try:
                text = ocrmod.recognise_pdf_page(svc.tools, path, index, password, lang)
                if text.strip():
                    ocr_pages.append(index + 1)
            except DependencyMissing as exc:
                warnings.append(f"Page {index + 1}: OCR failed ({exc.message})")
        page_texts.append(text)
    reference = line_width_reference(page_texts)
    for index, text in enumerate(page_texts):
        control.report(0.8 + index / max(count, 1) * 0.2, f"Structuring page {index + 1} of {count}")
        paragraphs = reflow(text, reference)
        if not paragraphs:
            empty_pages.append(index + 1)
            if index > 0 or True:
                # keep the page boundary so page numbers stay aligned with the source
                blocks.append(sldc().make_block("paragraph", "", source_ref=f"{name}#p{index + 1}",
                                                attributes={"page_break_before": True} if index else None))
            continue
        for k, para in enumerate(paragraphs):
            blocks.append(_paragraph_block(para, f"{name}#p{index + 1}", first_on_page=(k == 0 and index > 0)))
    # an empty block would be an empty paragraph in output files; keep them only as page anchors
    if empty_pages:
        shown = ", ".join(map(str, empty_pages[:12])) + ("…" if len(empty_pages) > 12 else "")
        hint = "" if do_ocr else " (scanned pages? OCR is " + ("available: enable it to read them)" if ocrmod.available(svc.tools) else "not available on this machine)")
        warnings.append(f"{len(empty_pages)} page(s) have no extractable text: {shown}{hint}")
    if failed:
        warnings.append(f"Text extraction failed on page(s) {', '.join(map(str, failed[:12]))}")
    if ocr_pages:
        warnings.append(f"{len(ocr_pages)} page(s) were recognised by OCR (a machine reading of the page image; it can "
                        "contain errors)")
    meta = reader.metadata
    title = str(getattr(meta, "title", "") or "").strip() if meta else ""
    author = str(getattr(meta, "author", "") or "").strip() if meta else ""
    sample = " ".join(b["text"] for b in blocks[:200])
    lang_guess = textstats.detect_language(sample)
    pub = {"authors": [author[:200]]} if author else None
    pkg = build_package(title or Path(name).stem, [b for b in blocks], language=textstats.sldc_language(lang_guess["code"]),
                        publication=pub)
    # blocks with empty text are page anchors: SLDC accepts empty text, extract_text yields blank lines for them
    return IRResult(pkg, "pypdf" + (" + Tesseract OCR" if ocr_pages else ""), warnings=warnings,
                    features=["text", "page boundaries", "document title/author"],
                    dropped=["images", "tables as tables", "fonts, colours and layout", "annotations", "form fields",
                             "bookmarks"], pages=count, ocr_pages=ocr_pages)


# ------------------------------------------------------------------ DOCX / ODT / HTML
def _read_docx(path: Path, svc: Services, name: str) -> IRResult:
    from .read_docx import read_docx
    if svc.tools.has("docx"):
        data = read_docx(str(path))
        pkg = build_package(data["title"] or Path(name).stem, data["blocks"], assets=data["assets"],
                            publication={"authors": [data["author"][:200]]} if data["author"] else None)
        _fix_language(pkg)
        return IRResult(pkg, "python-docx", warnings=data["warnings"],
                        features=["headings", "paragraphs", "lists", "tables incl. merged cells", "bold/italic/underline/strike",
                                  "hyperlinks", "page breaks", "inline images"],
                        dropped=data["dropped"] + ["fonts, sizes and colours", "page setup", "text boxes and shapes"])
    # fallback: Reader's DOCX text (no structure)
    return _read_text_like(path, svc, name, markdown=False,
                           extra_warning="python-docx is not installed, so only the body text was read "
                                         "(headings, tables and images need python-docx)")


def _read_odt(path: Path, name: str) -> IRResult:
    from .read_odt import read_odt
    data = read_odt(str(path))
    pkg = build_package(data["title"] or Path(name).stem, data["blocks"], assets=data["assets"],
                        publication={"authors": [data["author"][:200]]} if data["author"] else None)
    _fix_language(pkg)
    return IRResult(pkg, "OpenDocument XML reader (Documaster)", warnings=data["warnings"],
                    features=["headings", "paragraphs", "lists", "tables incl. merged cells", "bold/italic/underline/strike",
                              "hyperlinks", "page breaks", "inline images"],
                    dropped=data["dropped"] + ["fonts, sizes and colours", "page setup"])


def _read_html(path: Path, name: str) -> IRResult:
    from .read_html import read_html_bytes
    data = read_html_bytes(path.read_bytes())
    pkg = build_package(data["title"] or Path(name).stem, data["blocks"], assets=data["assets"])
    _fix_language(pkg, hint=data.get("lang"))
    return IRResult(pkg, "HTML reader (Documaster)", warnings=data["warnings"],
                    features=["headings", "paragraphs", "lists", "tables", "inline formatting", "links",
                              "embedded PNG/JPEG images"],
                    dropped=["CSS styling", "scripts", "remote images and resources", "forms"])


def _fix_language(pkg: Any, hint: str | None = None) -> None:
    text = " ".join(b["text"] for b in pkg.document["blocks"][:200] if b.get("text"))
    if hint and re.fullmatch(r"[A-Za-z]{2,3}", hint):
        pkg.document["language"] = hint.lower()
        return
    guess = textstats.detect_language(text)
    pkg.document["language"] = textstats.sldc_language(guess["code"])


# ------------------------------------------------------------------ text-like via Reader
def _read_text_like(path: Path, svc: Services, name: str, *, markdown: bool, xml: bool = False,
                    extra_warning: str | None = None) -> IRResult:
    result = svc.reader.parse(path)
    pkg_doc = sldc().document_from_reader_result(result, title=Path(name).stem, markdown=markdown)
    blocks = pkg_doc["blocks"]
    warnings = list(result.get("warnings") or [])
    if extra_warning:
        warnings.append(extra_warning)
    features = ["text"]
    if markdown:
        blocks = _markdown_enrich(blocks)
        features = ["headings", "lists", "quotes", "code blocks", "tables", "bold/italic/code/strike", "links"]
    pkg = build_package(pkg_doc["title"], blocks, provenance=pkg_doc.get("provenance"))
    # keep the document id SLDC derived so identical input gives identical documents
    pkg.document["id"] = pkg_doc["id"]
    _fix_language(pkg)
    return IRResult(pkg, f"SLAI Reader ({result.get('parser', 'ParserEngine')})", warnings=warnings, features=features,
                    dropped=["layout"] if not markdown else ["HTML inside Markdown", "images (not fetched)"])


_PIPE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_PIPE_SEP = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)+\|?\s*$")


def _markdown_enrich(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in blocks:
        kind = block["type"]
        text = block["text"]
        if kind == "paragraph" and "\n" in text:
            lines = text.split("\n")
            if len(lines) >= 2 and all(_PIPE_ROW.match(ln) for ln in lines) and _PIPE_SEP.match(lines[1]):
                table = _pipe_table(lines)
                if table is not None:
                    out.append(sldc().make_block("table", "", block_id=block["id"], source_ref=block.get("source_ref"),
                                                 table=table))
                    continue
        if kind in ("paragraph", "heading", "list_item", "quote"):
            plain, spans = parse_inline(text)
            if plain != text or spans:
                block = dict(block)
                block["text"] = plain
                if spans:
                    block["spans"] = spans
        out.append(block)
    return out


def _pipe_table(lines: list[str]) -> dict[str, Any] | None:
    def cells(line: str) -> list[str]:
        body = line.strip()
        body = body[1:] if body.startswith("|") else body
        body = body[:-1] if body.endswith("|") else body
        return [c.strip() for c in re.split(r"(?<!\\)\|", body)]
    rows = [cells(lines[0])] + [cells(ln) for ln in lines[2:]]
    width = len(rows[0])
    if width < 1 or width > 256:
        return None
    norm = [[{"text": parse_inline(c.replace("\\|", "|"))[0]} for c in (r + [""] * width)[:width]] for r in rows]
    return {"columns": width, "header_rows": 1, "rows": norm}


# ------------------------------------------------------------------ CSV
def _read_csv(path: Path, svc: Services, name: str) -> IRResult:
    result = svc.reader.parse(path)
    text = result.get("content") or ""
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    if not rows:
        raise FileRejected("This CSV file has no rows")
    if len(rows) > 10_000:
        raise LimitExceeded("A table in a document can hold 10,000 rows; this CSV has more", details={"rows": len(rows)})
    width = max(len(r) for r in rows)
    if width > 256:
        raise LimitExceeded("A table in a document can hold 256 columns; this CSV has more")
    table = sldc().make_table([[c for c in (r + [""] * width)[:width]] for r in rows], header_rows=1)
    pkg = build_package(Path(name).stem, [table], provenance=None)
    return IRResult(pkg, f"SLAI Reader ({result.get('parser', 'ParserEngine')}) + csv", features=["table"],
                    warnings=["The first row is treated as the table header"], dropped=["data types and formulas"])


# ------------------------------------------------------------------ DOC / RTF through LibreOffice
def _read_via_soffice(path: Path, fmt: str, svc: Services, name: str, control: JobControl) -> IRResult:
    if not svc.tools.soffice:
        raise DependencyMissing(
            f"Reading {FORMATS[fmt].label} needs LibreOffice, which is not installed. SLAI's Reader cannot read it.",
            details={"needs": "LibreOffice (soffice)", "format": fmt})
    with tempfile.TemporaryDirectory(prefix="dm-soffice-") as tmp:
        out = soffice_convert(svc, path, "docx", Path(tmp), control)
        inner = _read_docx(out, svc, name)
    inner.backend = "LibreOffice → DOCX → " + inner.backend
    inner.warnings.append(f"{FORMATS[fmt].label} was first converted to DOCX by LibreOffice, then read")
    return inner


def soffice_convert(svc: Services, source: Path, target_ext: str, out_dir: Path, control: JobControl = NO_CONTROL,
                    filter_name: str | None = None) -> Path:
    """Run LibreOffice headless in an isolated profile; returns the produced file or raises."""
    if not svc.tools.soffice:
        raise DependencyMissing("LibreOffice (soffice) is not installed")
    control.check()
    profile = Path(tempfile.mkdtemp(prefix="dm-lo-profile-"))
    arg = target_ext if not filter_name else f"{target_ext}:{filter_name}"
    cmd = [svc.tools.soffice, f"-env:UserInstallation={profile.as_uri()}", "--headless", "--norestore",
           "--convert-to", arg, "--outdir", str(out_dir), str(source)]
    try:
        run = subprocess.run(cmd, capture_output=True, timeout=svc.settings.subprocess_timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise FileRejected("LibreOffice took too long to convert this file") from exc
    finally:
        import shutil
        shutil.rmtree(profile, ignore_errors=True)
    produced = out_dir / (source.stem + "." + target_ext.split(":")[0])
    if run.returncode != 0 or not produced.exists() or produced.stat().st_size == 0:
        raise FileRejected("LibreOffice could not convert this file: " + run.stderr.decode("utf-8", "replace")[:200])
    return produced


def reading_options(fmt: str, svc: Services) -> dict[str, Any]:
    """What can be said about reading ``fmt`` on this machine (used by capabilities)."""
    return {"format": fmt}
