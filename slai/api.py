"""HTTP API handlers. Long operations run as jobs (``202`` + job id, or synchronously with ``?wait=1``)."""
from __future__ import annotations

import io
import time
from pathlib import Path
from typing import Any, Callable

from . import __version__, capabilities, counter, merger
from .app import App
from .docmodel.convert import ConvertOptions, convert_file
from .errors import DocumasterError, NotFound, UnsupportedOperation, ValidationFailed
from .formats import FORMATS, TARGETS
from .jobs import Job
from .pdfops import docinfo, forms, ops, redact
from .pdfops.annotate import listing as annotation_listing
from .pdfops.pdfio import open_reader
from .pdfops.render import page_sizes, render_page
from .router import Request, Response, ok, route
from .services import JobControl


# ---------------------------------------------------------------------------------------------- jobs
def launch(app: App, req: Request, kind: str, label: str, fn: Callable[[JobControl], Any]) -> Any:
    job = app.jobs.submit(kind, label, fn)
    if req.query.get("wait") in ("1", "true"):
        return wait_for(job)
    return Response(202, ok({"job": job.public(False)}).body)


def wait_for(job: Job) -> Any:
    while job.state in ("queued", "running"):
        time.sleep(0.02)
    if job.state == "failed" and job.error:
        raise DocumasterError(job.error["message"], code=job.error["code"], details=job.error.get("details"),
                              status=500 if job.error["code"] == "internal_error" else None)
    if job.state == "cancelled":
        raise DocumasterError("The operation was cancelled", code="cancelled", status=409)
    return job.result


@route("GET", "/api/jobs")
def jobs_list(app: App, req: Request) -> Any:
    return app.jobs.list()


@route("GET", "/api/jobs/{id}")
def job_get(app: App, req: Request) -> Any:
    return app.jobs.get(req.params["id"]).public()


@route("POST", "/api/jobs/{id}/cancel")
def job_cancel(app: App, req: Request) -> Any:
    return app.jobs.cancel(req.params["id"]).public(False)


# ---------------------------------------------------------------------------------------------- app
@route("GET", "/api/health")
def health(app: App, req: Request) -> Any:
    return {"name": "SLAI Documaster", "version": __version__, "uptimeS": round(time.time() - app.started),
            "documents": len(app.ws.documents)}


@route("GET", "/api/capabilities")
def get_capabilities(app: App, req: Request) -> Any:
    return capabilities.matrix(app.svc)


@route("GET", "/api/agents")
def get_agents(app: App, req: Request) -> Any:
    gw = app.svc.agents
    if req.query.get("test") == "1":                       # one real call per agent
        from .analysis import agent_calls
        for role in ("privacy", "safety", "language", "quality"):
            gw.ensure(role)
        agent_calls.self_test(app.svc, ["privacy", "safety", "language", "quality"])
    out = gw.status(probe=req.query.get("probe") == "1" or req.query.get("test") == "1")
    reader = app.svc.reader.status()["engines"]            # Reader is used through its engines, not through the ReaderAgent
    ok = [n for n, e in reader.items() if e["available"]]
    out["agents"]["reader"].update({"state": "ready" if len(ok) == len(reader) else ("failed" if not ok else "partial"),
                                    "class": "Reader engines", "error": None if len(ok) == len(reader) else "; ".join(
                                        f"{n}: {e['error']}" for n, e in reader.items() if not e["available"])[:300],
                                    "calls": out["agents"]["reader"]["calls"]})
    return out


@route("GET", "/api/settings")
def get_settings(app: App, req: Request) -> Any:
    return app.settings.public()


@route("PUT", "/api/settings")
def put_settings(app: App, req: Request) -> Any:
    body = req.json()
    if "agent_read_default" in body:
        app.settings.agent_read_default = bool(body["agent_read_default"])
        app.consent.default = app.settings.agent_read_default and app.settings.agents != "none"
    if "agent_telemetry" in body:
        app.settings.agent_telemetry = bool(body["agent_telemetry"])
    if "output_dir" in body:
        path = Path(str(body["output_dir"])).expanduser()
        if not path.is_absolute():
            raise ValidationFailed("The output folder must be a full path")
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / f".documaster-{int(time.time())}.tmp"
            probe.write_bytes(b"")
            probe.unlink()
        except OSError as exc:
            raise ValidationFailed(f"That folder cannot be written to: {exc}") from exc
        app.settings.output_dir = path
    return app.settings.public()


