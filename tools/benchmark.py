#!/usr/bin/env python3
"""Performance baselines for the main workflows, measured through the real server exactly as the browser drives it.

    SLAI_ROOT=... python3 tools/benchmark.py [--out docs/audit/performance.json] [--repeat 3]

Each case builds its own fixture, runs ``--repeat`` times and records min / median / max wall-clock seconds and the peak resident
memory of this process (the server runs in-process). Numbers describe the machine they were taken on (recorded in the output);
they are baselines to compare against, not promises.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import statistics
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP / "tests_py"))
from support import Harness, make_docx, make_pdf, ROOT  # noqa: E402


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024        # KiB on Linux


def lorem(n: int) -> list[str]:
    base = "The quick brown fox jumps over the lazy dog while counting the number of words in this sentence."
    return [f"{base} Line {i}." for i in range(n)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(APP / "docs" / "audit" / "performance.json"))
    ap.add_argument("--repeat", type=int, default=3)
    args = ap.parse_args()
    if ROOT is None:
        print("SKIP: the SLAI checkout was not found (set SLAI_ROOT)")
        return 0
    h = Harness()
    results: list[dict] = []

    def case(name: str, fn, *, setup=None, note: str = "") -> None:
        times = []
        ctx = setup() if setup else None
        for _ in range(args.repeat):
            t = time.perf_counter()
            fn(ctx)
            times.append(time.perf_counter() - t)
        row = {"case": name, "runs": args.repeat, "min_s": round(min(times), 3), "median_s": round(statistics.median(times), 3),
               "max_s": round(max(times), 3), "peak_rss_mb": round(rss_mb()), "note": note}
        results.append(row)
        print(f"{name:<58} median {row['median_s']:>7.3f}s  (min {row['min_s']}, max {row['max_s']})  rss {row['peak_rss_mb']} MB", flush=True)

    def up(name: str, data: bytes) -> dict:
        return h.upload(name, data)

    try:
        small = make_pdf([[f"Page {i} text"] for i in range(10)])
        big = make_pdf([lorem(30) for _ in range(300)])
        huge_text = ("word " * 2_000_000)
        docx_100 = make_docx([f"Paragraph {i}. " + lorem(1)[0] for i in range(2000)], heading="Benchmark")

        case("upload 10-page PDF", lambda _: up("s.pdf", small))
        case("upload 300-page PDF (%.1f MB)" % (len(big) / 1e6), lambda _: up("b.pdf", big))
        case("count words, 2M-word text (/api/count/text)", lambda _: h.call("POST", "/api/count/text", {"text": huge_text}),
             note="deterministic tokeniser, whole text in one request")
        d300 = up("b.pdf", big)
        case("count words, 300-page PDF", lambda _: h.job("POST", "/api/count/documents", {"docs": [d300["id"]]}, timeout=600))
        dx = up("long.docx", docx_100)
        case("count words, 2000-paragraph DOCX", lambda _: h.job("POST", "/api/count/documents", {"docs": [dx["id"]]}, timeout=600))
        case("convert 2000-paragraph DOCX -> PDF (model engine)",
             lambda _: h.job("POST", "/api/convert", {"docs": [dx["id"]], "options": {"target": "pdf"}}, timeout=900))
        case("convert 2000-paragraph DOCX -> PDF (LibreOffice engine)",
             lambda _: h.job("POST", "/api/convert", {"docs": [dx["id"]], "options": {"target": "pdf", "engine": "libreoffice"}}, timeout=900))
        case("convert 300-page PDF -> TXT", lambda _: h.job("POST", "/api/convert", {"docs": [d300["id"]], "options": {"target": "txt"}}, timeout=900))
        ids = [up(f"m{i}.pdf", make_pdf([[f"Doc {i} page {p}"] for p in range(50)]))["id"] for i in range(20)]
        case("merge 20 PDFs x 50 pages -> PDF (1000 pages)",
             lambda _: h.job("POST", "/api/merge", {"docs": ids, "target": "pdf"}, timeout=900))
        case("page image (w=1200) of a 300-page PDF, page 150",
             lambda _: h.raw("GET", f"/api/documents/{d300['id']}/pages/150/image?w=1200"))
        s10 = up("s.pdf", small)
        case("rotate 1 page of a 10-page PDF (op + new revision)",
             lambda _: h.call("POST", f"/api/documents/{s10['id']}/op", {"op": "rotate", "pages": "1", "degrees": 90}))
    finally:
        h.close()

    out = {"machine": {"platform": platform.platform(), "python": platform.python_version(), "cpus": os.cpu_count()},
           "date": time.strftime("%Y-%m-%d"), "repeat": args.repeat, "results": results}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    print(f"written {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
