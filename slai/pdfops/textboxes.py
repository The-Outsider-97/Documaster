"""Movable Documaster text boxes stored as standard PDF FreeText annotations.

Each box owns a real PDF appearance stream.  Its id, geometry and styling survive
save/reopen; moving it regenerates the appearance without touching page content.
Ordinary source-PDF text and third-party annotations are never made editable.
"""
from __future__ import annotations

import io
import math
import uuid
from typing import Any

from ..errors import ValidationFailed
from .geometry import clamp_rect, from_user, geometry_of, hex_colour, rect_to_user
from .stamp import pick_font


def _appearance(writer: Any, text: str, size: float, color: tuple[float, float, float],
                box: tuple[float, float, float, float], rotation: int) -> Any:
    from pypdf import PdfReader
    from pypdf.generic import ArrayObject, DecodedStreamObject, FloatObject, NameObject
    from reportlab.lib.utils import simpleSplit
    from reportlab.pdfgen import canvas

    x0, y0, x1, y1 = box
    pw, ph = x1 - x0, y1 - y0
    dw, dh = (ph, pw) if rotation in (90, 270) else (pw, ph)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(pw, ph), pageCompression=0)
    if rotation == 90:
        c.translate(pw, 0)
        c.rotate(90)
    elif rotation == 180:
        c.translate(pw, ph)
        c.rotate(180)
    elif rotation == 270:
        c.translate(0, ph)
        c.rotate(270)
    font = pick_font(text)
    c.setFillColorRGB(*color)
    c.setFont(font, size)
    y = dh - size
    for paragraph in text.split('\n'):
        for line in simpleSplit(paragraph, font, size, max(dw - 4, 10)) or ['']:
            if y < 0:
                break
            c.drawString(2, y, line)
            y -= size * 1.2
    c.showPage()
    c.save()
    page = PdfReader(io.BytesIO(buf.getvalue())).pages[0]
    stream = DecodedStreamObject()
    stream.set_data(page.get_contents().get_data())
    resources = page['/Resources'].clone(writer)
    stream.update({
        NameObject('/Type'): NameObject('/XObject'),
        NameObject('/Subtype'): NameObject('/Form'),
        NameObject('/BBox'): ArrayObject([FloatObject(v) for v in (0, 0, pw, ph)]),
        NameObject('/Resources'): resources,
    })
    return writer._add_object(stream)


def _check(text: Any, size: Any, color: Any, rect: Any) -> tuple[str, float, tuple[float, float, float], list[float]]:
    if not isinstance(text, str) or not text.strip() or len(text) > 2000:
        raise ValidationFailed('Text boxes require 1-2000 characters of text')
    try:
        size = float(size)
        if not math.isfinite(size) or not 4 <= size <= 200:
            raise ValueError('size')
        rgb = hex_colour(color, (0.07, 0.09, 0.15))
        if not all(math.isfinite(c) and 0 <= c <= 1 for c in rgb):
            raise ValueError('color')
        r = clamp_rect(rect)
    except (ValueError, TypeError, OverflowError, KeyError) as exc:
        raise ValidationFailed('Invalid text box geometry, size or color') from exc
    return text, size, rgb, r


def add(writer: Any, page: int, rect: Any, text: Any, size: Any = 12, color: Any = '#111827') -> str:
    from pypdf.generic import ArrayObject, DictionaryObject, FloatObject, NameObject, NumberObject, TextStringObject
    if not isinstance(page, int) or not 0 <= page < len(writer.pages):
        raise ValidationFailed('The text box page does not exist')
    text, size, rgb, r = _check(text, size, color, rect)
    p = writer.pages[page]
    g = geometry_of(p)
    box = rect_to_user(r, g)
    ident = 'dm-text-' + uuid.uuid4().hex[:16]
    a = DictionaryObject({
        NameObject('/Type'): NameObject('/Annot'),
        NameObject('/Subtype'): NameObject('/FreeText'),
        NameObject('/NM'): TextStringObject(ident),
        NameObject('/T'): TextStringObject('Documaster'),
        NameObject('/Contents'): TextStringObject(text),
        NameObject('/F'): NumberObject(4),
        NameObject('/Rect'): ArrayObject([FloatObject(v) for v in box]),
        NameObject('/C'): ArrayObject([FloatObject(v) for v in rgb]),
        NameObject('/DMTextSize'): FloatObject(size),
        NameObject('/DMTextColor'): ArrayObject([FloatObject(v) for v in rgb]),
        NameObject('/AP'): DictionaryObject({NameObject('/N'): _appearance(writer, text, size, rgb, box, g.rotate)}),
    })
    # /DA permits ordinary PDF tools to display the text if they regenerate an appearance.
    a[NameObject('/DA')] = TextStringObject(f'{rgb[0]:.4f} {rgb[1]:.4f} {rgb[2]:.4f} rg /Helv {size:g} Tf')
    if '/Annots' not in p:
        p[NameObject('/Annots')] = ArrayObject()
    p['/Annots'].append(writer._add_object(a))
    return ident