# ---------------------------------------------------------------------------------------------- documents
@route("GET", "/api/documents")
def documents(app: App, req: Request) -> Any:
    return app.ws.list()


@route("POST", "/api/documents")
def upload(app: App, req: Request) -> Any:
    name = req.query.get("name") or "document"
    doc = app.ws.add_upload(name, req.stream, req.length, allow_images=req.query.get("images") == "1")  # type: ignore[arg-type]
    return doc.public()


@route("DELETE", "/api/documents/{id}")
def delete_document(app: App, req: Request) -> Any:
    app.ws.remove(req.params["id"])
    return {"removed": req.params["id"]}


@route("POST", "/api/documents/{id}/unlock")
def unlock(app: App, req: Request) -> Any:
    return app.ws.unlock(req.params["id"], str(req.json().get("password", ""))).public()


@route("POST", "/api/documents/{id}/undo")
def undo(app: App, req: Request) -> Any:
    return app.ws.step(app.ws.get(req.params["id"]), -1).public()


@route("POST", "/api/documents/{id}/redo")
def redo(app: App, req: Request) -> Any:
    return app.ws.step(app.ws.get(req.params["id"]), +1).public()


@route("POST", "/api/documents/{id}/revision/{n}")
def goto_revision(app: App, req: Request) -> Any:
    doc = app.ws.get(req.params["id"])
    target = int(req.params["n"])
    if not 0 <= target < len(doc.revisions):
        raise ValidationFailed("That revision does not exist")
    return app.ws.step(doc, target - doc.cursor).public()


@route("GET", "/api/documents/{id}/file")
def document_file(app: App, req: Request) -> Any:
    doc = app.ws.get(req.params["id"])
    app.ws.require_open(doc)
    return Response(200, file_path=str(doc.path), content_type=FORMATS[doc.format].mime, download_name=_download_name(doc))


def _download_name(doc: Any) -> str:
    stem = Path(doc.name).stem
    return f"{stem}{FORMATS[doc.format].exts[0]}"


@route("GET", "/api/documents/{id}/pages/{n}/image")
def page_image(app: App, req: Request) -> Any:
    doc = app.ws.get(req.params["id"])
    app.ws.require_open(doc)
    index = int(req.params["n"])
    width = min(max(int(req.query.get("w", 240)), 40), 2400)
    fmt = "jpeg" if req.query.get("fmt") == "jpeg" else "png"
    if doc.format in ("png", "jpeg"):
        from PIL import Image
        with Image.open(doc.path) as im:
            im.thumbnail((width, width * 4))
            buf = io.BytesIO()
            im.convert("RGB").save(buf, "PNG")
            data, media = buf.getvalue(), "image/png"
    elif doc.format == "pdf":
        data, media, _ = render_page(doc.path, index, width_px=width, password=doc.password, fmt=fmt)
    else:
        raise UnsupportedOperation("Page images are available for PDF documents and images")
    return Response(200, data, media, {"Cache-Control": "private, max-age=600"})


@route("GET", "/api/documents/{id}/pdfinfo")
def pdf_info(app: App, req: Request) -> Any:
    doc = app.ws.get(req.params["id"])
    app.ws.require_open(doc)
    if doc.format != "pdf":
        raise UnsupportedOperation("Only PDF documents have this information")
    reader = open_reader(doc.path, doc.password)
    return {"metadata": docinfo.read_metadata(reader), "outline": docinfo.outline_tree(reader),
            "annotations": annotation_listing(reader), "fields": forms.list_fields(reader),
            "pages": page_sizes(doc.path, doc.password), "permissions": doc.permissions}


@route("POST", "/api/documents/{id}/find")
def find(app: App, req: Request) -> Any:
    doc = app.ws.get(req.params["id"])
    app.ws.require_open(doc)
    if doc.format != "pdf":
        raise UnsupportedOperation("Text search with page positions works on PDF documents")
    b = req.json()
    hits = redact.find_text(doc.path, doc.password, str(b.get("query", "")), regex=bool(b.get("regex")),
                            case=bool(b.get("case")), whole_word=bool(b.get("wholeWord")))
    return {"query": b.get("query"), "count": len(hits), "hits": hits}


