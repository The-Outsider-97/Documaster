"""PDF editing operations. Each one reads the document's current revision and produces the next revision.

``apply(ws, doc, op, control)`` validates the request, writes the new file to a temporary revision path, verifies what the
operation promises, and only then adds the revision. If anything fails the previous revision stays current and no file remains.
Encrypted documents stay encrypted with the same password.
"""
from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..errors import (DocumasterError, DependencyMissing, OutputFailed, PermissionDenied, UnsupportedOperation,
                      ValidationFailed)
from ..services import NO_CONTROL, JobControl, Services
from . import annotate, docinfo, forms, redact, stamp
from .geometry import clamp_rect, geometry_of, rect_to_user
from .organize import PdfSource, PlanItem, assemble, parse_plan, verify_assembly
from .pdfio import norm_text, open_reader, parse_ranges, save_writer
from .render import open_pdfium
from ..pdfium_lock import pdfium_locked


@dataclass
class OpResult:
    label: str
    changed: bool = True
    details: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    verification: dict[str, Any] | None = None


OPS: dict[str, tuple[Callable[..., OpResult], str | None]] = {}


def op(name: str, needs: str | None = None) -> Callable[[Callable[..., OpResult]], Callable[..., OpResult]]:
    def deco(fn: Callable[..., OpResult]) -> Callable[..., OpResult]:
        OPS[name] = (fn, needs)
        return fn
    return deco


@dataclass
class Ctx:
    svc: Services
    ws: Any
    doc: Any
    out: Path
    control: JobControl
    out_pw: str | None = field(default=None)
    _set: bool = field(default=False)

    @property
    def opw(self) -> str | None:
        """Password the output is written with (the document's, unless the password operation changes it)."""
        return self.out_pw if self._set else self.doc.password

    def set_out_password(self, value: str | None) -> None:
        self.out_pw, self._set = value, True

    @property
    def src(self) -> Path:
        return self.doc.path

    @property
    def password(self) -> str | None:
        return self.doc.password

    def reader(self) -> Any:
        return open_reader(self.src, self.password)

    def writer(self) -> Any:
        import pypdf
        return pypdf.PdfWriter(clone_from=self.reader())

    def save(self, writer: Any) -> None:
        save_writer(writer, self.out, password=self.opw)

    def pages(self, spec: Any) -> list[int]:
        return stamp.resolve_pages(spec, self.doc.pages or len(self.reader().pages))


def _require(doc: Any, need: str | None) -> None:
    if need and doc.encrypted and not doc.owner and doc.permissions and not doc.permissions.get(need, True):
        raise PermissionDenied(
            f"This PDF's permissions do not allow this operation ({need}). Open it with the owner password to continue.",
            details={"permission": need})


def apply(svc: Services, ws: Any, doc: Any, spec: dict[str, Any], control: JobControl = NO_CONTROL) -> dict[str, Any]:
    if doc.format != "pdf":
        raise UnsupportedOperation("The PDF editor works on PDF documents")
    ws.require_open(doc)
    name = spec.get("op")
    if name not in OPS:
        raise ValidationFailed(f"Unknown operation {name!r}", details={"operations": sorted(OPS)})
    fn, need = OPS[name]
    _require(doc, need)
    holder: dict[str, OpResult] = {}

    def produce(path: Path) -> None:
        ctx = Ctx(svc, ws, doc, path, control)
        holder['ctx'] = ctx  # type: ignore[assignment]
        res = fn(ctx, spec)
        holder["r"] = res
        if not res.changed:
            raise _NoChange
        _verify_basic(ctx, res)

    try:
        rev = ws.add_revision(doc, produce, name)
    except _NoChange:
        res = holder["r"]
        return {"changed": False, "label": res.label, "details": res.details, "warnings": res.warnings,
                "document": doc.public()}
    res = holder["r"]
    rev.label = res.label
    if name == "password":
        new = spec.get("new") or None
        doc.password, doc.encrypted, doc.owner = new, bool(new), True
    ws._inspect_pages(doc)
    doc.info = _info(doc)
    return {"changed": True, "label": res.label, "details": res.details, "warnings": res.warnings,
            "verification": res.verification, "document": doc.public()}


