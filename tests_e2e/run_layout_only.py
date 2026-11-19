"""Browser check: an Excel workbook dropped in the Converter offers only PDF, converts through LibreOffice and downloads.

    python3 tests_e2e/run_layout_only.py      # needs Playwright + Chromium, openpyxl and LibreOffice; SKIPs (exit 0) when one is missing
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tests_py"))
from support import Harness, TOKEN, ROOT  # noqa: E402

try:
    import openpyxl
    from playwright.sync_api import expect, sync_playwright
except ImportError as exc:
    print(f"SKIP: {exc.name} is not installed")
    sys.exit(0)
from slai.services import Tools  # noqa: E402


def main() -> int:
    if ROOT is None or not Tools.discover().soffice:
        print("SKIP: needs the SLAI checkout and LibreOffice")
        return 0
    shots = HERE / "shots"
    shots.mkdir(exist_ok=True)
    wb = openpyxl.Workbook()
    wb.active.append(["Region", "Quarter one"])
    wb.active.append(["Northland", 10])
    with tempfile.TemporaryDirectory(prefix="dm-layout-e2e-") as temp:
        xlsx = Path(temp) / "sales.xlsx"
        wb.save(xlsx)
        h = Harness()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(executable_path=os.getenv("DOCUMASTER_BROWSER_BINARY") or None, args=["--no-sandbox"])
                ctx = browser.new_context(viewport={"width": 1400, "height": 900}, accept_downloads=True)
                page = ctx.new_page()
                problems = []
                page.on("pageerror", lambda e: problems.append(str(e)))
                page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None)
                page.goto(f"{h.base}/?token={TOKEN}")
                page.wait_for_selector(".nav-item")
                page.click("a[data-ws=converter]")
                page.set_input_files(".converter input[type=file]", [str(xlsx)])
                expect(page.locator(".converter .convert-row")).to_have_count(1, timeout=20000)
                page.locator(".converter .options select").first.select_option("individual")      # per-file targets
                options = page.locator(".converter .convert-row").first.locator("select option").all_text_contents()
                assert any("PDF" in o for o in options), options
                assert not any("Word" in o or "Markdown" in o for o in options), options
                print("PASS an Excel file offers PDF and no model-only target")
                page.get_by_role("button", name="Convert All").click()
                expect(page.locator(".converter .card.result:not(.bad)")).to_have_count(1, timeout=120000)
                with page.expect_download(timeout=30000) as got:
                    page.locator('.converter .card.result a:has-text("Download")').first.click()
                data = Path(got.value.path()).read_bytes()
                assert data.startswith(b"%PDF-"), data[:8]
                from pypdf import PdfReader
                text = "".join(pg.extract_text() or "" for pg in PdfReader(io.BytesIO(data)).pages)
                assert "Northland" in text, text
                page.screenshot(path=str(shots / "layout-only-converter.png"))
                print("PASS the workbook converted to a PDF that contains its cell text")
                assert not problems, problems
                print("PASS no console errors or warnings")
                browser.close()
        finally:
            h.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
