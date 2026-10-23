"""The workspace: uploaded documents (untrusted), their revisions, and the files Documaster has produced.

* Uploads are streamed to a private session folder, never executed, and identified from their bytes (a ``.docx`` that is a PDF
  is treated as a PDF and the user is told).
* A PDF document keeps a list of revisions (``rev-N.pdf``) so the editor can undo/redo; other formats have one revision.
* Passwords live in memory only. They are never written to disk, logs or reports.
* Outputs are written to the configured output folder; the workspace only remembers where, so the UI can offer downloads.
"""
from __future__ import annotations

import io
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

from .bridge import sldc
from .config import Settings
from .errors import (FileRejected, LimitExceeded, NotFound, PasswordRequired, UnsupportedOperation, ValidationFailed,
                     WrongPassword)
from .files import free_bytes, replace_file, rmtree_quiet, safe_filename, sha256_file
from .formats import FORMATS, format_for_ext, label_for, sniff
from .services import Services


@dataclass
class Revision:
    number: int
    path: Path
    label: str
    created: float
    bytes: int
    sha256: str
    password: str | None = None       # what opens this revision (kept in memory only)
    encrypted: bool = False


@dataclass
class Document:
    id: str
    name: str
    format: str
    claimed_format: str | None
    created: float
    revisions: list[Revision]
    cursor: int = 0
    locked: bool = False                      # needs a password that has not been supplied yet
    pages: int | None = None
    warnings: list[str] = field(default_factory=list)
    permissions: dict[str, bool] | None = None   # PDF owner-restricted operations
    owner: bool = True                        # opened with the owner password (or not restricted)
    info: dict[str, Any] = field(default_factory=dict)

    @property
    def password(self) -> str | None:
        return self.revisions[self.cursor].password

    @password.setter
    def password(self, value: str | None) -> None:
        self.revisions[self.cursor].password = value

    @property
    def encrypted(self) -> bool:
        return self.revisions[self.cursor].encrypted

    @encrypted.setter
    def encrypted(self, value: bool) -> None:
        self.revisions[self.cursor].encrypted = value

    @property
    def revision(self) -> Revision:
        return self.revisions[self.cursor]

    @property
    def path(self) -> Path:
        return self.revision.path

    def public(self) -> dict[str, Any]:
        rev = self.revision
        return {
            "id": self.id, "name": self.name, "format": self.format, "formatLabel": label_for(self.format),
            "bytes": rev.bytes, "sha256": rev.sha256, "pages": self.pages, "encrypted": self.encrypted,
            "locked": self.locked, "owner": self.owner, "warnings": self.warnings, "permissions": self.permissions, "info": self.info,
            "revision": {"current": self.cursor, "count": len(self.revisions),
                         "labels": [r.label for r in self.revisions], "canUndo": self.cursor > 0,
                         "canRedo": self.cursor < len(self.revisions) - 1},
        }


@dataclass
class Asset:
    id: str
    path: Path
    name: str
    media_type: str
    width: int
    height: int


@dataclass
class Output:
    id: str
    path: Path
    name: str
    format: str
    bytes: int
    created: float
    extras: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "format": self.format, "bytes": self.bytes, "path": str(self.path),
                "extras": self.extras}


