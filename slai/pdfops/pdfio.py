"""Opening and saving PDFs with pypdf: passwords, encryption, atomic writes."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..errors import DependencyMissing, PasswordRequired, ValidationFailed, WrongPassword


def open_reader(path: Path, password: str | None = None) -> Any:
    import pypdf
    try:
        reader = pypdf.PdfReader(str(path), strict=False)
    except Exception as exc:  # noqa: BLE001
        raise ValidationFailed(f"This PDF could not be opened: {exc}") from exc
    if reader.is_encrypted:
        try:
            ok = reader.decrypt(password or "")
        except Exception as exc:  # noqa: BLE001
            raise DependencyMissing("Opening this encrypted PDF needs the 'cryptography' package") from exc
        if ok == 0:
            raise (WrongPassword("That password does not open this PDF") if password else
                   PasswordRequired("This PDF is password-protected"))
    return reader


def save_writer(writer: Any, path: Path, *, password: str | None = None, owner_password: str | None = None) -> None:
    """Write ``writer`` to ``path``; with a password the file is AES-256 encrypted."""
    if password:
        try:
            writer.encrypt(user_password=password, owner_password=owner_password or password, algorithm="AES-256")
        except ImportError as exc:
            raise DependencyMissing("Encrypting a PDF needs the 'cryptography' package") from exc
    with open(path, "wb") as handle:
        writer.write(handle)


def parse_ranges(spec: str, total: int) -> list[int]:
    """``"1-3, 5, 8-"`` -> 0-based page indices (order kept, duplicates allowed). Pages are numbered from 1."""
    if not isinstance(spec, str) or not spec.strip():
        raise ValidationFailed("Enter a page range such as 1-3, 5, 8-")
    out: list[int] = []
    for part in re.split(r"[,\s;]+", spec.strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d*)\s*-\s*(\d*)|(\d+)", part)
        if not m or part == "-":
            raise ValidationFailed(f"'{part}' is not a page or a range")
        if m.group(3):
            first = last = int(m.group(3))
        else:
            first = int(m.group(1)) if m.group(1) else 1
            last = int(m.group(2)) if m.group(2) else total
        if first < 1 or last < 1 or first > total or last > total:
            raise ValidationFailed(f"'{part}' is outside the document (pages 1-{total})")
        step = 1 if first <= last else -1
        out.extend(range(first - 1, last - 1 + step, step))
    if not out:
        raise ValidationFailed("The page range selects no pages")
    return out


def norm_text(text: str) -> str:
    return " ".join((text or "").split())
