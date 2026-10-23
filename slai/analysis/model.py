"""The text of a document as located units, so every finding can point at its evidence (document, page, block, offsets)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..docmodel.sldc_merge import logical_pages


@dataclass
class Unit:
    doc_id: str
    doc_name: str
    block_id: str
    index: int
    page: int | None           # 1-based real page (PDF) or None
    part: int                  # 1-based logical part
    kind: str
    text: str
    level: int = 0

    def evidence(self, start: int = 0, end: int | None = None, quote: bool = True) -> dict[str, Any]:
        end = len(self.text) if end is None else end
        ev: dict[str, Any] = {"docId": self.doc_id, "doc": self.doc_name, "blockId": self.block_id, "page": self.page,
                              "part": self.part, "start": start, "end": end}
        if quote:
            q = self.text[max(0, start):end]
            ev["quote"] = q if len(q) <= 240 else q[:237] + "…"
        return ev


def units_of(doc: Any, result: Any) -> list[Unit]:
    package = result.package
    blocks = package.document["blocks"]
    pages = logical_pages(package, 0)
    part_of: dict[int, int] = {}
    for p in pages:
        for i in range(p.start, p.end):
            part_of[i] = p.index + 1
    real = doc.format == "pdf"
    out: list[Unit] = []
    for i, b in enumerate(blocks):
        text = b.get("text", "")
        if b["type"] == "table":
            rows = b["table"]["rows"]
            text = "\n".join(" | ".join(c["text"] for c in r) for r in rows)
        if not text.strip():
            continue
        part = part_of.get(i, 1)
        out.append(Unit(doc.id, doc.name, b["id"], i, part if real else None, part, b["type"], text,
                        int((b.get("attributes") or {}).get("level", 0)) if b["type"] == "heading" else 0))
    return out


def full_text(units: list[Unit]) -> str:
    return "\n\n".join(u.text for u in units)