class Workspace:
    def __init__(self, svc: Services) -> None:
        self.svc = svc
        self.settings: Settings = svc.settings
        self.run_id = uuid.uuid4().hex[:12]
        self.root = self.settings.sessions_dir / self.run_id
        self.root.mkdir(parents=True, exist_ok=True)
        self.documents: dict[str, Document] = {}
        self.outputs: dict[str, Output] = {}
        self.assets: dict[str, Asset] = {}
        self._ir: dict[tuple[Any, ...], Any] = {}
        self.lock = threading.RLock()
        self.cleanup_stale()

    # ------------------------------------------------------------------ housekeeping
    def cleanup_stale(self) -> None:
        """Remove old session folders left by earlier runs (older than the configured TTL)."""
        base = self.settings.sessions_dir
        now = time.time()
        try:
            for child in base.iterdir():
                if child == self.root or not child.is_dir():
                    continue
                try:
                    if now - child.stat().st_mtime > self.settings.session_ttl_s:
                        rmtree_quiet(child)
                except OSError:
                    continue
        except OSError:
            pass

    def close(self) -> None:
        rmtree_quiet(self.root)

    # ------------------------------------------------------------------ documents
    def get(self, doc_id: str) -> Document:
        with self.lock:
            doc = self.documents.get(doc_id)
        if doc is None:
            raise NotFound("That document is not open (it may have been closed)", details={"id": doc_id})
        return doc

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            return [d.public() for d in self.documents.values()]

    def remove(self, doc_id: str) -> None:
        with self.lock:
            doc = self.documents.pop(doc_id, None)
        if doc is None:
            raise NotFound("That document is not open")
        rmtree_quiet(self.root / doc.id)

    def add_upload(self, name: str, stream: BinaryIO, length: int | None, *, allow_images: bool = False) -> Document:
        """Store an upload and identify it from its bytes."""
        if length is not None and length > self.settings.max_upload_bytes:
            raise LimitExceeded(f"File is larger than the {self.settings.max_upload_bytes // (1024 * 1024)} MB limit",
                                details={"limitBytes": self.settings.max_upload_bytes})
        free = free_bytes(self.root)
        if free is not None and length is not None and length * 3 > free:
            raise LimitExceeded("Not enough free disk space to open this file")
        doc_id = uuid.uuid4().hex[:10]
        folder = self.root / doc_id
        folder.mkdir(parents=True)
        clean = safe_filename(name, "document")
        original = folder / "rev-0.bin"
        written = 0
        try:
            with open(original, "wb") as handle:
                remaining = length
                while True:
                    chunk = stream.read(min(1024 * 1024, remaining) if remaining is not None else 1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > self.settings.max_upload_bytes:
                        raise LimitExceeded("File is larger than the upload limit")
                    handle.write(chunk)
                    if remaining is not None:
                        remaining -= len(chunk)
                        if remaining <= 0:
                            break
        except BaseException:
            rmtree_quiet(folder)
            raise
        if written == 0:
            rmtree_quiet(folder)
            raise FileRejected("The file is empty")
        try:
            doc = self._register(doc_id, clean, original, allow_images)
        except BaseException:
            rmtree_quiet(folder)
            raise
        with self.lock:
            self.documents[doc.id] = doc
        return doc

    def add_existing(self, name: str, data: bytes) -> Document:
        import io
        return self.add_upload(name, io.BytesIO(data), len(data))

    def _register(self, doc_id: str, name: str, original: Path, allow_images: bool = False) -> Document:
        sniffed = sniff(original, name)
        fmt = sniffed["format"]
        warnings: list[str] = []
        if fmt is None:
            fmt = sniffed["claimed"]
            if fmt is None or (fmt in ("png", "jpeg") and not allow_images):
                raise FileRejected(f"'{name}' is not a document type Documaster can open"
                                   + (f" ({sniffed['note']})" if sniffed["note"] else ""),
                                   details={"note": sniffed["note"]})
            if fmt in ("docx", "odt", "pdf", "sldc"):
                raise FileRejected(f"'{name}' has a {label_for(fmt)} extension but its content is not a {label_for(fmt)} file "
                                   "(it may be damaged or a different format)")
        if sniffed["mismatch"]:
            warnings.append(f"The file is named like a {label_for(sniffed['claimed'])} file but its content is "
                            f"{label_for(fmt)}; it is treated as {label_for(fmt)}")
        if fmt in ("png", "jpeg") and not allow_images:
            raise UnsupportedOperation("Images are not documents. Add them to a PDF in the Merger instead.")
        ext = FORMATS[fmt].exts[0]
        final = original.with_name("rev-0" + ext)
        replace_file(original, final)
        stem = Path(name).stem
        shown = f"{stem}{Path(name).suffix}" if format_for_ext(name) == fmt else f"{stem}{ext}"
        rev = Revision(0, final, "Original", time.time(), final.stat().st_size, sha256_file(final))
        doc = Document(doc_id, shown, fmt, sniffed["claimed"], time.time(), [rev], warnings=warnings)
        self._inspect(doc)
        return doc

    def _inspect(self, doc: Document) -> None:
        """Format-specific facts: page count, encryption, permissions. Does not parse the whole document."""
        path = doc.path
        if doc.format == "pdf":
            import pypdf
            try:
                reader = pypdf.PdfReader(str(path), strict=False)
            except Exception as exc:  # noqa: BLE001
                raise FileRejected(f"This PDF could not be opened: {exc}") from exc
            if reader.is_encrypted:
                doc.encrypted = True
                try:
                    opened = reader.decrypt("")
                except Exception as exc:  # noqa: BLE001 - missing crypto package etc.
                    from .errors import DependencyMissing
                    raise DependencyMissing("This PDF is encrypted and the 'cryptography' package is needed to open it") from exc
                if opened == 0:
                    doc.locked = True
                    doc.warnings.append("This PDF is password-protected. Enter the password to open it.")
                    return
                doc.owner = opened == 2
                if not doc.owner:
                    doc.warnings.append("This PDF restricts some operations (its permissions); Documaster respects them "
                                        "unless the owner password is entered")
            try:
                doc.pages = len(reader.pages)
            except Exception as exc:  # noqa: BLE001
                raise FileRejected(f"This PDF's page tree is damaged: {exc}") from exc
            if doc.pages > self.settings.max_pdf_pages:
                raise LimitExceeded(f"This PDF has {doc.pages} pages; the limit is {self.settings.max_pdf_pages}")
            doc.permissions = pdf_permissions(reader)
            meta = reader.metadata
            doc.info = {"title": str(getattr(meta, "title", "") or "") if meta else "",
                        "author": str(getattr(meta, "author", "") or "") if meta else ""}
        elif doc.format in ("png", "jpeg"):
            try:
                from PIL import Image
                with Image.open(path) as im:
                    if im.width * im.height > 100_000_000:
                        raise LimitExceeded("That image is too large")
                    im.verify()
            except LimitExceeded:
                raise
            except Exception as exc:  # noqa: BLE001
                raise FileRejected("That image is damaged or not a valid PNG/JPEG") from exc
            doc.pages = 1
        elif doc.format == "sldc":
            s = sldc()
            try:
                report = s.inspect_package(path)
            except s.SLDCError as exc:
                raise FileRejected(f"This is not a valid SLDC document: {exc}") from exc
            doc.encrypted = report["container"] == "encrypted"
            doc.locked = doc.encrypted
            doc.info = {"container": report["container"], "version": report["version"], "requires": report["requires"],
                        "unsupportedRequires": report["unsupported_requires"]}
            if report["unsupported_requires"]:
                doc.warnings.append("This SLDC needs features this build cannot present faithfully: "
                                    + ", ".join(report["unsupported_requires"]))
            if doc.encrypted:
                doc.warnings.append("This SLDC document is encrypted. Enter the password to open it.")
        elif doc.format == "docx":
            import zipfile
            try:
                with zipfile.ZipFile(path) as z:
                    names = z.namelist()
                    if len(names) > 5000 or sum(i.file_size for i in z.infolist()) > 1024 ** 3:
                        raise FileRejected("This DOCX archive is larger than the safety limit")
                    if any(n.startswith("word/vbaProject") for n in names):
                        doc.warnings.append("This file contains macros. Documaster never runs macros.")
            except zipfile.BadZipFile as exc:
                raise FileRejected("This DOCX is damaged (not a valid ZIP archive)") from exc

    def unlock(self, doc_id: str, password: str) -> Document:
        doc = self.get(doc_id)
        if not doc.encrypted:
            return doc
        if doc.format == "pdf":
            import pypdf
            reader = pypdf.PdfReader(str(doc.path), strict=False)
            opened = reader.decrypt(password)
            if opened == 0:
                raise WrongPassword("That password does not open this PDF")
            doc.owner = opened == 2
            doc.password, doc.locked = password, False
            doc.warnings = [w for w in doc.warnings if "restricts some operations" not in w]
            doc.warnings = [w for w in doc.warnings if "password-protected" not in w]
            doc.pages = len(reader.pages)
            doc.permissions = pdf_permissions(reader)
            meta = reader.metadata
            doc.info = {"title": str(getattr(meta, "title", "") or "") if meta else "",
                        "author": str(getattr(meta, "author", "") or "") if meta else ""}
        elif doc.format == "sldc":
            s = sldc()
            try:
                s.read_package(doc.path, password=password)
            except s.SLDCDecryptionError as exc:
                raise WrongPassword("That password does not open this SLDC document") from exc
            doc.password, doc.locked = password, False
            doc.warnings = [w for w in doc.warnings if "encrypted" not in w]
        return doc

    def require_open(self, doc: Document) -> None:
        if doc.locked:
            raise PasswordRequired("This document is password-protected; unlock it first", details={"id": doc.id})

    # ------------------------------------------------------------------ the document model of an open document
    def ir(self, doc: Document, control: Any = None, *, ocr: bool = False, ocr_lang: str = "eng") -> Any:
        """The document read into the SLDC model (cached per revision). Passwords come from the unlocked document."""
        from .docmodel.readers import read_document
        from .services import NO_CONTROL
        self.require_open(doc)
        key = (doc.id, doc.revision.sha256, bool(ocr), ocr_lang if ocr else "")
        with self.lock:
            hit = self._ir.get(key)
        if hit is not None:
            return hit
        result = read_document(doc.path, doc.format, self.svc, password=doc.password, name=doc.name, ocr=ocr,
                               ocr_lang=ocr_lang, control=control or NO_CONTROL)
        with self.lock:
            if len(self._ir) > 12:
                self._ir.pop(next(iter(self._ir)))
            self._ir[key] = result
        return result

    # ------------------------------------------------------------------ image assets (stamps, signatures drawn by the user)
    def add_asset(self, name: str, data: bytes) -> Asset:
        if len(data) > 25 * 1024 * 1024:
            raise LimitExceeded("Images may be up to 25 MB")
        try:
            from PIL import Image
        except ImportError as exc:
            from .errors import DependencyMissing
            raise DependencyMissing("Placing images needs the 'Pillow' package") from exc
        try:
            with Image.open(io.BytesIO(data)) as im:
                if im.format not in ("PNG", "JPEG"):
                    raise FileRejected("Only PNG and JPEG images can be placed")
                width, height = im.size
                if width * height > 100_000_000:
                    raise LimitExceeded("That image is too large")
                im.verify()
                media = "image/png" if im.format == "PNG" else "image/jpeg"
        except (FileRejected, LimitExceeded):
            raise
        except Exception as exc:  # noqa: BLE001
            raise FileRejected("That file is not a valid PNG or JPEG image") from exc
        asset_id = uuid.uuid4().hex[:10]
        folder = self.root / "assets"
        folder.mkdir(exist_ok=True)
        path = folder / f"{asset_id}{'.png' if media == 'image/png' else '.jpg'}"
        path.write_bytes(data)
        asset = Asset(asset_id, path, safe_filename(name, "image"), media, width, height)
        with self.lock:
            self.assets[asset_id] = asset
        return asset

    def asset(self, asset_id: str) -> Asset:
        with self.lock:
            a = self.assets.get(asset_id)
        if a is None:
            raise NotFound("That image is not available; upload it again")
        return a

    # ------------------------------------------------------------------ revisions
    def add_revision(self, doc: Document, data_writer: Any, label: str) -> Revision:
        """Create revision N+1 from ``data_writer(path)``; drops any redo history. Keeps at most ``max_revisions``."""
        number = doc.revisions[-1].number + 1
        path = self.root / doc.id / f"rev-{number}{FORMATS[doc.format].exts[0]}"
        try:
            data_writer(path)
        except BaseException:
            try:
                path.unlink()
            except OSError:
                pass
            raise
        prev = doc.revisions[doc.cursor]
        rev = Revision(number, path, label, time.time(), path.stat().st_size, sha256_file(path), prev.password, prev.encrypted)
        with self.lock:
            dropped = doc.revisions[doc.cursor + 1:]
            doc.revisions = doc.revisions[:doc.cursor + 1] + [rev]
            doc.cursor = len(doc.revisions) - 1
            for old in dropped:
                try:
                    old.path.unlink()
                except OSError:
                    pass
            while len(doc.revisions) > self.settings.max_revisions:
                first = doc.revisions.pop(1) if len(doc.revisions) > 1 else None
                doc.cursor = max(1, doc.cursor - 1) if doc.cursor else 0
                if first:
                    try:
                        first.path.unlink()
                    except OSError:
                        pass
        return rev

    def step(self, doc: Document, delta: int) -> Document:
        target = doc.cursor + delta
        if not 0 <= target < len(doc.revisions):
            raise ValidationFailed("Nothing to " + ("undo" if delta < 0 else "redo"))
        doc.cursor = target
        self._inspect_pages(doc)
        return doc

    def _inspect_pages(self, doc: Document) -> None:
        if doc.format == "pdf":
            import pypdf
            reader = pypdf.PdfReader(str(doc.path), strict=False)
            if reader.is_encrypted:
                reader.decrypt(doc.password or "")
            doc.pages = len(reader.pages)

    # ------------------------------------------------------------------ outputs
    def register_output(self, path: Path, fmt: str, extras: list[str] | None = None) -> Output:
        out = Output(uuid.uuid4().hex[:10], path, path.name, fmt, path.stat().st_size, time.time(), extras or [])
        with self.lock:
            self.outputs[out.id] = out
        return out

    def output(self, out_id: str) -> Output:
        with self.lock:
            out = self.outputs.get(out_id)
        if out is None or not out.path.exists():
            raise NotFound("That output file is no longer available")
        return out

    def output_dir(self) -> Path:
        path = self.settings.output_dir
        path.mkdir(parents=True, exist_ok=True)
        if not os.access(path, os.W_OK):
            raise ValidationFailed(f"The output folder is not writable: {path}")
        return path


def pdf_permissions(reader: Any) -> dict[str, bool]:
    """What the PDF's own permission bits allow (owner-password restrictions)."""
    if not reader.is_encrypted:
        return {"print": True, "modify": True, "copy": True, "annotate": True, "fillForms": True, "assemble": True}
    try:
        flags = int(reader.user_access_permissions or 0)
    except Exception:  # noqa: BLE001
        return {"print": True, "modify": True, "copy": True, "annotate": True, "fillForms": True, "assemble": True}
    return {"print": bool(flags & (1 << 2)), "modify": bool(flags & (1 << 3)), "copy": bool(flags & (1 << 4)),
            "annotate": bool(flags & (1 << 5)), "fillForms": bool(flags & (1 << 8)), "assemble": bool(flags & (1 << 10))}