class _NoChange(Exception):
    pass


def _info(doc: Any) -> dict[str, Any]:
    try:
        meta = open_reader(doc.path, doc.password).metadata
        return {"title": str(getattr(meta, "title", "") or "") if meta else "",
                "author": str(getattr(meta, "author", "") or "") if meta else ""}
    except DocumasterError:
        return doc.info


@pdfium_locked
def _verify_basic(ctx: Ctx, res: OpResult) -> None:
    """Every revision must reopen with an independent parser (pypdf and pdfium) and render its first page."""
    reader = open_reader(ctx.out, ctx.opw)
    n = len(reader.pages)
    if n < 1:
        raise OutputFailed("The edited PDF has no pages and was discarded")
    pdoc = open_pdfium(ctx.out, ctx.opw)
    try:
        if len(pdoc) != n:
            raise OutputFailed("The edited PDF is inconsistent (the two parsers disagree on the page count) and was discarded")
        page = pdoc[0]
        try:
            page.render(scale=0.2)
        finally:
            page.close()
    finally:
        pdoc.close()
    res.details.setdefault("pages", n)


# ----------------------------------------------------------------------------------------------- pages
@op("rotate", "assemble")
def _rotate(ctx: Ctx, spec: dict[str, Any]) -> OpResult:
    degrees = int(spec.get("degrees", 90)) % 360
    if degrees % 90 or degrees == 0:
        raise ValidationFailed("Rotate by 90, 180 or 270 degrees")
    pages = ctx.pages(spec.get("pages"))
    w = ctx.writer()
    before = [int(w.pages[i].get("/Rotate", 0) or 0) for i in pages]
    for i in pages:
        w.pages[i].rotate(degrees)
    ctx.save(w)
    after = open_reader(ctx.out, ctx.opw)
    ok = all((b + degrees) % 360 == int(after.pages[i].get("/Rotate", 0) or 0) % 360 for b, i in zip(before, pages))
    if not ok:
        raise OutputFailed("The rotation could not be verified and was discarded")
    return OpResult(f"Rotated {len(pages)} page(s) by {degrees}°", details={"pages": len(after.pages), "rotated": [i + 1 for i in pages]},
                    verification={"ok": True, "checks": [{"name": "Rotation", "ok": True, "detail": f"{len(pages)} page(s) verified"}]})


def _rebuild(ctx: Ctx, items: list[PlanItem], sources: list[PdfSource], label: str, **kw: Any) -> OpResult:
    info = assemble(sources, items, ctx.out, password=ctx.opw, **kw)
    ver = verify_assembly(ctx.out, sources, items, ctx.opw)
    if not ver["ok"]:
        raise OutputFailed("The rearranged PDF failed verification and was discarded: "
                           + "; ".join(c["detail"] for c in ver["checks"] if not c["ok"]))
    return OpResult(label, details={"pages": info["pages"], "bookmarks": info["bookmarks"]}, warnings=info["warnings"],
                    verification=ver)


@op("delete", "assemble")
def _delete(ctx: Ctx, spec: dict[str, Any]) -> OpResult:
    total = ctx.doc.pages
    gone = set(ctx.pages(spec.get("pages")))
    keep = [i for i in range(total) if i not in gone]
    if not keep:
        raise ValidationFailed("That would delete every page")
    src = PdfSource(ctx.src, ctx.doc.name, ctx.password)
    return _rebuild(ctx, [PlanItem(0, i) for i in keep], [src], f"Deleted {len(gone)} page(s)")


@op("arrange", "assemble")
def _arrange(ctx: Ctx, spec: dict[str, Any]) -> OpResult:
    """Reorder, rotate, duplicate, delete and insert blank pages in one step. ``plan`` entries: {page, rotation?, included?} or {blank}."""
    entries = spec.get("plan")
    if not isinstance(entries, list):
        raise ValidationFailed("arrange needs a 'plan' list")
    norm = []
    for e in entries:
        if isinstance(e, dict) and not e.get("blank"):
            e = {**e, "doc": 0}
        norm.append(e)
    items = parse_plan(norm, [ctx.doc.pages], ctx.svc.settings.max_pdf_pages)
    src = PdfSource(ctx.src, ctx.doc.name, ctx.password)
    return _rebuild(ctx, items, [src], "Rearranged pages")


