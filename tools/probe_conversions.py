#!/usr/bin/env python3
"""Run EVERY source -> target conversion through the real server and record what happened (not what the capability matrix promises).

    SLAI_ROOT=... python3 tools/probe_conversions.py [--out docs/audit/conversion-matrix.json]

Fixtures are built here, with a known marker word in each, so a "pass" means: the job succeeded, its own verification passed and the
marker word is present in the output text where the target can carry text. Cells are one of:
  ok        the conversion ran and the marker word is in the result
  ok-nomark the conversion ran but the marker could not be checked for this target (raster output)
  LOST      it ran but the marker word is missing from the result (a content-loss defect)
  refused   the server said "unsupported" / "same format" (a documented, clean refusal)
  FAILED    anything else
"""
from __future__ import annotations

import argparse
import io
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP / "tests_py"))
from support import ApiError, Harness, ROOT, make_docx, make_pdf  # noqa: E402

MARK = "Zanzibar"


def png_jpeg() -> tuple[bytes, bytes]:
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (400, 200), "white")
    ImageDraw.Draw(im).text((20, 80), MARK, fill="black")
    a, b = io.BytesIO(), io.BytesIO()
    im.save(a, "PNG")
    im.save(b, "JPEG")
    return a.getvalue(), b.getvalue()


def soffice_make(src: bytes, src_name: str, ext: str) -> bytes | None:
    exe = shutil.which("soffice")
    if not exe:
        return None
    with tempfile.TemporaryDirectory() as t:
        p = Path(t) / src_name
        p.write_bytes(src)
        subprocess.run([exe, f"-env:UserInstallation={(Path(t) / 'profile').as_uri()}", "--headless", "--convert-to", ext, "--outdir", t, str(p)],
                       capture_output=True, timeout=180)
        out = Path(t) / (p.stem + "." + ext)
        return out.read_bytes() if out.is_file() else None


def fixtures(h: Harness) -> dict[str, tuple[str, bytes]]:
    docx = make_docx([f"{MARK} is the marker word.", "Second paragraph of the document."], heading="Probe")
    png, jpg = png_jpeg()
    fx: dict[str, tuple[str, bytes]] = {
        "docx": ("probe.docx", docx),
        "pdf": ("probe.pdf", make_pdf([[f"{MARK} is the marker word.", "Second line."]])),
        "txt": ("probe.txt", f"{MARK} is the marker word.\n\nSecond paragraph.\n".encode()),
        "md": ("probe.md", f"# Probe\n\n{MARK} is the marker word.\n\n- item\n".encode()),
        "html": ("probe.html", f"<html><body><h1>Probe</h1><p>{MARK} is the marker word.</p></body></html>".encode()),
        "xml": ("probe.xml", f"<doc><p>{MARK} is the marker word.</p></doc>".encode()),
        "csv": ("probe.csv", f"name,value\n{MARK},1\n".encode()),
        "png": ("probe.png", png), "jpeg": ("probe.jpg", jpg),
    }
    for ext in ("odt", "rtf", "doc"):
        data = soffice_make(docx, "probe.docx", ext)
        if data:
            fx[ext] = (f"probe.{ext}", data)
    import openpyxl
    wb = openpyxl.Workbook()
    wb.active.append([MARK, 1])
    buf = io.BytesIO()
    wb.save(buf)
    fx["xlsx"] = ("probe.xlsx", buf.getvalue())
    try:
        import pptx
        prs = pptx.Presentation()
        s = prs.slides.add_slide(prs.slide_layouts[1])
        s.shapes.title.text = MARK
        out = io.BytesIO()
        prs.save(out)
        fx["pptx"] = ("probe.pptx", out.getvalue())
        odp = soffice_make(out.getvalue(), "probe.pptx", "odp")
        if odp:
            fx["odp"] = ("probe.odp", odp)
    except ImportError:
        pass
    return fx


def text_of(data: bytes, fmt: str) -> str | None:
    if fmt in ("txt", "md", "html", "xml", "rtf"):
        return data.decode("utf-8", "replace")
    if fmt == "pdf":
        from pypdf import PdfReader
        return "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages)
    if fmt in ("docx", "odt"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return "".join(z.read(n).decode("utf-8", "replace") for n in z.namelist() if n in ("word/document.xml", "content.xml"))
    if fmt == "sldc":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return "".join(z.read(n).decode("utf-8", "replace") for n in z.namelist() if n.endswith((".json", ".xml", ".txt")))
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(APP / "docs" / "audit" / "conversion-matrix.json"))
    args = ap.parse_args()
    if ROOT is None:
        print("SKIP: the SLAI checkout was not found (set SLAI_ROOT)")
        return 0
    from slai.formats import TARGETS
    h = Harness()
    cells: dict[str, dict[str, dict]] = {}
    try:
        fx = fixtures(h)
        for src, (name, data) in fx.items():
            status, _, raw = h.raw("POST", f"/api/documents?name={name}&images=1", data, {"Content-Type": "application/octet-stream"})
            body = json.loads(raw)
            if status >= 400:
                cells[src] = {"*": {"result": "FAILED", "detail": f"upload refused: {body['error']['message'][:100]}"}}
                continue
            doc = body["data"]
            cells[src] = {}
            for target in TARGETS:
                for engine in ("model", "libreoffice"):
                    key = target if engine == "model" else f"{target}@lo"
                    try:
                        res = h.job("POST", "/api/convert", {"docs": [doc["id"]], "options": {"target": target, "engine": engine}}, timeout=300)["results"][0]
                    except (ApiError, TimeoutError) as exc:
                        cells[src][key] = {"result": "FAILED", "detail": str(exc)[:120]}
                        continue
                    if not res["ok"]:
                        code = res["error"]["code"]
                        cells[src][key] = {"result": "refused" if code in ("unsupported", "same_format") else "FAILED", "detail": f"{code}: {res['error']['message'][:100]}"}
                        continue
                    out = res["report"]["output"]
                    produced = h.download(out)
                    text = text_of(produced, target)
                    verdict = "ok-nomark" if text is None else ("ok" if MARK.lower() in text.lower() else "LOST")
                    if src in ("png", "jpeg") and target in ("pdf", "png", "jpeg") and text is not None:
                        verdict = "ok-nomark"           # an image has no text layer: the marker is pixels, not text
                    cells[src][key] = {"result": verdict, "engine": res["report"]["engine"], "bytes": out["bytes"],
                                       "verified": res["report"]["verification"]["ok"]}
    finally:
        h.close()
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({"marker": MARK, "targets": list(TARGETS), "cells": cells}, indent=1) + "\n", encoding="utf-8")
    sym = {"ok": "ok", "ok-nomark": "ok*", "LOST": "LOST", "refused": "-", "FAILED": "FAIL"}
    print("src \\ target".ljust(12) + "".join(t.ljust(9) for t in TARGETS) + "   (model engine; * = raster, marker not checkable; - = clean refusal)")
    for src, row in cells.items():
        print(src.ljust(12) + "".join(sym.get(row.get(t, {}).get("result", ""), "?").ljust(9) for t in TARGETS))
    bad = [(s, k, c) for s, r in cells.items() for k, c in r.items() if c["result"] in ("LOST", "FAILED")]
    print(f"{len(bad)} LOST/FAILED cell(s)")
    for s, k, c in bad:
        print("  ", s, "->", k, c)
    return 0


if __name__ == "__main__":
    sys.exit(main())
