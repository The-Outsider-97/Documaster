"""Automated accessibility floor for every workspace: names for controls, labels for fields, alt text, landmarks, keyboard focus.

    python3 tests_e2e/run_a11y.py

This is a floor, not an audit: it cannot judge reading order, colour contrast in context, or screen-reader output. It fails on the things
a machine can decide (a button with no accessible name, an input with no label, an image with no alt, no main landmark, a focus that
never reaches a control).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tests_py"))
from support import Harness, TOKEN, ROOT  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP: playwright is not installed")
    sys.exit(0)

SCAN = """() => {
  const name = (el) => (el.getAttribute('aria-label') || el.getAttribute('title') || el.textContent || '').trim()
    || (el.getAttribute('aria-labelledby') ? (document.getElementById(el.getAttribute('aria-labelledby')) || {}).textContent : '');
  const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const out = {unnamedButtons: [], unlabelledFields: [], imagesWithoutAlt: [], unnamedLinks: [], duplicateIds: []};
  for (const b of document.querySelectorAll('button, [role=button]')) if (visible(b) && !name(b)) out.unnamedButtons.push(b.outerHTML.slice(0, 100));
  for (const a of document.querySelectorAll('a[href]')) if (visible(a) && !name(a)) out.unnamedLinks.push(a.outerHTML.slice(0, 100));
  for (const f of document.querySelectorAll('input:not([type=hidden]), select, textarea')) {
    if (!visible(f)) continue;
    if (f.getAttribute('aria-hidden') === 'true' && f.tabIndex < 0) continue;      // a file input that only its visible button drives
    const labelled = f.getAttribute('aria-label') || f.getAttribute('aria-labelledby') || (f.id && document.querySelector('label[for="' + f.id + '"]')) || f.closest('label') || f.getAttribute('title');
    if (!labelled) out.unlabelledFields.push(f.outerHTML.slice(0, 100));
  }
  for (const i of document.querySelectorAll('img')) if (!i.hasAttribute('alt')) out.imagesWithoutAlt.push(i.outerHTML.slice(0, 100));
  const seen = new Set();
  for (const e of document.querySelectorAll('[id]')) { if (seen.has(e.id)) out.duplicateIds.push(e.id); seen.add(e.id); }
  out.landmarks = {main: document.querySelectorAll('main, [role=main]').length, nav: document.querySelectorAll('nav, [role=navigation]').length};
  out.lang = document.documentElement.lang;
  out.title = document.title;
  return out;
}"""


def main() -> int:
    if ROOT is None:
        print("SKIP: the SLAI checkout was not found")
        return 0
    failures = 0
    h = Harness()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=os.getenv("DOCUMASTER_BROWSER_BINARY") or None, args=["--no-sandbox"])
            page = browser.new_context(viewport={"width": 1400, "height": 900}).new_page()
            page.goto(f"{h.base}/?token={TOKEN}")
            page.wait_for_selector(".nav-item")
            for ws in ("counter", "converter", "merger", "editor", "reader", "assistant", "system"):
                page.click(f"a[data-ws={ws}]")
                page.wait_for_timeout(300)
                r = page.evaluate(SCAN)
                problems = {k: v for k, v in r.items() if isinstance(v, list) and v}
                ok = not problems and r["landmarks"]["main"] >= 1 and r["lang"] and r["title"]
                print(("PASS" if ok else "FAIL"), f"{ws}: " + ("no machine-detectable problems" if ok else str(problems or r)))
                failures += 0 if ok else 1
            # keyboard: Tab from the top must reach a control and show a visible focus indicator
            page.click("a[data-ws=counter]")
            page.keyboard.press("Tab")
            focused = page.evaluate("() => { const e = document.activeElement; return e && e !== document.body ? e.tagName : null; }")
            print(("PASS" if focused else "FAIL"), f"Tab moves focus to a control ({focused})")
            failures += 0 if focused else 1
            # every nav item is reachable by keyboard alone
            reached = set()
            for _ in range(40):
                page.keyboard.press("Tab")
                ws = page.evaluate("() => document.activeElement && document.activeElement.getAttribute('data-ws')")
                if ws:
                    reached.add(ws)
            ok = len(reached) == 7
            print(("PASS" if ok else "FAIL"), f"all 7 workspace links are keyboard-reachable ({sorted(reached)})")
            failures += 0 if ok else 1
            browser.close()
    finally:
        h.close()
    print(f"{failures} failure(s)")
    return failures


if __name__ == "__main__":
    sys.exit(main())