@op("insert", "assemble")
def _insert(ctx: Ctx, spec: dict[str, Any]) -> OpResult:
    """Insert pages from another open PDF (``from``: its id, ``pages``: range string or all) after page ``after`` (0 = start)."""
    other = ctx.ws.get(str(spec.get("from", "")))
    ctx.ws.require_open(other)
    if other.format != "pdf":
        raise ValidationFailed("Pages can only be inserted from a PDF")
    chosen = parse_ranges(spec["pages"], other.pages) if spec.get("pages") else list(range(other.pages))
    after = int(spec.get("after", ctx.doc.pages))
    if not 0 <= after <= ctx.doc.pages:
        raise ValidationFailed("'after' must be between 0 and the page count")
    items = [PlanItem(0, i) for i in range(after)] + [PlanItem(1, i) for i in chosen] + \
            [PlanItem(0, i) for i in range(after, ctx.doc.pages)]
    if len(items) > ctx.svc.settings.max_pdf_pages:
        raise ValidationFailed("The result would exceed the page limit")
    sources = [PdfSource(ctx.src, ctx.doc.name, ctx.password), PdfSource(other.path, other.name, other.password)]
    return _rebuild(ctx, items, sources, f"Inserted {len(chosen)} page(s) from {other.name}")


@op("blank", "assemble")
def _blank(ctx: Ctx, spec: dict[str, Any]) -> OpResult:
    after = int(spec.get("after", ctx.doc.pages))
    count = int(spec.get("count", 1))
    if not 0 <= after <= ctx.doc.pages or not 1 <= count <= 100:
        raise ValidationFailed("'after' must be within the document and 'count' between 1 and 100")
    g = geometry_of(ctx.reader().pages[min(max(after - 1, 0), ctx.doc.pages - 1)])
    w, h = g.display_size
    items = [PlanItem(0, i) for i in range(after)] + [PlanItem(0, 0, 0, True, w, h) for _ in range(count)] + \
            [PlanItem(0, i) for i in range(after, ctx.doc.pages)]
    return _rebuild(ctx, items, [PdfSource(ctx.src, ctx.doc.name, ctx.password)], f"Inserted {count} blank page(s)")


@op("crop", "assemble")
def _crop(ctx: Ctx, spec: dict[str, Any]) -> OpResult:
    try:
        rect = clamp_rect(spec["rect"])
    except (KeyError, ValueError) as exc:
        raise ValidationFailed(f"crop needs a rectangle: {exc}") from exc
    from pypdf.generic import RectangleObject
    w = ctx.writer()
    pages = ctx.pages(spec.get("pages"))
    for i in pages:
        page = w.pages[i]
        x0, y0, x1, y1 = rect_to_user(rect, geometry_of(page))
        box = RectangleObject([x0, y0, x1, y1])
        page.cropbox = box
        page.trimbox = box
    ctx.save(w)
    return OpResult(f"Cropped {len(pages)} page(s)", warnings=["Cropping hides the outer area but does not delete it from the file. "
                                                              "Use Redact to remove content."])


# ----------------------------------------------------------------------------------------------- annotate / stamp
@op("annotate", "annotate")
def _annotate(ctx: Ctx, spec: dict[str, Any]) -> OpResult:
    items = spec.get("annotations")
    if not isinstance(items, list) or not items or len(items) > 2000:
        raise ValidationFailed("Give between 1 and 2000 annotations")
    w = ctx.writer()
    total = len(w.pages)
    author = str(spec.get("author") or "Documaster")[:100]
    ids: list[str] = []
    for a in items:
        ids += annotate.add(w, a, total, author)
    ctx.save(w)
    got = annotate.listing(open_reader(ctx.out, ctx.opw))
    present = {g["id"] for g in got}
    if not set(ids) <= present:
        raise OutputFailed("The annotations could not be verified in the saved file and were discarded")
    return OpResult(f"Added {len(ids)} annotation(s)", details={"ids": ids},
                    verification={"ok": True, "checks": [{"name": "Annotations present", "ok": True, "detail": f"{len(ids)} found after saving"}]})


