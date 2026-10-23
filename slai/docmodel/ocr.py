"""Optional OCR through the Tesseract program. SLAI has no OCR of its own; this runs only when the binary is installed.

Pages that were recognised are always reported as such (``IRResult.ocr_pages`` and a provenance entry): recognised text
is a machine reading of an image and can contain errors.
"""
from __future__ import annotations

from ..pdfium_lock import pdfium_locked

import subprocess
import tempfile
from pathlib import Path

from ..errors import DependencyMissing, ValidationFailed
from ..services import Tools


def available(tools: Tools) -> bool:
    return bool(tools.tesseract) and tools.has("pypdfium2")


def requirement_message() -> str:
    return "OCR needs the Tesseract program (https://github.com/tesseract-ocr/tesseract) and the pypdfium2 package"


def check_language(tools: Tools, lang: str) -> str:
    parts = [p for p in (lang or "eng").replace(",", "+").split("+") if p]
    if not parts:
        parts = ["eng"]
    for part in parts:
        if not part.isalnum() and not part.replace("_", "").isalnum():
            raise ValidationFailed(f"Invalid OCR language {part!r}")
        if tools.tesseract_langs and part not in tools.tesseract_langs:
            raise DependencyMissing(f"Tesseract language data {part!r} is not installed",
                                    details={"installed": tools.tesseract_langs})
    return "+".join(parts)


@pdfium_locked
def render_page_png(pdf_path: Path, index: int, password: str | None, dpi: int = 220) -> bytes:
    import io
    import pypdfium2 as pdfium
    document = pdfium.PdfDocument(str(pdf_path), password=password)
    try:
        page = document[index]
        try:
            bitmap = page.render(scale=dpi / 72.0)
            image = bitmap.to_pil()
            buffer = io.BytesIO()
            image.convert("RGB").save(buffer, format="PNG")
            return buffer.getvalue()
        finally:
            page.close()
    finally:
        document.close()


def recognise_png(tools: Tools, png: bytes, lang: str, timeout: int) -> str:
    if not tools.tesseract:
        raise DependencyMissing(requirement_message())
    with tempfile.TemporaryDirectory(prefix="dm-ocr-") as tmp:
        image = Path(tmp) / "page.png"
        image.write_bytes(png)
        try:
            result = subprocess.run([tools.tesseract, str(image), "stdout", "-l", lang, "--psm", "3"],
                                    capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise DependencyMissing("Tesseract took too long on this page") from exc
        if result.returncode != 0:
            raise DependencyMissing("Tesseract failed: " + result.stderr.decode("utf-8", "replace")[:300])
        return result.stdout.decode("utf-8", "replace")


def recognise_pdf_page(tools: Tools, pdf_path: Path, index: int, password: str | None, lang: str,
                       timeout: int = 120) -> str:
    return recognise_png(tools, render_page_png(pdf_path, index, password), lang, timeout)
