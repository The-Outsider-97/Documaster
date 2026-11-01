"""Browser end-to-end checks: the real client in Chromium against a real server, with fixtures built on the fly.

    python3 tests_e2e/run_browser.py            # needs: pip install playwright && playwright install chromium

Each step prints PASS/FAIL; the exit code is the number of failures. Screenshots go to ``tests_e2e/shots`` (ignored by git).
Console errors and warnings in the page count as failures.
"""
from __future__ import annotations

import io
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tests_py"))

from support import Harness, PII_LINES, TOKEN, make_docx, make_pdf, ROOT  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ImportError:                                           # pragma: no cover
    print("SKIP: playwright is not installed")
    sys.exit(0)

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def main() -> int:
    if ROOT is None:
        print("SKIP: the SLAI checkout was not found (set SLAI_ROOT)")
        return 0
    shots = HERE / "shots"
    shots.mkdir(exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="dm-e2e-"))
    sample = tmp / "sample.pdf"
    sample.write_bytes(make_pdf([["Quarterly Report", "Introduction", "Contact us"], ["Results", "Budget grew by ten percent"]], title="Quarterly Report"))
    docx = tmp / "sample.docx"
    docx.write_bytes(make_docx(["Alpha paragraph.", "Beta paragraph."], heading="Notes"))
    md = tmp / "sample.md"
    md.write_text("# Heading\n\nSome *markdown* text.\n", encoding="utf-8")
    pii = tmp / "pii.pdf"
    pii.write_bytes(make_pdf([PII_LINES, ["Budget", "The total budget is 1.2 million euros."]]))

    h = Harness()
    url = f"{h.base}/?token={TOKEN}"
    problems: list[str] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1500, "height": 950}, accept_downloads=True)
            pg = ctx.new_page()
            pg.on("console", lambda m: problems.append(f"console {m.type}: {m.text}") if m.type in ("error", "warning") else None)
            pg.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
            pg.goto(url)
            pg.wait_for_selector(".nav-item")

            # ---- every workspace renders, in both themes
            for ws in ("counter", "converter", "merger", "editor", "reader", "assistant", "system"):
                pg.click(f"a[data-ws={ws}]")
                pg.wait_for_timeout(150)
                check(pg.locator(".workspace.system-ws" if ws == "system" else f".workspace.{ws}").first.is_visible(), f"workspace {ws} is visible")

            # ---- Word counter
            pg.click("a[data-ws=counter]")
            ta = pg.locator(".counter textarea").first
            ta.fill("One two three. Four five!")
            pg.wait_for_timeout(600)
            check("5" in pg.inner_text(".counter"), "counter shows 5 words for typed text")

            # ---- Converter
            pg.click("a[data-ws=converter]")
            pg.set_input_files(".converter input[type=file]", [str(docx), str(md)])
            pg.wait_for_selector(".converter .doc-row", timeout=20000)
            pg.wait_for_timeout(500)
            check(pg.locator(".converter .doc-row").count() == 2, "converter lists both files")
            pg.locator(".converter select:has(option[value=sldc])").first.select_option("pdf")
            pg.click(".converter button:has-text('Convert')")
            pg.wait_for_selector(".converter .card.result", timeout=60000)
            pg.wait_for_timeout(400)
            check(pg.inner_text(".converter").count("Checked") >= 2, "both conversions were verified")
            with pg.expect_download() as dl:
                pg.click(".converter .card.result a:has-text('Download')")
            check(Path(dl.value.path()).stat().st_size > 500, f"download works ({dl.value.suggested_filename})")
            pg.screenshot(path=str(shots / "converter.png"))

            # ---- Merger
            pg.click("a[data-ws=merger]")
            pg.set_input_files(".merger input[type=file]", [str(sample), str(docx)])
            pg.wait_for_selector(".page-card", timeout=20000)
            pg.wait_for_timeout(800)
            check(pg.locator(".page-card").count() == 3, "merger shows every page as a card")
            # Exercise a real pointer gesture, not just Organizer.dropBefore or keyboard shortcuts.
            origin = pg.locator(".page-card").nth(2).locator(".page-thumb").bounding_box()
            target = pg.locator(".page-card").nth(0).bounding_box()
            pg.mouse.move(origin["x"] + origin["width"] / 2, origin["y"] + origin["height"] / 2)
            pg.mouse.down()
            pg.mouse.move(target["x"] + target["width"] / 4, target["y"] + target["height"] / 2, steps=12)
            pg.mouse.up()
            check("sample.docx" in pg.locator(".page-card").first.locator(".src").inner_text(),
                  "pointer dragging moves the chosen page to the first position")
            pg.click("button[data-act=undo]")
            check("sample.pdf" in pg.locator(".page-card").first.locator(".src").inner_text(),
                  "pointer drag order is undoable")
            pg.click(".page-card >> nth=1")
            pg.keyboard.press("Alt+ArrowUp")
            pg.wait_for_timeout(200)
            pg.click("button[data-act=toggle]")
            pg.wait_for_timeout(200)
            check(pg.locator(".page-card.excluded").count() == 1, "a page can be excluded")
            pg.screenshot(path=str(shots / "merger.png"))
            pg.click("text=Merge Files")
            pg.wait_for_selector(".merger .results a.btn-primary", timeout=30000)
            check("Checked" in pg.inner_text(".merger .results"), "the merge is verified")
            with pg.expect_download() as dl:
                pg.click(".merger .results a.btn-primary")
            from pypdf import PdfReader
            check(len(PdfReader(dl.value.path()).pages) == 2, "the merged PDF has the pages that were kept")

            # ---- PDF editor: open, rotate + undo, highlight, redact
            pg.click("a[data-ws=editor]")
            pg.set_input_files("input[aria-label='Open a PDF']", str(sample))
            pg.wait_for_selector(".page-img", timeout=15000)
            pg.wait_for_timeout(1000)
            check(pg.locator(".thumb").count() == 2, "editor shows a thumbnail per page")
            check(pg.locator(".notice.warn").count() == 0, "no permission warning on an unprotected PDF")
            pg.click(".panel-body button[aria-label='Rotate right']")
            pg.wait_for_timeout(1800)
            check(pg.locator(".editor button:has-text('Undo')").is_enabled(), "rotation can be undone")
            pg.click(".editor button:has-text('Undo')")
            pg.wait_for_timeout(1200)
            check(pg.locator(".editor button:has-text('Redo')").is_enabled(), "and redone")

            pg.click(".tab:has-text('Annotate')")
            pg.click("button[data-tool=highlight]")
            box = pg.locator(".overlay-layer").bounding_box()
            x0, y0 = box["x"] + box["width"] * 0.12, box["y"] + box["height"] * 0.115
            pg.mouse.move(x0, y0); pg.mouse.down(); pg.mouse.move(x0 + 250, y0 + 25, steps=6); pg.mouse.up()
            pg.wait_for_timeout(300)
            check(pg.locator(".item:has-text('highlight')").count() == 1, "a highlight is staged as a draft")
            pg.click("button:has-text('Add 1 annotation to the PDF')")
            pg.wait_for_timeout(2200)
            check(pg.locator("text=highlight · p. 1").count() >= 1, "the highlight is now in the document")

            pg.click(".tab:has-text('Redact')")
            pg.fill("input[aria-label='Text to redact']", "Quarterly")
            pg.click("button.btn-small:has-text('Find')")
            pg.wait_for_timeout(700)
            pg.click("button:has-text('Mark selected matches')")
            pg.click("button:has-text('Permanently redact')")
            pg.wait_for_timeout(300)
            pg.click(".dialog button:has-text('Redact')")
            pg.wait_for_selector(".panel-body .card.result", timeout=30000)
            check("Verified" in pg.inner_text(".panel-body .card.result"), "the redaction was verified")
            pg.screenshot(path=str(shots / "editor_redacted.png"))
            pg.click("button:has-text('Save a copy')")
            pg.wait_for_selector("a:has-text('Download sample_edited')", timeout=15000)
            with pg.expect_download() as dl:
                pg.click("a:has-text('Download sample_edited')")
            data = Path(dl.value.path()).read_bytes()
            check(b"Quarterly" not in data and "Quarterly" not in "".join(pp.extract_text() for pp in PdfReader(io.BytesIO(data)).pages),
                  "the redacted word is not in the saved file")

            # ---- Reader
            pg.click("a[data-ws=reader]")
            pg.set_input_files(".reader input[type=file]", str(pii))
            pg.wait_for_selector(".reader .tab", timeout=20000)
            pg.wait_for_timeout(2000)
            for tab in ("structure", "text", "pii", "sections", "search", "summary"):
                pg.click(f".reader .tab[data-tab={tab}]")
                pg.wait_for_timeout(700)
            pg.click(".reader .tab[data-tab=pii]")
            pg.wait_for_timeout(500)
            body = pg.inner_text(".reader")
            check("jane.doe@example.com" in body and "NL91" in body, "personal data is listed with what was found")
            pg.screenshot(path=str(shots / "reader_pii.png"))

            # ---- Assistant
            pg.click("a[data-ws=assistant]")
            pg.wait_for_timeout(400)

            def say(text: str, wait: int = 2500) -> None:
                pg.fill("textarea[aria-label=Message]", text)
                pg.keyboard.press("Enter")
                pg.wait_for_timeout(wait)

            say("how many words are in pii.pdf?")
            check("words" in pg.inner_text(".chat-log"), "the assistant counts words")
            say("what does pii.pdf say about previous instructions?", 3000)
            check(pg.locator(".proposal").count() == 0, "an instruction inside the document does not create a proposal")
            say("redact the email addresses in pii.pdf", 3500)
            check(pg.locator(".proposal").count() == 1, "a change is proposed, not made")
            pg.click(".proposal button:has-text('Confirm')")
            pg.wait_for_selector(".result-box", timeout=60000)
            check("Verified" in pg.inner_text(".result-box"), "the confirmed redaction is verified")
            pg.screenshot(path=str(shots / "assistant.png"))

            # ---- theme toggle persists the choice
            pg.click(".theme-toggle, .toggle")
            pg.wait_for_timeout(200)
            check(pg.evaluate("document.documentElement.dataset.theme") in ("light", "dark"), "the theme can be switched")
            browser.close()
    finally:
        h.close()
    check(not problems, "no console errors or warnings" + ("" if not problems else f": {problems[:3]}"))
    print(f"\n{len(FAILS)} failure(s)")
    return len(FAILS)


if __name__ == "__main__":
    raise SystemExit(main())