@op("remove_annotations", "annotate")
def _remove_annotations(ctx: Ctx, spec: dict[str, Any]) -> OpResult:
    sel = spec.get("selectors")
    if not isinstance(sel, list) or not sel:
        raise ValidationFailed("Say which annotations to remove")
    w = ctx.writer()
    n = annotate.remove(w, sel)
    if n == 0:
        return OpResult("No matching annotations", changed=False)
    ctx.save(w)
    return OpResult(f"Removed {n} annotation(s)", details={"removed": n})


@op("edit_note", "annotate")
def _edit_note(ctx: Ctx, spec: dict[str, Any]) -> OpResult:
    selector = spec.get("selector")
    if not isinstance(selector, dict):
        raise ValidationFailed("Choose the comment to edit")
    text = spec.get("text")
    if not isinstance(text, str):
        raise ValidationFailed("Enter the replacement comment text")
    w = ctx.writer()
    if not annotate.edit_note(w, selector, text):
        return OpResult("Comment unchanged", changed=False)
    ctx.save(w)
    notes = annotate.listing(open_reader(ctx.out, ctx.opw))
    matching = [a for a in notes if (a["id"] == selector.get("id") if selector.get("id") else
                                      a["page"] == selector.get("page") and a["index"] == selector.get("index"))]
    if len(matching) != 1 or matching[0]["text"] != text:
        raise OutputFailed("The updated comment could not be verified and was discarded")
    return OpResult("Edited comment", verification={"ok": True, "checks": [
        {"name": "Comment text preserved", "ok": True, "detail": "Reopened and checked the saved annotation"}]})


@op("stamp", "modify")
def _stamp(ctx: Ctx, spec: dict[str, Any]) -> OpResult:
    kind = spec.get("kind")
    w = ctx.writer()
    total = len(w.pages)
    if kind == "text":
        n = stamp.text_stamp(w, spec, total)
    elif kind == "watermark":
        n = stamp.watermark(w, spec, total)
    elif kind == "page_numbers":
        n = stamp.page_numbers(w, spec, total)
    elif kind == "textbox":
        n = stamp.text_box(w, spec, total)
    elif kind == "image":
        asset = ctx.ws.asset(str(spec.get("asset", "")))
        n = stamp.image_stamp(w, spec, asset.path, total)
    else:
        raise ValidationFailed("stamp kind must be text, watermark, page_numbers, textbox or image")
    ctx.save(w)
    return OpResult(f"Added {kind.replace('_', ' ')} to {n} page(s)", details={"pagesChanged": n},
                    warnings=["Stamped content is part of the page and cannot be edited afterwards; use Undo to remove it."])


# ----------------------------------------------------------------------------------------------- forms / info
@op("fill_form", "fillForms")
def _fill_form(ctx: Ctx, spec: dict[str, Any]) -> OpResult:
    w = ctx.writer()
    res = forms.fill(w, spec.get("values") or {})
    if not spec.get("flatten"):
        ctx.save(w)
        return OpResult(f"Filled {res['filled']} field(s)", details=res)
    tmp = ctx.out.with_suffix(".fill.pdf")
    save_writer(w, tmp, password=None)
    flat = ctx.out.with_suffix(".flat.pdf")
    try:
        forms.flatten_file(tmp, flat, None, forms_only=True)
        import pypdf
        wr = pypdf.PdfWriter(clone_from=pypdf.PdfReader(str(flat)))
        save_writer(wr, ctx.out, password=ctx.opw)
    finally:
        tmp.unlink(missing_ok=True)
        flat.unlink(missing_ok=True)
    return OpResult(f"Filled and flattened {res['filled']} field(s)", details=res)


