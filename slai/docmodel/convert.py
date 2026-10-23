"""The converter: any supported file -> any supported target, through the SLDC document model, with verification.

Two engines:

``model``        read the source into the SLDC document model, write the target from it. Structure and text are preserved
                 as listed in the capability matrix; page layout/fonts of the source are not (the report says so).
``libreoffice``  hand the file to LibreOffice (when installed) for layout-preserving Office/PDF conversions. The result is
                 verified the same way; the report names the engine.

Nothing is overwritten: the output gets a free name next to existing files. A cancelled or failed conversion leaves no file.
"""
from __future__ import annotations

import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..bridge import sldc
from ..errors import DependencyMissing, DocumasterError, OutputFailed, UnsupportedOperation, ValidationFailed
from ..files import atomic_output, reserve_path, sha256_file
from ..formats import FORMATS, TARGETS, ext_for, label_for
from ..services import NO_CONTROL, JobControl, Services
from .readers import read_document, soffice_convert
from .verify import verify_written, words_of
from .writers import WriteOptions, WriteResult, get_writer

LO_TARGETS = {"pdf": "pdf", "docx": "docx", "odt": "odt", "rtf": "rtf", "html": "html", "txt": "txt"}
LO_SOURCES = {"docx", "doc", "odt", "rtf", "html", "txt", "md"}


@dataclass
class ConvertOptions:
    target: str
    engine: str = "model"
    password: str | None = None
    ocr: bool = False
    ocr_lang: str = "eng"
    title: str | None = None
    page_size: str | None = None
    margins_mm: float | None = None
    font_size_pt: float = 11.0
    font_family: str | None = None
    page_numbers: bool = True
    author: str | None = None
    include_notes: bool = True
    line_ending: str = "\n"
    encrypt_password: str | None = None      # SLDC output only

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConvertOptions":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        clean = {k: v for k, v in data.items() if k in known and v is not None}
        if "target" not in clean:
            raise ValidationFailed("A target format is required")
        opts = cls(**clean)
        if opts.target not in TARGETS:
            raise ValidationFailed(f"Unsupported target format {opts.target!r}", details={"supported": list(TARGETS)})
        if opts.engine not in ("model", "libreoffice"):
            raise ValidationFailed("engine must be 'model' or 'libreoffice'")
        if not 6 <= opts.font_size_pt <= 36:
            raise ValidationFailed("font size must be between 6 and 36 pt")
        if opts.margins_mm is not None and not 5 <= opts.margins_mm <= 60:
            raise ValidationFailed("margins must be between 5 and 60 mm")
        if opts.line_ending not in ("\n", "\r\n"):
            raise ValidationFailed("line ending must be LF or CRLF")
        return opts

    def writer_options(self, stem: str) -> WriteOptions:
        return WriteOptions(title=self.title, page_size=self.page_size, margins_mm=self.margins_mm,
                            font_size_pt=self.font_size_pt, font_family=self.font_family, page_numbers=self.page_numbers,
                            author=self.author, include_notes=self.include_notes, line_ending=self.line_ending, stem=stem)


def _free_stem(out_dir: Path, stem: str, ext: str, with_folder: bool) -> str:
    for n in range(0, 10_000):
        candidate = stem if n == 0 else f"{stem} ({n})"
        if not (out_dir / f"{candidate}{ext}").exists() and not (with_folder and (out_dir / f"{candidate}_files").exists()):
            return candidate
    return stem


