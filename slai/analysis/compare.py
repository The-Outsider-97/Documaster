"""Comparing two documents.

Two documents that share block identity (revisions of one SLDC file) are compared with SLDC's own ``diff_documents``. Any other
pair is aligned by text: blocks are matched by normalised content, then by similarity, and the result says it is a text
alignment (ids of independently imported documents are unrelated, so id comparison would be meaningless)."""
from __future__ import annotations

import difflib
import re
from typing import Any

from ..bridge import sldc
from .model import Unit


def _n(t: str) -> str:
    return re.sub(r"\W+", " ", t.lower()).strip()


def compare(pa: Any, ua: list[Unit], pb: Any, ub: list[Unit], *, name_a: str, name_b: str) -> dict[str, Any]:
    s = sldc()
    da, db = pa.document, pb.document
    ids_a, ids_b = {b["id"] for b in da["blocks"]}, {b["id"] for b in db["blocks"]}
    shared = len(ids_a & ids_b)
    lineage = da.get("id") == db.get("id") or (shared >= max(3, 0.5 * min(len(ids_a), len(ids_b))) and not all(i.startswith("b0") for i in ids_a & ids_b))
    result: dict[str, Any] = {"a": name_a, "b": name_b}
    by_a, by_b = {u.block_id: u for u in ua}, {u.block_id: u for u in ub}
    if lineage:
        d = s.diff_documents(pa, pb)
        result.update(method="SLDC diff_documents (block identity)", summary={k: (len(v) if isinstance(v, list) else v) for k, v in d.items() if k != "changed_fields"},
                      changedFields=d["changed_fields"])
        result["added"] = [by_b[i].evidence(quote=True) for i in d["added"] if i in by_b][:200]
        result["removed"] = [by_a[i].evidence(quote=True) for i in d["removed"] if i in by_a][:200]
        result["modified"] = [{"before": by_a[i].evidence(quote=True), "after": by_b[i].evidence(quote=True), "similarity": round(
            difflib.SequenceMatcher(None, _n(by_a[i].text), _n(by_b[i].text)).ratio(), 3)} for i in d["modified"] if i in by_a and i in by_b][:200]
        result["moved"] = [by_b[i].evidence(quote=False) for i in d["moved"] if i in by_b][:100]
    else:
        ka, kb = [_n(u.text) for u in ua], [_n(u.text) for u in ub]
        sm = difflib.SequenceMatcher(None, ka, kb, autojunk=False)
        added, removed, modified, same = [], [], [], 0
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                same += i2 - i1
            elif tag == "delete":
                removed += [ua[i].evidence() for i in range(i1, i2)]
            elif tag == "insert":
                added += [ub[j].evidence() for j in range(j1, j2)]
            else:
                a_blocks, b_blocks = list(range(i1, i2)), list(range(j1, j2))
                unmatched_b = set(b_blocks)
                for i in a_blocks:
                    best, bj = 0.0, None
                    for j in unmatched_b:
                        r = difflib.SequenceMatcher(None, ka[i], kb[j]).ratio()
                        if r > best:
                            best, bj = r, j
                    if bj is not None and best >= 0.6:
                        unmatched_b.discard(bj)
                        modified.append({"before": ua[i].evidence(), "after": ub[bj].evidence(), "similarity": round(best, 3)})
                    else:
                        removed.append(ua[i].evidence())
                added += [ub[j].evidence() for j in sorted(unmatched_b)]
        result.update(method="text alignment (the documents do not share block identity)",
                      summary={"added": len(added), "removed": len(removed), "modified": len(modified), "unchanged": same},
                      added=added[:200], removed=removed[:200], modified=modified[:200], moved=[])
    wa, wb = _n(" ".join(u.text for u in ua)).split(), _n(" ".join(u.text for u in ub)).split()
    result["wordSimilarity"] = round(difflib.SequenceMatcher(None, wa[:20000], wb[:20000], autojunk=False).ratio(), 3) if wa and wb else 0.0
    result["words"] = {"a": len(wa), "b": len(wb)}
    return result