@op("flatten", "modify")
def _flatten(ctx: Ctx, spec: dict[str, Any]) -> OpResult:
    tmp = ctx.out.with_suffix(".src.pdf")
    flat = ctx.out.with_suffix(".flat.pdf")
    try:
        w = ctx.writer()
        save_writer(w, tmp, password=None)
        forms.flatten_file(tmp, flat, None)
        import pypdf
        save_writer(pypdf.PdfWriter(clone_from=pypdf.PdfReader(str(flat))), ctx.out, password=ctx.opw)
    finally:
        tmp.unlink(missing_ok=True)
        flat.unlink(missing_ok=True)
    return OpResult("Flattened form fields and annotations into the pages")


@op("metadata", "modify")
def _metadata(ctx: Ctx, spec: dict[str, Any]) -> OpResult:
    w = ctx.writer()
    changed = docinfo.set_metadata(w, spec.get("values") or {})
    ctx.save(w)
    return OpResult("Updated document properties: " + ", ".join(changed), details={"changed": changed})


@op("bookmarks", "modify")
def _bookmarks(ctx: Ctx, spec: dict[str, Any]) -> OpResult:
    entries = spec.get("entries")
    if not isinstance(entries, list):
        raise ValidationFailed("bookmarks needs an 'entries' list")
    w = ctx.writer()
    n = docinfo.set_outline(w, entries, len(w.pages))
    ctx.save(w)
    return OpResult(f"Set {n} bookmark(s)", details={"bookmarks": n})


@op("sanitize", "modify")
def _sanitize(ctx: Ctx, spec: dict[str, Any]) -> OpResult:
    w = ctx.writer()
    removed = docinfo.sanitize(w)
    ctx.save(w)
    return OpResult("Removed hidden data (metadata, attachments, scripts, actions)", details=removed)


@op("password")
def _password(ctx: Ctx, spec: dict[str, Any]) -> OpResult:
    """Set (``new``) or remove (``new`` empty) the password. The document stays open with the new password."""
    new = spec.get("new") or None
    if new is not None and (not isinstance(new, str) or len(new) < 4 or len(new) > 128):
        raise ValidationFailed("A password must be 4 to 128 characters")
    w = ctx.writer()
    ctx.set_out_password(new)
    save_writer(w, ctx.out, password=new)
    return OpResult("Password " + ("set" if new else "removed"), details={"encrypted": bool(new)})


# ----------------------------------------------------------------------------------------------- redaction
def _wholly_outside_page(rect: Any) -> bool:
    """A well-formed rectangle (positive area) that lies entirely outside the page, as opposed to a malformed or empty one."""
    try:
        l, t, r, b = (float(v) for v in rect)
    except (TypeError, ValueError):
        return False
    l, r, t, b = min(l, r), max(l, r), min(t, b), max(t, b)
    if len(rect) != 4 or not (r - l > 1e-4 and b - t > 1e-4):
        return False
    return r <= 0 or l >= 1 or b <= 0 or t >= 1


