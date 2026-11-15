"""Live Chromium interaction checks for mixed conversion and persistent PDF text dragging.

    SLAI_ROOT=/path/to/SLAI python3 tests_e2e/run_batch_text.py
Set DOCUMASTER_BROWSER_BINARY when Playwright's bundled Chromium is unavailable.
"""
from __future__ import annotations

import os
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'tests_py'))
from support import Harness, TOKEN, ROOT, make_docx, make_pdf  # noqa: E402

from playwright.sync_api import sync_playwright
from pypdf import PdfReader
from slai.pdfops.textboxes import listing


def main() -> int:
    if ROOT is None:
        print('SKIP: SLAI_ROOT must point to an SLAI checkout')
        return 0
    shots = HERE / 'shots'
    shots.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='dm-batch-e2e-') as temp:
        base = Path(temp)
        pdf = base / 'source.pdf'; pdf.write_bytes(make_pdf([['Original PDF body']], title='Original'))
        docx = base / 'source.docx'; docx.write_bytes(make_docx(['This is a Word document.']))
        txt = base / 'source.txt'; txt.write_text('Source text for conversion.', encoding='utf-8')
        h = Harness()
        try:
            with sync_playwright() as p:
                executable = os.getenv('DOCUMASTER_BROWSER_BINARY')
                browser = p.chromium.launch(executable_path=executable or None, args=['--no-sandbox'])
                ctx = browser.new_context(viewport={'width': 1560, 'height': 1000}, accept_downloads=True)
                page = ctx.new_page()
                errors = []
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.goto(f'{h.base}/?token={TOKEN}')
                page.wait_for_selector('.nav-item')

                page.click('a[data-ws=converter]')
                page.set_input_files('.converter input[type=file]', [str(docx), str(pdf), str(txt)])
                page.wait_for_function('document.querySelectorAll(".converter .convert-row").length === 3', timeout=20000)
                page.locator('.converter .options select').first.select_option('individual')
                rows = page.locator('.converter .convert-row')
                rows.nth(0).locator('select').select_option('pdf')  # Word -> PDF
                rows.nth(1).locator('select').select_option('txt')  # PDF -> text
                rows.nth(2).locator('select').select_option('pdf')  # text -> PDF
                page.get_by_role('button', name='Convert All').click()
                page.wait_for_function('document.querySelectorAll(".converter .card.result:not(.bad)").length === 3', timeout=90000)
                assert page.locator('.converter .card.result:not(.bad)').count() == 3
                assert page.locator('.converter .card.result.bad').count() == 0
                with page.expect_download(timeout=30000) as downloaded:
                    page.get_by_role('button', name='Download as ZIP').click()
                archive_path = downloaded.value.path()
                with zipfile.ZipFile(archive_path) as z:
                    assert z.testzip() is None
                    assert len(z.namelist()) == 3
                    assert sum(name.endswith('.pdf') for name in z.namelist()) == 2
                with page.expect_download() as single:
                    page.locator('.converter .card.result a:has-text("Download")').first.click()
                assert Path(single.value.path()).stat().st_size > 100
                page.screenshot(path=str(shots / 'batch-converter.png'))
                print('PASS mixed per-file conversion, ZIP and individual download')

                page.click('a[data-ws=editor]')
                page.set_input_files("input[aria-label='Open a PDF']", str(pdf))
                page.wait_for_selector('.page-img[src]', timeout=20000)
                page.click('.tab[data-panel=stamp]')
                page.click('button[data-tool=textbox]')
                overlay = page.locator('.overlay-layer').bounding_box()
                assert overlay is not None
                x = overlay['x'] + overlay['width'] * .1
                y = overlay['y'] + overlay['height'] * .3
                page.mouse.move(x, y)
                page.mouse.down()
                page.mouse.move(x + overlay['width'] * .35, y + overlay['height'] * .11, steps=10)
                page.mouse.up()
                page.wait_for_selector('.dialog textarea')
                page.locator('.dialog textarea').fill('Movable Documaster text')
                page.locator('.dialog button:has-text("OK")').click()
                page.wait_for_selector('.staged.textbox')
                staged = page.locator('.staged.textbox').bounding_box()
                assert staged is not None
                page.mouse.move(staged['x'] + 20, staged['y'] + 10)
                page.mouse.down()
                page.mouse.move(staged['x'] + 90, staged['y'] + 45, steps=8)
                page.mouse.up()
                page.wait_for_timeout(250)
                shifted = page.locator('.staged.textbox').bounding_box()
                assert shifted is not None and shifted['x'] > staged['x'] + 40
                page.get_by_role('button', name='Add text boxes to the PDF').click()
                page.wait_for_selector('.editable-textbox', timeout=30000)
                before = page.locator('.editable-textbox').bounding_box()
                assert before is not None
                page.mouse.move(before['x'] + 20, before['y'] + 12)
                page.mouse.down()
                page.mouse.move(before['x'] + 100, before['y'] + 12, steps=10)
                page.mouse.up()
                page.wait_for_timeout(1500)
                after = page.locator('.editable-textbox').bounding_box()
                assert after is not None and after['x'] > before['x'] + 35
                page.screenshot(path=str(shots / 'textboxes.png'))
                page.get_by_role('button', name='Save a copy').click()
                page.wait_for_selector('.editor .saved a', timeout=30000)
                with page.expect_download() as saved:
                    page.locator('.editor .saved a').click()
                reader = PdfReader(saved.value.path())
                boxes = listing(reader)
                assert len(boxes) == 1
                assert boxes[0]['text'] == 'Movable Documaster text'
                assert boxes[0]['rect'][0] > .2
                print('PASS text placement, staged drag, saved-object drag and PDF export')
                assert not errors, errors
                browser.close()
        finally:
            h.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