def listing(reader: Any) -> list[dict[str, Any]]:
    found = []
    for pi, page in enumerate(reader.pages):
        g = geometry_of(page)
        for ref in page.get('/Annots', []):
            a = ref.get_object()
            ident = str(a.get('/NM', ''))
            if not ident.startswith('dm-text-') or a.get('/Subtype') != '/FreeText':
                continue
            try:
                x0, y0, x1, y1 = (float(v) for v in a['/Rect'])
                points = [from_user(x, y, g) for x in (x0, x1) for y in (y0, y1)]
                r = [min(q[0] for q in points), min(q[1] for q in points),
                     max(q[0] for q in points), max(q[1] for q in points)]
                rgb = [float(c) for c in a.get('/DMTextColor', [0.07, 0.09, 0.15])]
                color = '#' + ''.join(f'{min(255, max(0, round(c * 255))):02x}' for c in rgb)
                found.append({'id': ident, 'page': pi, 'rect': r, 'text': str(a.get('/Contents', '')),
                              'size': float(a.get('/DMTextSize', 12)), 'color': color})
            except (ValueError, TypeError, KeyError):
                continue
    return found


def move(writer: Any, ident: str, rect: Any) -> bool:
    from pypdf.generic import ArrayObject, DictionaryObject, FloatObject, NameObject
    if not isinstance(ident, str) or not ident.startswith('dm-text-'):
        raise ValidationFailed('Only text boxes added by Documaster can be moved')
    try:
        r = clamp_rect(rect)
    except (TypeError, ValueError) as exc:
        raise ValidationFailed('Invalid text box position') from exc
    for page in writer.pages:
        for ref in page.get('/Annots', []):
            a = ref.get_object()
            if str(a.get('/NM', '')) != ident:
                continue
            if a.get('/Subtype') != '/FreeText':
                raise ValidationFailed('This object is not an editable Documaster text box')
            g = geometry_of(page)
            box = rect_to_user(r, g)
            previous = [float(v) for v in a['/Rect']]
            if all(abs(x - y) < 0.001 for x, y in zip(box, previous)):
                return False
            size = float(a.get('/DMTextSize', 12))
            rgb = tuple(float(v) for v in a.get('/DMTextColor', [0.07, 0.09, 0.15]))
            text = str(a.get('/Contents', ''))
            a[NameObject('/Rect')] = ArrayObject([FloatObject(v) for v in box])
            a[NameObject('/AP')] = DictionaryObject({NameObject('/N'): _appearance(writer, text, size, rgb, box, g.rotate)})
            return True
    raise ValidationFailed('That editable text box no longer exists')


def edit(writer: Any, ident: str, value: Any) -> bool:
    from pypdf.generic import DictionaryObject, NameObject, TextStringObject
    if not isinstance(ident, str) or not ident.startswith('dm-text-'):
        raise ValidationFailed('Only Documaster text boxes can be edited')
    for page in writer.pages:
        for ref in page.get('/Annots', []):
            ann = ref.get_object()
            if ann.get('/Subtype') != '/FreeText' or str(ann.get('/NM', '')) != ident:
                continue
            old = str(ann.get('/Contents', ''))
            size = float(ann.get('/DMTextSize', 12))
            rgb = tuple(float(c) for c in ann.get('/DMTextColor', [0.07, 0.09, 0.15]))
            box = tuple(float(v) for v in ann['/Rect'])
            new, _, _, _ = _check(value, size, rgb, [0, 0, 1, 1])
            if old == new:
                return False
            ann[NameObject('/Contents')] = TextStringObject(new)
            ann[NameObject('/AP')] = DictionaryObject({NameObject('/N'): _appearance(writer, new, size, rgb, box, geometry_of(page).rotate)})
            return True
    raise ValidationFailed('That editable text box no longer exists')