@op("redact", "modify")
def _redact(ctx: Ctx, spec: dict[str, Any]) -> OpResult:
    regions: list[dict[str, Any]] = []
    off_page: set[int] = set()
    for r in spec.get("regions") or []:
        try:
            page, rect = int(r["page"]), r["rect"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationFailed("A redaction area needs a page and a rectangle") from exc
        try:
            clamp_rect(rect)
        except ValueError:
            if _wholly_outside_page(rect):
                off_page.add(page)     # e.g. text that runs past the page edge: rasterising the page removes it
                continue
        regions.append({"page": page, "rect": rect})
    q = spec.get("find")
    if q:
        hits = redact.find_text(ctx.src, ctx.password, str(q.get("query", "")), regex=bool(q.get("regex")),
                                case=bool(q.get("case")), whole_word=bool(q.get("wholeWord")))
        allowed = set(q["pages"]) if q.get("pages") else None
        for h in hits:
            if allowed is not None and h["page"] not in allowed:
                continue
            for rect in h["rects"]:
                try:
                    clamp_rect(rect)
                except ValueError:
                    off_page.add(h["page"])      # the match lies outside the visible page: rasterising the page removes it
                else:
                    regions.append({"page": h["page"], "rect": rect})
        if not regions and not off_page:
            return OpResult("The text was not found, nothing was redacted", changed=False, details={"matches": 0})
    report = redact.redact(ctx.src, ctx.out, regions, password=ctx.password, dpi=int(spec.get("dpi", 200)),
                           sanitize=bool(spec.get("sanitize", True)), out_password=ctx.opw,
                           lossless=bool(spec.get("lossless", False)), also_pages=sorted(off_page))
    warnings = list(report["limits"])
    if off_page:
        warnings.append(f"Some matches on page(s) {', '.join(str(p + 1) for p in sorted(off_page))} lie outside the visible page area; "
                        "they were removed together with the rest of the page content")
    if report["sameTextOnOtherPages"]:
        pages = ", ".join(str(x["page"]) for x in report["sameTextOnOtherPages"][:12])
        warnings.append(f"The same wording still appears on pages that were not redacted: {pages}")
    return OpResult(f"Redacted {report['regions']} area(s) on {len(report['pagesFlattened'])} page(s)", details=report,
                    warnings=warnings, verification=report["verification"])


# ----------------------------------------------------------------------------------------------- compression / OCR
@op("compress")
def _compress(ctx: Ctx, spec: dict[str, Any]) -> OpResult:
    before = ctx.src.stat().st_size
    work = Path(tempfile.mkdtemp(prefix="dm-cmp-", dir=ctx.out.parent))
    try:
        src = ctx.src
        if ctx.doc.encrypted:
            src = work / "plain.pdf"
            save_writer(ctx.writer(), src, password=None)
        backend = "SLAI Reader CompressionEngine (lossless)"
        report: dict[str, Any] | None = None
        candidate: Path | None = None
        try:
            report = ctx.svc.reader.optimize_pdf(src, work / "opt")
            if report.get("outcome") == "optimized" and report.get("output_path"):
                candidate = Path(report["output_path"])
        except DocumasterError as exc:
            ctx.control.report(0.5, "Reader unavailable, using the built-in lossless optimiser")
            backend = f"pypdf lossless ({type(exc).__name__}: Reader engine not available)"
        if candidate is None and report is None:
            import pypdf
            w = pypdf.PdfWriter(clone_from=open_reader(src, None))
            for page in w.pages:
                page.compress_content_streams()
            w.compress_identical_objects(remove_identicals=True, remove_orphans=True)
            candidate = work / "pypdf.pdf"
            save_writer(w, candidate)
        if candidate is None:
            return OpResult("The PDF is already well compressed; nothing was changed", changed=False,
                            details={"bytesBefore": before, "bytesAfter": before, "backend": backend})
        text_before = [norm_text(t) for t in _texts(src, None)]
        text_after = [norm_text(t) for t in _texts(candidate, None)]
        if text_before != text_after:
            raise OutputFailed("The optimised file changed the text and was discarded")
        after = candidate.stat().st_size
        if after >= before:
            return OpResult("No size reduction was possible; the document was left as it is", changed=False,
                            details={"bytesBefore": before, "bytesAfter": before, "backend": backend})
        import pypdf
        w2 = pypdf.PdfWriter(clone_from=pypdf.PdfReader(str(candidate)))
        save_writer(w2, ctx.out, password=ctx.opw) if ctx.doc.encrypted else shutil.copyfile(candidate, ctx.out)
        final = ctx.out.stat().st_size
        return OpResult(f"Optimised: {before:,} to {final:,} bytes", details={"bytesBefore": before, "bytesAfter": final,
                                                                                "backend": backend, "lossless": True},
                        verification={"ok": True, "checks": [{"name": "Text unchanged", "ok": True,
                                                              "detail": f"{len(text_after)} page(s) compared"}]})
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _texts(path: Path, password: str | None) -> list[str]:
    r = open_reader(path, password)
    return [p.extract_text() or "" for p in r.pages]


@op("ocr", "modify")
def _ocr(ctx: Ctx, spec: dict[str, Any]) -> OpResult:
    """Make scanned pages searchable: pages without text are recognised with Tesseract and replaced by a page that shows the
    original image with an invisible text layer. Pages that already have text are kept untouched."""
    tools = ctx.svc.tools
    if not tools.tesseract:
        raise DependencyMissing("OCR needs the Tesseract program (https://github.com/tesseract-ocr/tesseract). It is not installed.",
                                details={"needs": "Tesseract"})
    from ..docmodel.ocr import check_language, render_page_png
    lang = check_language(tools, str(spec.get("lang", "eng")))
    import pypdf
    reader = ctx.reader()
    targets = [i for i in ctx.pages(spec.get("pages")) if len(norm_text(reader.pages[i].extract_text() or "")) < 3] \
        if not spec.get("force") else ctx.pages(spec.get("pages"))
    if not targets:
        return OpResult("Every selected page already has text; nothing to recognise", changed=False)
    timeout = ctx.svc.settings.subprocess_timeout_s
    replaced: dict[int, Any] = {}
    work = Path(tempfile.mkdtemp(prefix="dm-ocr-", dir=ctx.out.parent))
    try:
        for n, i in enumerate(targets):
            ctx.control.report(n / len(targets), f"Recognising page {i + 1} ({n + 1}/{len(targets)})")
            png = work / f"p{i}.png"
            png.write_bytes(render_page_png(ctx.src, i, ctx.password, dpi=300))
            base = work / f"o{i}"
            proc = subprocess.run([tools.tesseract, str(png), str(base), "-l", lang, "--dpi", "300", "pdf"],
                                  capture_output=True, text=True, timeout=timeout)
            out_pdf = base.with_suffix(".pdf")
            if proc.returncode != 0 or not out_pdf.exists():
                raise OutputFailed(f"Tesseract failed on page {i + 1}: {(proc.stderr or '').strip()[:200]}")
            replaced[i] = pypdf.PdfReader(str(out_pdf)).pages[0]
        w = pypdf.PdfWriter()
        for i in range(len(reader.pages)):
            w.add_page(replaced[i] if i in replaced else reader.pages[i])
        meta = reader.metadata
        if meta:
            w.add_metadata({k: str(v) for k, v in meta.items()})
        save_writer(w, ctx.out, password=ctx.opw)
        got = open_reader(ctx.out, ctx.opw)
        recognised = sum(1 for i in replaced if len(norm_text(got.pages[i].extract_text() or "")) >= 3)
        if recognised == 0:
            raise OutputFailed("OCR found no text on the selected pages; the document was left as it is")
        return OpResult(f"Recognised text on {recognised} of {len(targets)} page(s)",
                        details={"pages": [i + 1 for i in targets], "recognised": recognised, "language": lang},
                        warnings=["Recognised text is a machine reading of an image and can contain mistakes."],
                        verification={"ok": True, "checks": [{"name": "Searchable text present", "ok": True,
                                                              "detail": f"{recognised} page(s) now have text"}]})
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ----------------------------------------------------------------------------------------------- exports (no revision)
def export_pages(ws: Any, doc: Any, pages_spec: Any, out_dir: Path, stem: str, *, password: str | None = None,
                 split_each: bool = False) -> list[Path]:
    """Write selected pages as new PDF(s) in ``out_dir`` (never overwriting). Returns the files."""
    from ..files import atomic_output
    total = doc.pages
    chosen = stamp.resolve_pages(pages_spec, total)
    groups = [[i] for i in chosen] if split_each else [chosen]
    outs: list[Path] = []
    src = PdfSource(doc.path, doc.name, doc.password)
    for n, group in enumerate(groups):
        name = f"{stem}" if not split_each else f"{stem}_p{group[0] + 1}"
        with atomic_output(out_dir, name, ".pdf") as out:
            items = [PlanItem(0, i) for i in group]
            assemble([src], items, out.temp, title=None, keep_bookmarks=True, password=password)
            ver = verify_assembly(out.temp, [src], items, password)
            if not ver["ok"]:
                raise OutputFailed("The extracted PDF failed verification and was discarded")
            outs.append(out.commit())
    return outs