@route("POST", "/api/assets")
def upload_asset(app: App, req: Request) -> Any:
    data = req.body_bytes(25 * 1024 * 1024)
    a = app.ws.add_asset(req.query.get("name") or "image", data)
    return {"id": a.id, "name": a.name, "mediaType": a.media_type, "width": a.width, "height": a.height}


# ---------------------------------------------------------------------------------------------- outputs
@route("GET", "/api/outputs/{id}/download")
def output_download(app: App, req: Request) -> Any:
    out = app.ws.output(req.params["id"])
    return Response(200, file_path=str(out.path), content_type=FORMATS.get(out.format).mime if out.format in FORMATS
                    else "application/octet-stream", download_name=out.name)


@route("POST", "/api/outputs/{id}/open")
def output_open(app: App, req: Request) -> Any:
    """Open a file Documaster produced as a document, so it can be edited, counted or merged further."""
    out = app.ws.output(req.params["id"])
    return app.ws.add_existing(out.name, out.path.read_bytes()).public()


# ---------------------------------------------------------------------------------------------- word counter
@route("POST", "/api/count/text")
def count_text(app: App, req: Request) -> Any:
    b = req.json()
    text = b.get("text")
    if not isinstance(text, str):
        raise ValidationFailed("'text' must be a string")
    return counter.count_text(text, top=int(b.get("top", 15)), goals=b.get("goals"))


@route("POST", "/api/count/documents")
def count_documents(app: App, req: Request) -> Any:
    b = req.json()
    ids = b.get("docs")
    if not isinstance(ids, list) or not ids:
        raise ValidationFailed("Choose at least one document")
    docs = [app.ws.get(i) for i in ids]

    def work(control: JobControl) -> Any:
        results = []
        for n, d in enumerate(docs):
            sub = _Slice(control, n, len(docs))
            try:
                r = counter.count_document(app.svc, app.ws, d, top=int(b.get("top", 15)), goals=b.get("goals"),
                                           ocr=bool(b.get("ocr")), ocr_lang=str(b.get("ocrLang", "eng")), control=sub)  # type: ignore[arg-type]
                results.append({"id": d.id, "ok": True, "stats": r})
            except DocumasterError as exc:
                if exc.code == "cancelled":
                    raise
                results.append({"id": d.id, "name": d.name, "ok": False, "error": exc.to_dict()})
        good = [r["stats"] for r in results if r["ok"]]
        return {"results": results, "totals": counter.totals(good) if len(good) > 1 else None}

    return launch(app, req, "count", f"Count {len(docs)} document(s)", work)


class _Slice(JobControl):
    """Maps a sub-task's progress into its share of a batch."""

    def __init__(self, parent: JobControl, index: int, total: int) -> None:
        super().__init__(None)
        self.parent, self.index, self.total = parent, index, total

    def check(self) -> None:
        self.parent.check()

    def report(self, fraction: float, message: str = "") -> None:
        self.parent.report((self.index + fraction) / self.total, message)


# ---------------------------------------------------------------------------------------------- converter
@route("POST", "/api/convert")
def convert(app: App, req: Request) -> Any:
    b = req.json()
    ids = b.get("docs")
    if not isinstance(ids, list) or not ids:
        raise ValidationFailed("Choose at least one document to convert")
    opts_in = b.get("options") or {}
    if not isinstance(opts_in, dict):
        raise ValidationFailed("'options' must be an object")
    docs = [app.ws.get(i) for i in ids]
    for d in docs:
        app.ws.require_open(d)
    ConvertOptions.from_dict(opts_in)          # validate before queueing
    out_dir = app.ws.output_dir()

    def work(control: JobControl) -> Any:
        results = []
        for n, d in enumerate(docs):
            sub = _Slice(control, n, len(docs))
            o = ConvertOptions.from_dict({**opts_in, "password": opts_in.get("password") or d.password})
            try:
                ir = None
                if o.engine == "model" and d.format != o.target:
                    ir = app.ws.ir(d, sub, ocr=o.ocr, ocr_lang=o.ocr_lang)  # type: ignore[arg-type]
                report = convert_file(app.svc, d.path, d.name, d.format, o, out_dir, sub, ir=ir)  # type: ignore[arg-type]
                out = app.ws.register_output(Path(report["output"]["path"]), o.target,
                                             [Path(p).name for p in report["output"]["extras"]])
                report["output"]["id"] = out.id
                results.append({"id": d.id, "ok": True, "report": report})
            except DocumasterError as exc:
                if exc.code == "cancelled":
                    raise
                results.append({"id": d.id, "name": d.name, "ok": False, "error": exc.to_dict()})
        return {"results": results, "succeeded": sum(1 for r in results if r["ok"]), "failed": sum(1 for r in results if not r["ok"])}

    return launch(app, req, "convert", f"Convert {len(docs)} document(s) to {opts_in.get('target')}", work)


