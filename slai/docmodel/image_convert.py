"""Direct image/PDF conversions. Avoids passing raster images through a text document model."""
from __future__ import annotations

import io
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from ..errors import DependencyMissing, OutputFailed, UnsupportedOperation, ValidationFailed
from ..files import atomic_output, sha256_file
from ..formats import ext_for
from ..pdfops.render import page_sizes, render_page
from ..services import JobControl, Services

IMAGE_TYPES = {'png': 'PNG', 'jpeg': 'JPEG'}


def convert_images(svc: Services, source: Path, name: str, source_fmt: str, target: str,
                   out_dir: Path, control: JobControl, password: str | None) -> dict[str, Any]:
    from PIL import Image, ImageOps, UnidentifiedImageError
    from .convert import _free_stem

    if not ((source_fmt in IMAGE_TYPES and target in (*IMAGE_TYPES, 'pdf')) or
            (source_fmt == 'pdf' and target in IMAGE_TYPES)):
        raise UnsupportedOperation(f'Image conversion from {source_fmt} to {target} is not available')
    started = time.perf_counter()
    stem = _free_stem(out_dir, Path(name).stem or 'document', ext_for(target), False)
    paths: list[Path] = []
    pages: int | None = None
    try:
        if source_fmt in IMAGE_TYPES:
            try:
                with Image.open(source) as original:
                    original.verify()
                with Image.open(source) as original:
                    image = ImageOps.exif_transpose(original)
                    image.load()
                    if image.width * image.height > 100_000_000:
                        raise ValidationFailed('The image contains too many pixels')
                    if target == 'pdf':
                        from reportlab.lib.utils import ImageReader
                        from reportlab.pdfgen import canvas
                        # One image per page, at up to 150 DPI, while respecting aspect ratio.
                        scale = min(1, 1152 / max(image.width, image.height))
                        w, h = image.width * scale * 72 / 150, image.height * scale * 72 / 150
                        # A small image may need a minimum PDF page size, but
                        # padding the page must never stretch its aspect ratio.
                        pw, ph = max(w, 36), max(h, 36)
                        with atomic_output(out_dir, stem, '.pdf') as out:
                            c = canvas.Canvas(str(out.temp), pagesize=(pw, ph))
                            c.drawImage(ImageReader(image.convert('RGB')), (pw - w) / 2, (ph - h) / 2, w, h)
                            c.showPage()
                            c.save()
                            paths.append(out.commit())
                        pages = 1
                    else:
                        with atomic_output(out_dir, stem, ext_for(target)) as out:
                            if target == 'jpeg':
                                # JPEG has no transparency; composite onto white instead of turning it black.
                                rgba = image.convert('RGBA')
                                flattened = Image.new('RGB', rgba.size, 'white')
                                flattened.paste(rgba, mask=rgba.getchannel('A'))
                                flattened.save(out.temp, format='JPEG', quality=92, optimize=True)
                            else:
                                image.save(out.temp, format='PNG', optimize=True)
                            paths.append(out.commit())
            except (UnidentifiedImageError, OSError) as exc:
                raise ValidationFailed('Image data could not be decoded or converted') from exc
        else:
            info = page_sizes(source, password)
            pages = len(info)
            if pages < 1 or pages > svc.settings.max_pdf_pages:
                raise ValidationFailed('The PDF page count is outside the permitted range')
            with tempfile.TemporaryDirectory(prefix='dm-render-') as tmp:
                temp = Path(tmp)
                for i, item in enumerate(info):
                    control.check()
                    width = max(500, min(2000, int(item['width'] * 2)))
                    png, _, _ = render_page(source, i, width_px=width, password=password)
                    with Image.open(io.BytesIO(png)) as image:
                        image.load()
                        if target == 'jpeg':
                            converted = image.convert('RGB')
                            path = temp / f'page-{i + 1}.jpg'
                            converted.save(path, format='JPEG', quality=92, optimize=True)
                        else:
                            path = temp / f'page-{i + 1}.png'
                            path.write_bytes(png)
                    control.report((i + 1) / (2 * pages), f'Rendered page {i + 1} of {pages}')
                for i in range(pages):
                    control.check()
                    suffix = '' if i == 0 else f'-page-{i + 1}'
                    output_stem = _free_stem(out_dir, stem + suffix, ext_for(target), False)
                    with atomic_output(out_dir, output_stem, ext_for(target)) as out:
                        shutil.copyfile(temp / f'page-{i + 1}{ext_for(target)}', out.temp)
                        paths.append(out.commit())
                    control.report(0.5 + (i + 1) / (2 * pages), f'Saved page {i + 1} of {pages}')

        if not paths:
            raise OutputFailed('No images were produced')
        # Independently reopen each output before delivering any of them.
        for path in paths:
            if target == 'pdf':
                from pypdf import PdfReader
                if len(PdfReader(path).pages) != 1:
                    raise OutputFailed('Converted image PDF could not be reopened')
            else:
                with Image.open(path) as checked:
                    if checked.format != IMAGE_TYPES[target] or checked.width < 1 or checked.height < 1:
                        raise OutputFailed('Converted image failed format verification')
                    checked.verify()
        control.report(1, 'Verified images')
    except BaseException:
        for path in paths:
            path.unlink(missing_ok=True)
        raise

    primary = paths[0]
    return {
        'input': {'name': name, 'format': source_fmt, 'bytes': source.stat().st_size},
        'output': {'path': str(primary), 'name': primary.name, 'format': target, 'bytes': primary.stat().st_size,
                   'sha256': sha256_file(primary), 'pages': pages if target == 'pdf' else None,
                   'extras': [str(p) for p in paths[1:]]},
        'engine': 'raster', 'backend': {'read': 'Pillow' if source_fmt in IMAGE_TYPES else 'PDFium',
                                       'write': 'Pillow' if target in IMAGE_TYPES else 'ReportLab'},
        'kept': ['Image content and aspect ratio'], 'lost': ['Editable text and vector structure'] if source_fmt == 'pdf'
                else (['Transparency'] if target in ('jpeg', 'pdf') else []),
        'warnings': [f'Exported {pages} separate images (first image and supporting files)'] if source_fmt == 'pdf' and pages and pages > 1 else [],
        'ocrPages': [], 'verification': {'ok': True, 'checks': [
            {'name': 'Output parser', 'ok': True, 'detail': f'Reopened {len(paths)} generated file(s)'},
            {'name': 'Output formats', 'ok': True, 'detail': f'All files validated as {target.upper()}'}]},
        'durationMs': round((time.perf_counter() - started) * 1000),
    }