def convert_file(svc: Services, source: Path, source_name: str, source_fmt: str, opts: ConvertOptions, out_dir: Path,
                 control: JobControl = NO_CONTROL, ir: Any = None) -> dict[str, Any]:
    started = time.perf_counter()
    target = opts.target
    if source_fmt == target:
        raise ValidationFailed(f"The file is already {label_for(target)}. Choose another target format.",
                               code="same_format")
    if opts.engine == "libreoffice":
        return _convert_libreoffice(svc, source, source_name, source_fmt, opts, out_dir, control, started)
    control.report(0.02, "Reading the document")
    result = ir or read_document(source, source_fmt, svc, password=opts.password, name=source_name, ocr=opts.ocr,
                                 ocr_lang=opts.ocr_lang, control=control)
    package = result.package
    if opts.title:
        package.document["title"] = opts.title
    control.report(0.6, f"Writing {label_for(target)}")
    stem = Path(source_name).stem or "document"
    has_assets_folder = target == "md" and bool(package.assets)
    stem = _free_stem(out_dir, stem, ext_for(target), has_assets_folder)
    writer = get_writer(target)
    wopts = opts.writer_options(stem)
    with atomic_output(out_dir, stem, ext_for(target)) as out:
        wopts.stem = out.destination.stem
        try:
            written: WriteResult = writer(package, out.temp, wopts)
        except DocumasterError:
            raise
        except sldc().SLDCError as exc:
            raise OutputFailed(f"The document model is not valid for {label_for(target)}: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise OutputFailed(f"Writing {label_for(target)} failed: {type(exc).__name__}: {exc}") from exc
        control.report(0.8, "Verifying the output")
        if target == "sldc" and opts.encrypt_password:
            _encrypt_sldc(out.temp, opts.encrypt_password)
        verification = verify_written(out.temp, target, package, svc,
                                      password=opts.encrypt_password if target == "sldc" else None)
        if not verification["ok"]:
            # A file that fails its own verification is not delivered.
            failed = [c for c in verification["checks"] if c["ok"] is False]
            raise OutputFailed("The converted file failed verification and was discarded: "
                               + "; ".join(f"{c['name']} ({c['detail']})" for c in failed),
                               details={"verification": verification})
        final = out.commit()
    extras: list[Path] = []
    for rel, data in written.extra_data:
        extra_path = out_dir / rel
        extra_path.parent.mkdir(parents=True, exist_ok=True)
        extra_path.write_bytes(data)
        extras.append(extra_path)
    control.report(1.0, "Done")
    return _report(svc, source, source_name, source_fmt, final, extras, result, written, verification, "model",
                   started, opts)


def _encrypt_sldc(path: Path, password: str) -> None:
    s = sldc()
    try:
        package = s.read_package(path)
        recipient = s.password_recipient(password)
        path.write_bytes(s.dumps_package(package, recipients=[recipient]))
    except s.SLDCDependencyError as exc:
        raise DependencyMissing("Encrypting an SLDC document needs the 'cryptography' package") from exc
    except s.SLDCError as exc:
        raise ValidationFailed(f"The SLDC password cannot be used: {exc}") from exc


def _report(svc: Services, source: Path, source_name: str, source_fmt: str, final: Path, extras: list[Path], ir: Any,
            written: WriteResult, verification: dict[str, Any], engine: str, started: float,
            opts: ConvertOptions) -> dict[str, Any]:
    warnings = list(dict.fromkeys([*ir.warnings, *written.warnings]))
    return {
        "input": {"name": source_name, "format": source_fmt, "bytes": source.stat().st_size},
        "output": {"path": str(final), "name": final.name, "format": written.format, "bytes": final.stat().st_size,
                   "sha256": sha256_file(final), "pages": written.pages, "extras": [str(p) for p in extras]},
        "engine": engine,
        "backend": {"read": ir.backend, "write": written.backend},
        "kept": written.kept, "lost": sorted(set([*ir.dropped, *written.lost])), "readFeatures": ir.features,
        "warnings": warnings, "ocrPages": ir.ocr_pages, "verification": verification,
        "durationMs": round((time.perf_counter() - started) * 1000),
    }


def _convert_libreoffice(svc: Services, source: Path, source_name: str, source_fmt: str, opts: ConvertOptions, out_dir: Path,
                         control: JobControl, started: float) -> dict[str, Any]:
    target = opts.target
    if not svc.tools.soffice:
        raise DependencyMissing("The layout-preserving engine needs LibreOffice, which is not installed. "
                                "Use the document-model engine instead.", details={"needs": "LibreOffice"})
    if source_fmt not in LO_SOURCES or target not in LO_TARGETS:
        raise UnsupportedOperation(
            f"LibreOffice cannot convert {label_for(source_fmt)} to {label_for(target)} in Documaster; use the "
            "document-model engine", details={"from": source_fmt, "to": target})
    control.report(0.1, "Converting with LibreOffice")
    with tempfile.TemporaryDirectory(prefix="dm-lo-") as tmp:
        produced = soffice_convert(svc, source, LO_TARGETS[target], Path(tmp), control)
        control.report(0.7, "Verifying the output")
        expected = None
        try:
            ir = read_document(source, source_fmt, svc, password=opts.password, name=source_name, control=control)
            expected_pkg = ir.package
        except DocumasterError:
            ir = None
            expected_pkg = None
        verification = verify_written(produced, target, expected_pkg, svc, check_text=expected_pkg is not None)
        if not verification["ok"]:
            failed = [c for c in verification["checks"] if c["ok"] is False]
            raise OutputFailed("The LibreOffice result failed verification and was discarded: "
                               + "; ".join(f"{c['name']} ({c['detail']})" for c in failed))
        stem = _free_stem(out_dir, Path(source_name).stem or "document", ext_for(target), False)
        with atomic_output(out_dir, stem, ext_for(target)) as out:
            shutil.copyfile(produced, out.temp)
            final = out.commit()
    pages = None
    if target == "pdf":
        import pypdf
        pages = len(pypdf.PdfReader(str(final)).pages)
    from .readers import IRResult  # noqa: F401
    written = WriteResult(final, target, f"LibreOffice ({Path(svc.tools.soffice).name})", pages=pages,
                          kept=["layout, fonts and styles as LibreOffice renders them"],
                          lost=["anything LibreOffice does not support for this pair"])
    fake = ir if ir is not None else type("IRStub", (), {"warnings": [], "dropped": [], "features": [], "backend": "LibreOffice",
                                                          "ocr_pages": []})()
    return _report(svc, source, source_name, source_fmt, final, [], fake, written, verification, "libreoffice", started, opts)