@route("GET", "/api/convert/formats")
def convert_formats(app: App, req: Request) -> Any:
    m = capabilities.matrix(app.svc)
    return {"targets": [{"id": t, "label": FORMATS[t].label, "available": m["write"][t]["available"],
                         "limits": m["write"][t]["limits"]} for t in TARGETS],
            "engines": {"libreoffice": bool(app.svc.tools.soffice), "ocr": bool(app.svc.tools.tesseract)}}


# ---------------------------------------------------------------------------------------------- merger
@route("POST", "/api/merge/outline")
def merge_outline(app: App, req: Request) -> Any:
    b = req.json()
    ids = b.get("docs")
    if not isinstance(ids, list) or not ids:
        raise ValidationFailed("Add at least one document")
    mode = "pdf" if b.get("target", "pdf") == "pdf" else "model"
    return merger.outline(app.svc, app.ws, ids, mode=mode, split_headings=int(b.get("splitHeadings", 0) or 0))


@route("POST", "/api/merge")
def merge(app: App, req: Request) -> Any:
    b = req.json()

    def work(control: JobControl) -> Any:
        report = merger.run_merge(app.svc, app.ws, b, control)
        out = app.ws.register_output(Path(report["output"]["path"]), report["output"]["format"],
                                     [Path(p).name for p in report["output"]["extras"]])
        report["output"]["id"] = out.id
        return report

    return launch(app, req, "merge", "Merge documents", work)


# ---------------------------------------------------------------------------------------------- PDF editor
@route("POST", "/api/documents/{id}/op")
def pdf_op(app: App, req: Request) -> Any:
    doc = app.ws.get(req.params["id"])
    spec = req.json()

    def work(control: JobControl) -> Any:
        with app.ws.lock:
            return ops.apply(app.svc, app.ws, doc, spec, control)

    return launch(app, req, "edit", f"{spec.get('op', 'edit')} on {doc.name}", work)


@route("POST", "/api/documents/{id}/export")
def pdf_export(app: App, req: Request) -> Any:
    doc = app.ws.get(req.params["id"])
    app.ws.require_open(doc)
    b = req.json()
    if doc.format != "pdf":
        raise UnsupportedOperation("Page export works on PDF documents")
    out_dir = app.ws.output_dir()

    def work(control: JobControl) -> Any:
        files = ops.export_pages(app.ws, doc, b.get("pages", "all"), out_dir, Path(doc.name).stem + "_pages",
                                 password=b.get("password") or None, split_each=bool(b.get("splitEach")))
        outs = [app.ws.register_output(p, "pdf") for p in files]
        return {"outputs": [o.public() for o in outs]}

    return launch(app, req, "export", f"Export pages of {doc.name}", work)


@route("POST", "/api/documents/{id}/save")
def pdf_save(app: App, req: Request) -> Any:
    """Write the current revision of a PDF to the output folder (never overwriting)."""
    from .files import atomic_output
    import shutil
    doc = app.ws.get(req.params["id"])
    app.ws.require_open(doc)
    out_dir = app.ws.output_dir()
    ext = FORMATS[doc.format].exts[0]
    from .files import safe_filename
    stem = safe_filename(str(req.json().get("name") or Path(doc.name).stem + "_edited"))
    stem = Path(stem).stem or "document"
    with atomic_output(out_dir, stem, ext) as out:
        shutil.copyfile(doc.path, out.temp)
        final = out.commit()
    return app.ws.register_output(final, doc.format).public()
