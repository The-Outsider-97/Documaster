"""Safe file handling: names, collision-free output, atomic writes, hashing, temp areas.

Nothing here overwrites an existing file. Outputs are written to a temporary file next to the
destination, flushed to disk and then moved into a name that was reserved exclusively, so a crash or
cancellation never leaves a half-written document under the final name.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from .errors import OutputFailed, ValidationFailed

_RESERVED = {"CON", "PRN", "AUX", "NUL", *{f"COM{i}" for i in range(1, 10)}, *{f"LPT{i}" for i in range(1, 10)}}
_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')


def safe_filename(name: str, fallback: str = "document", max_len: int = 120) -> str:
    """A file name that is valid on Windows, macOS and Linux and cannot escape its directory."""
    text = unicodedata.normalize("NFC", str(name or ""))
    text = text.replace("\\", "/").split("/")[-1]  # drop any directory part
    text = _BAD.sub("_", text).strip(" .")
    if not text:
        text = fallback
    stem, dot, ext = text.rpartition(".")
    if not dot:
        stem, ext = text, ""
    if stem.upper() in _RESERVED:
        stem = f"_{stem}"
    ext = ext[:16]
    stem = stem[: max(1, max_len - len(ext) - 1)].rstrip(" .") or fallback
    return f"{stem}.{ext}" if ext else stem


def split_name(name: str) -> tuple[str, str]:
    path = Path(name)
    return path.stem, path.suffix.lower()


def sha256_file(path: str | os.PathLike[str], chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def reserve_path(directory: Path, stem: str, ext: str, *, overwrite: bool = False) -> Path:
    """Exclusively create an empty placeholder named ``stem[ (n)]ext`` and return its path.

    ``overwrite=True`` reuses the exact name when it already exists (the caller has been told).
    """
    directory.mkdir(parents=True, exist_ok=True)
    stem = safe_filename(stem, "document", 100)
    ext = ext if ext.startswith(".") or not ext else f".{ext}"
    if overwrite:
        target = directory / f"{stem}{ext}"
        target.touch(exist_ok=True)
        return target
    for attempt in range(0, 10_000):
        name = f"{stem}{ext}" if attempt == 0 else f"{stem} ({attempt}){ext}"
        target = directory / name
        try:
            with open(target, "xb"):
                pass
            return target
        except FileExistsError:
            continue
        except OSError as exc:
            raise OutputFailed(f"Cannot create output in {directory}: {exc}") from exc
    raise OutputFailed("Could not find a free output name", details={"directory": str(directory)})


def replace_file(source: Path, destination: Path) -> None:
    """Move ``source`` onto ``destination`` in the same folder.

    ``os.replace`` is used first (atomic). Some Windows set-ups (virus scanners, sync clients, redirected folders) make it fail with
    errors such as "cannot move the file to a different drive" even inside one folder; then the content is copied and the source removed.
    """
    try:
        os.replace(source, destination)
        return
    except OSError as first:
        try:
            shutil.copyfile(source, destination)
        except OSError as exc:
            raise OSError(f"{first}; copy fallback also failed: {exc}") from exc
        try:                      # best effort: Windows only flushes handles that are open for writing
            with open(destination, "r+b") as handle:
                os.fsync(handle.fileno())
        except OSError:
            pass
    try:
        Path(source).unlink()
    except OSError:
        pass


class AtomicOutput:
    """Reserve a destination, hand out a temp path to write to, then commit or discard."""

    def __init__(self, directory: Path, stem: str, ext: str, *, overwrite: bool = False) -> None:
        self.destination = reserve_path(directory, stem, ext, overwrite=overwrite)
        fd, name = tempfile.mkstemp(prefix=".dm-", suffix=self.destination.suffix, dir=str(directory))
        os.close(fd)
        self.temp = Path(name)
        self.committed = False

    def commit(self) -> Path:
        try:
            with open(self.temp, "rb") as handle:
                os.fsync(handle.fileno())
        except OSError:
            pass
        replace_file(self.temp, self.destination)
        self.committed = True
        return self.destination

    def discard(self) -> None:
        for path in (self.temp, self.destination if not self.committed else None):
            if path is not None:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass


@contextmanager
def atomic_output(directory: Path, stem: str, ext: str, *, overwrite: bool = False) -> Iterator[AtomicOutput]:
    """``with atomic_output(dir, "name", ".pdf") as out: write(out.temp); out.commit()``.

    Leaving the block without ``commit()`` (an exception, cancellation) removes the temp file and the
    reserved placeholder; the destination never exists half-written.
    """
    out = AtomicOutput(directory, stem, ext, overwrite=overwrite)
    try:
        yield out
        if not out.committed:
            out.commit()
    except BaseException:
        out.discard()
        raise


def write_atomic(directory: Path, stem: str, ext: str, data: bytes, *, overwrite: bool = False) -> Path:
    with atomic_output(directory, stem, ext, overwrite=overwrite) as out:
        out.temp.write_bytes(data)
        return out.commit()


def write_with(directory: Path, stem: str, ext: str, writer: Callable[[Path], None], *,
               overwrite: bool = False) -> Path:
    with atomic_output(directory, stem, ext, overwrite=overwrite) as out:
        writer(out.temp)
        return out.commit()


def ensure_inside(base: Path, candidate: Path) -> Path:
    """Resolve ``candidate`` and require it to live under ``base`` (path-traversal guard)."""
    base_r, cand_r = base.resolve(), candidate.resolve()
    try:
        cand_r.relative_to(base_r)
    except ValueError as exc:
        raise ValidationFailed("Path escapes its directory") from exc
    return cand_r


def rmtree_quiet(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def free_bytes(path: Path) -> int | None:
    try:
        return shutil.disk_usage(path if path.exists() else path.parent).free
    except OSError:
        return None
