"""Merger & page organiser.

Two engines, chosen from the target format and the inputs, and always named in the report:

* **PDF assembly** (target PDF): PDF pages are copied as they are (text, fonts, vectors, forms, bookmarks stay intact); images
  become pages; documents of other formats (SLDC, DOCX, ODT, HTML, Markdown, text, CSV, RTF) are rendered to PDF through the SLDC
  model and their pages are placed in the order given. Page rotation applies to PDF and image pages.
* **Model merge** (any other target): every input is read into the SLDC model and merged by ``docmodel.sldc_merge`` (ids, styles,
  sections, notes, images and provenance handled by the format's rules), then written to the target and verified.

The page plan is a list of ``{doc, page, rotation?, included?}`` (``doc`` is the position in the document list, ``page`` is 0-based);
``{blank: true}`` inserts a blank page in PDF output. Without a plan every page of every document is used in order.
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Any

from .docmodel import ir as irmod
from .docmodel.sldc_merge import MergeUnit, logical_pages, merge_units
from .docmodel.verify import verify_written
from .docmodel.writers import WriteOptions, get_writer
from .errors import DocumasterError, OutputFailed, PasswordRequired, UnsupportedOperation, ValidationFailed
from .files import atomic_output, sha256_file
from .formats import TARGETS, ext_for, label_for
from .pdfops.organize import PdfSource, PlanItem, assemble, parse_plan, verify_assembly
from .pdfops.render import page_sizes
from .services import NO_CONTROL, JobControl, Services


def kind_of(doc: Any) -> str:
    return "pdf" if doc.format == "pdf" else "image" if doc.format in ("png", "jpeg") else "model"


def _stem(name: str) -> str:
    return Path(name).stem or "document"


def outline(svc: Services, ws: Any, ids: list[str], *, mode: str, split_headings: int = 0,
            control: JobControl = NO_CONTROL) -> list[dict[str, Any]]:
    """What the organiser shows: the pages of each document (real PDF pages, or SLDC logical pages)."""
    if mode not in ("pdf", "model"):
        raise ValidationFailed("mode must be 'pdf' or 'model'")
    out: list[dict[str, Any]] = []
    for n, doc_id in enumerate(ids):
        control.report(n / max(len(ids), 1), "Reading document structure")
        doc = ws.get(doc_id)
        ws.require_open(doc)
        kind = kind_of(doc)
        entry: dict[str, Any] = {"id": doc.id, "name": doc.name, "format": doc.format, "kind": kind, "pages": [],
                                 "warnings": []}
        if kind == "pdf" and mode == "pdf":
            sizes = page_sizes(doc.path, doc.password)
            labels = _pdf_labels(doc)
            entry["pages"] = [{"index": s["index"], "label": labels[s["index"]] if s["index"] < len(labels) else f"Page {s['index'] + 1}",
                               "width": s["width"], "height": s["height"], "rotation": s["rotation"]} for s in sizes]
        elif kind == "image":
            entry["pages"] = [{"index": 0, "label": doc.name, "rotation": 0}]
        else:
            result = ws.ir(doc, control)
            pages = logical_pages(result.package, split_headings if kind == "model" else 0)
            entry["pages"] = [p.public() for p in pages]
            entry["warnings"] = result.warnings[:5]
            if kind == "pdf":
                entry["warnings"].append("In this output format a PDF is merged as text only: its layout, images and fonts are not kept")
        out.append(entry)
    return out


def _pdf_labels(doc: Any, limit: int = 400) -> list[str]:
    from .pdfops.organize import page_texts
    if (doc.pages or 0) > limit:
        return []
    try:
        texts = page_texts(doc.path, doc.password)
    except DocumasterError:
        return []
    return [(" ".join(t.split()[:9]) or f"Page {i + 1}") for i, t in enumerate(texts)]


def _default_plan(counts: list[int]) -> list[dict[str, Any]]:
    return [{"doc": d, "page": p} for d, n in enumerate(counts) for p in range(n)]


def run_merge(svc: Services, ws: Any, req: dict[str, Any], control: JobControl = NO_CONTROL) -> dict[str, Any]:
    started = time.perf_counter()
    ids = req.get("docs")
    if not isinstance(ids, list) or not ids:
        raise ValidationFailed("Add at least one document to merge")
    if len(ids) > svc.settings.max_sources_per_merge:
        raise ValidationFailed(f"At most {svc.settings.max_sources_per_merge} documents can be merged at once")
    target = req.get("target", "pdf")
    if target not in TARGETS:
        raise ValidationFailed(f"Unsupported target format {target!r}", details={"supported": list(TARGETS)})
    docs = [ws.get(i) for i in ids]
    for d in docs:
        ws.require_open(d)
    password = req.get("password") or None
    out_dir = ws.output_dir()
    stem_req = (req.get("stem") or "").strip()
    stem = stem_req or (f"{_stem(docs[0].name)}_merged" if len(docs) > 1 else f"{_stem(docs[0].name)}_organized")
    warnings: list[str] = []
    mode = "pdf" if target == "pdf" else "model"
    split = int(req.get("splitHeadings", 0) or 0)
    if not 0 <= split <= 6:
        raise ValidationFailed("splitHeadings must be 0-6")
    control.report(0.03, "Reading documents")
    outlines = outline(svc, ws, ids, mode=mode, split_headings=split, control=control)
    counts = [len(o["pages"]) for o in outlines]
    plan_entries = req.get("plan") or _default_plan(counts)
    if not isinstance(plan_entries, list):
        raise ValidationFailed("'plan' must be a list")
    for e in plan_entries:
        if isinstance(e, dict) and e.get("rotation") and not e.get("blank"):
            try:
                if kind_of(docs[int(e["doc"])]) == "model":
                    warnings.append("Rotation applies to PDF and image pages; it was ignored for pages of other document types")
                    break
            except (KeyError, ValueError, IndexError, TypeError):
                break
    if mode == "pdf":
        result = _merge_pdf(svc, ws, docs, plan_entries, counts, out_dir, stem, req, password, control, warnings)
    else:
        result = _merge_model(svc, ws, docs, plan_entries, counts, out_dir, stem, target, req, password, split, control, warnings)
    result["inputs"] = [{"name": d.name, "format": d.format, "pages": c, "sha256": d.revision.sha256}
                        for d, c in zip(docs, counts)]
    if any(d.encrypted for d in docs) and not password:
        result["warnings"].append("Some inputs were password-protected; the merged file is not. Set a password to protect it.")
    result["durationMs"] = round((time.perf_counter() - started) * 1000)
    return result


def _commit_extras(out_dir: Path, extras: list[tuple[str, bytes]]) -> list[str]:
    paths: list[str] = []
    for rel, data in extras:
        p = out_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        paths.append(str(p))
    return paths


def _output_block(final: Path, fmt: str, pages: int | None, extras: list[str]) -> dict[str, Any]:
    return {"path": str(final), "name": final.name, "format": fmt, "bytes": final.stat().st_size,
            "sha256": sha256_file(final), "pages": pages, "extras": extras}


# ------------------------------------------------------------------------------------------------ PDF assembly
def _merge_pdf(svc: Services, ws: Any, docs: list[Any], plan_entries: list[dict[str, Any]], counts: list[int], out_dir: Path,
               stem: str, req: dict[str, Any], password: str | None, control: JobControl, warnings: list[str]) -> dict[str, Any]:
    items = parse_plan(plan_entries, counts, svc.settings.max_pdf_pages)
    kinds = [kind_of(d) for d in docs]
    tmp = Path(tempfile.mkdtemp(prefix="dm-merge-", dir=ws.root))
    try:
        sources: list[PdfSource] = []
        src_index: dict[int, int] = {}           # doc position -> source index (pdf/image docs)
        for i, d in enumerate(docs):
            if kinds[i] in ("pdf", "image") and any((not it.blank) and it.source == i for it in items):
                src_index[i] = len(sources)
                sources.append(PdfSource(d.path, d.name, d.password, "image" if kinds[i] == "image" else "pdf"))
        final_items: list[PlanItem] = []
        render_notes: list[str] = []
        lost: list[str] = []
        i = 0
        while i < len(items):
            it = items[i]
            if it.blank or kinds[it.source] != "model":
                if it.blank:
                    final_items.append(it)
                else:
                    final_items.append(PlanItem(src_index[it.source], it.page, it.rotation))
                i += 1
                continue
            j = i
            run: list[PlanItem] = []
            while j < len(items) and not items[j].blank and kinds[items[j].source] == "model":
                run.append(items[j])
                j += 1
            control.report(0.2 + 0.4 * (i / len(items)), "Rendering documents to PDF")
            used = sorted({r.source for r in run})
            units = [MergeUnit(ws.ir(docs[u], control).package, docs[u].name, docs[u].revision.sha256) for u in used]
            local = {u: n for n, u in enumerate(used)}
            merged = merge_units(units, [{"doc": local[r.source], "page": r.page} for r in run],
                                 split_headings=int(req.get("splitHeadings", 0) or 0), title=req.get("title"))
            warnings += merged.warnings
            pdf_path = tmp / f"run{i}.pdf"
            wopts = WriteOptions(title=req.get("title"), page_size=req.get("pageSize"), margins_mm=req.get("marginsMm"),
                                 font_size_pt=float(req.get("fontSizePt", 11)), stem=f"run{i}")
            written = get_writer("pdf")(merged.package, pdf_path, wopts)
            warnings += written.warnings
            lost += written.lost
            n_pages = written.pages or len(__import__("pypdf").PdfReader(str(pdf_path)).pages)
            idx = len(sources)
            sources.append(PdfSource(pdf_path, f"{docs[used[0]].name}.pdf"))
            final_items += [PlanItem(idx, p) for p in range(n_pages)]
            i = j
        if not final_items:
            raise ValidationFailed("No pages are included in the plan")
        control.report(0.7, "Assembling the PDF")
        with atomic_output(out_dir, stem, ".pdf") as out:
            info = assemble(sources, final_items, out.temp, title=req.get("title"),
                            source_bookmarks=bool(req.get("sourceBookmarks", len(docs) > 1)),
                            keep_bookmarks=bool(req.get("keepBookmarks", True)), password=password,
                            author=req.get("author"))
            control.report(0.9, "Verifying the result")
            ver = verify_assembly(out.temp, sources, final_items, password)
            if not ver["ok"]:
                raise OutputFailed("The merged PDF failed verification and was discarded: "
                                   + "; ".join(c["detail"] for c in ver["checks"] if not c["ok"]), details={"verification": ver})
            final = out.commit()
        warnings += info["warnings"]
        engine = "PDF page assembly (pypdf)" + (" + SLDC model rendering for non-PDF inputs" if any(k == "model" for k in kinds) else "")
        return {"output": _output_block(final, "pdf", info["pages"], []), "engine": "pdf-assembly",
                "backend": {"merge": engine}, "bookmarks": info["bookmarks"],
                "kept": ["PDF pages are copied unchanged: text, fonts, vector graphics, images"] +
                        (["bookmarks of the source PDFs for pages that are included"] if req.get("keepBookmarks", True) else []),
                "lost": sorted(set(lost)) + (["Source form fields only when rearranged pages could be reattached"]
                                              if info["formsKept"] else []),
                "warnings": list(dict.fromkeys(warnings)), "verification": ver,
                "plan": {"pages": len(final_items), "excluded": sum(counts) - len([e for e in plan_entries if not e.get('blank') and e.get('included', True)])}}
    finally:
        __import__("shutil").rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------------------------------------ model merge
def _image_unit(doc: Any) -> MergeUnit:
    from .bridge import sldc
    s = sldc()
    data = doc.path.read_bytes()
    asset = s.make_asset(data)
    alt = f"Image {Path(doc.name).stem}"
    block = {"id": "b00001", "type": "figure", "text": "", "figure": {"asset": asset.id, "alt": alt}}
    package = irmod.build_package(Path(doc.name).stem, [block], assets=[asset],
                                  provenance=[irmod.import_provenance(doc.name, "Pillow", doc.revision.sha256,
                                                                      source_format=doc.format)])
    return MergeUnit(package, doc.name, doc.revision.sha256)


def _merge_model(svc: Services, ws: Any, docs: list[Any], plan_entries: list[dict[str, Any]], counts: list[int],
                 out_dir: Path, stem: str, target: str, req: dict[str, Any], password: str | None, split: int,
                 control: JobControl, warnings: list[str]) -> dict[str, Any]:
    if any(e.get("blank") for e in plan_entries if isinstance(e, dict)):
        raise UnsupportedOperation("Blank pages can only be inserted when the output is a PDF")
    units: list[MergeUnit] = []
    backends: list[str] = []
    for n, d in enumerate(docs):
        control.report(0.05 + 0.3 * n / len(docs), f"Reading {d.name}")
        if kind_of(d) == "image":
            units.append(_image_unit(d))
            backends.append("Pillow (image)")
            continue
        res = ws.ir(d, control)
        units.append(MergeUnit(res.package, d.name, d.revision.sha256))
        backends.append(res.backend)
        warnings += [f"{d.name}: {w}" for w in res.warnings[:4]]
        if d.format == "pdf":
            warnings.append(f"{d.name}: a PDF merged into {label_for(target)} keeps its text and structure, not its page layout")
    control.report(0.4, "Merging")
    merged = merge_units(units, plan_entries if req.get("plan") else None, title=req.get("title"), split_headings=split,
                         author=req.get("author"))
    warnings += merged.warnings
    wopts = WriteOptions(title=req.get("title"), page_size=req.get("pageSize"), margins_mm=req.get("marginsMm"),
                         font_size_pt=float(req.get("fontSizePt", 11)), page_numbers=bool(req.get("pageNumbers", True)),
                         author=req.get("author"), stem=stem)
    writer = get_writer(target)
    with atomic_output(out_dir, stem, ext_for(target)) as out:
        wopts.stem = out.destination.stem
        try:
            written = writer(merged.package, out.temp, wopts)
        except DocumasterError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise OutputFailed(f"Writing {label_for(target)} failed: {type(exc).__name__}: {exc}") from exc
        control.report(0.8, "Verifying the result")
        if target == "sldc" and password:
            from .docmodel.convert import _encrypt_sldc
            _encrypt_sldc(out.temp, password)
        ver = verify_written(out.temp, target, merged.package, svc, password=password if target == "sldc" else None)
        if not ver["ok"]:
            raise OutputFailed("The merged file failed verification and was discarded: "
                               + "; ".join(f"{c['name']} ({c['detail']})" for c in ver["checks"] if c["ok"] is False),
                               details={"verification": ver})
        final = out.commit()
    extras = _commit_extras(out_dir, written.extra_data)
    warnings += written.warnings
    dropped = [f"{k}: {v}" for k, v in merged.dropped.items()]
    return {"output": _output_block(final, target, written.pages, extras), "engine": "sldc-model-merge",
            "backend": {"read": backends, "merge": "Documaster SLDC merge (sldc.py rules)", "write": written.backend},
            "kept": written.kept, "lost": sorted(set([*written.lost, *dropped])),
            "warnings": list(dict.fromkeys(warnings)), "verification": ver,
            "plan": {"pages": merged.pages, "excluded": sum(counts) - merged.pages if not req.get("plan") else
                     sum(counts) - len([e for e in plan_entries if e.get("included", True)])}}
