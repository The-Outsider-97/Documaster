"""Merging SLDC documents and arranging their logical pages.

SLDC has no fixed pages, so a *logical page* is a run of blocks that begins at the start of the document, at a block marked
``page_break_before`` (PDF imports mark every source page), at a change of section, or - when asked - at a heading. The page
organiser works on those units.

The merge is built on the format's own rules (nothing here defines a competing structure):

* block ids keep their value unless they would collide, in which case the later one is renamed; ``xref`` spans follow;
* paragraph/character styles with an identical definition are shared, different ones are renamed (``based_on`` follows);
* sections stay contiguous: a section that the arrangement splits is cloned for the later run, so page setups, headers and
  footers survive any reordering;
* footnotes/citations, embeds and images are carried only when an included block uses them. A page that is left out takes its
  notes, comment threads and images with it, so nothing from an excluded page remains in the result;
* the sources' provenance is kept and a ``documaster.merge`` entry records every input with its SHA-256; the result starts a
  new revision. Signatures sign one revision of one document, so they are not carried over (the result says so).
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .. import __version__
from ..bridge import sldc
from ..errors import LimitExceeded, ValidationFailed

_ID_MAX = 128


@dataclass
class MergeUnit:
    package: Any
    name: str
    sha256: str | None = None


@dataclass
class LogicalPage:
    index: int
    start: int                 # first block index (inclusive)
    end: int                   # last block index (exclusive)
    label: str
    words: int
    kinds: list[str]

    def public(self) -> dict[str, Any]:
        return {"index": self.index, "label": self.label, "words": self.words, "blocks": self.end - self.start,
                "kinds": self.kinds}


@dataclass
class MergeResult:
    package: Any
    warnings: list[str] = field(default_factory=list)
    pages: int = 0
    dropped: dict[str, int] = field(default_factory=dict)


def effective_sections(blocks: list[dict[str, Any]], sections: list[dict[str, Any]]) -> list[str | None]:
    """The section each block is in (blocks without one continue the previous; the first defaults to the first section)."""
    current = sections[0]["id"] if sections else None
    out: list[str | None] = []
    for block in blocks:
        if block.get("section"):
            current = block["section"]
        out.append(current)
    return out


def logical_pages(package: Any, split_headings: int = 0) -> list[LogicalPage]:
    """Split a document into logical pages (see the module docstring). ``split_headings`` N>0 also starts a page at every
    heading of level <= N."""
    doc = package.document
    blocks = doc["blocks"]
    if not blocks:
        return []
    secs = effective_sections(blocks, doc.get("sections", []))
    starts = [0]
    for i in range(1, len(blocks)):
        b = blocks[i]
        attrs = b.get("attributes") or {}
        new = bool(attrs.get("page_break_before")) or secs[i] != secs[i - 1]
        if not new and split_headings and b["type"] == "heading" and int(attrs.get("level", 1)) <= split_headings:
            new = True
        if new:
            starts.append(i)
    pages: list[LogicalPage] = []
    for n, s in enumerate(starts):
        e = starts[n + 1] if n + 1 < len(starts) else len(blocks)
        chunk = blocks[s:e]
        pages.append(LogicalPage(n, s, e, _label(chunk), sum(len(b.get("text", "").split()) for b in chunk),
                                 sorted({b["type"] for b in chunk})))
    return pages


def _label(chunk: list[dict[str, Any]]) -> str:
    for b in chunk:
        if b["type"] == "heading" and b.get("text", "").strip():
            return _clip(b["text"])
    for b in chunk:
        if b.get("text", "").strip():
            return _clip(b["text"])
    kinds = {b["type"] for b in chunk}
    return "(image)" if "figure" in kinds else "(table)" if "table" in kinds else "(empty)"


def _clip(text: str, n: int = 70) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= n else text[:n - 1] + "…"


class _Ids:
    """Hands out unique ids; a taken id is renamed ``<prefix><n>.<id>`` (kept within the 128 character limit)."""

    def __init__(self) -> None:
        self.used: set[str] = set()

    def claim(self, preferred: str, tag: str) -> str:
        if preferred not in self.used:
            self.used.add(preferred)
            return preferred
        n = 1
        while True:
            suffix = f"-{tag}{n}"
            candidate = preferred[:_ID_MAX - len(suffix)] + suffix
            if candidate not in self.used:
                self.used.add(candidate)
                return candidate
            n += 1


def _plan_items(units: list[MergeUnit], plan: list[dict[str, Any]] | None, pages_of: list[list[LogicalPage]]
                ) -> list[tuple[int, int]]:
    if plan is None:
        return [(u, p.index) for u, pages in enumerate(pages_of) for p in pages]
    items: list[tuple[int, int]] = []
    for entry in plan:
        if not isinstance(entry, dict):
            raise ValidationFailed("Each page-plan entry must be an object")
        try:
            u, p = int(entry["doc"]), int(entry["page"])
        except (KeyError, TypeError, ValueError):
            raise ValidationFailed("Each page-plan entry needs 'doc' and 'page' numbers") from None
        if not 0 <= u < len(units) or not 0 <= p < len(pages_of[u]):
            raise ValidationFailed(f"Page plan refers to a page that does not exist (document {u}, page {p})")
        if entry.get("included", True):
            items.append((u, p))
    return items


def merge_units(units: list[MergeUnit], plan: list[dict[str, Any]] | None = None, *, title: str | None = None,
                split_headings: int = 0, author: str | None = None, limit_blocks: int = 20_000) -> MergeResult:
    s = sldc()
    if not units:
        raise ValidationFailed("Add at least one document to merge")
    pages_of = [logical_pages(u.package, split_headings) for u in units]
    items = _plan_items(units, plan, pages_of)
    if not items:
        raise ValidationFailed("No pages are included in the plan: nothing to merge")
    total_blocks = sum(pages_of[u][p].end - pages_of[u][p].start for u, p in items)
    if total_blocks > limit_blocks:
        raise LimitExceeded(f"The result would have {total_blocks} blocks; the limit is {limit_blocks}")

    warnings: list[str] = []
    dropped: dict[str, int] = {}
    block_ids, section_ids = _Ids(), _Ids()
    ref_ids, embed_ids, asset_ids, thread_ids = _Ids(), _Ids(), _Ids(), _Ids()

    # ---- pass 1: choose the blocks and give them ids
    class Inst:
        __slots__ = ("unit", "page", "blocks", "idmap", "first", "natural")

    insts: list[Inst] = []
    first_map: dict[tuple[int, str], str] = {}        # (unit, old id) -> id of its first instance
    prev: tuple[int, int] | None = None
    for u, p in items:
        page = pages_of[u][p]
        src_blocks = units[u].package.document["blocks"][page.start:page.end]
        inst = Inst()
        inst.unit, inst.page = u, p
        inst.blocks = [copy.deepcopy(b) for b in src_blocks]
        inst.idmap = {}
        for b in inst.blocks:
            new = block_ids.claim(b["id"], "c")
            inst.idmap[b["id"]] = new
            first_map.setdefault((u, b["id"]), new)
        inst.natural = prev is not None and prev == (u, p - 1)
        inst.first = prev is None
        prev = (u, p)
        insts.append(inst)

    # ---- styles: only the ones used, with their based_on chains
    unit_styles: list[dict[str, dict[str, str]]] = [{"paragraph": {}, "character": {}} for _ in units]
    out_styles: dict[str, dict[str, Any]] = {"paragraph": {}, "character": {}}

    def need_style(u: int, kind: str, name: str) -> str:
        done = unit_styles[u][kind]
        if name in done:
            return done[name]
        group = units[u].package.document.get("styles", {}).get(kind, {})
        props = copy.deepcopy(group.get(name))
        if props is None:
            return name
        if "based_on" in props:
            props["based_on"] = need_style(u, kind, props["based_on"])
        # an identical definition under the same or any existing name is shared
        for existing, definition in out_styles[kind].items():
            if existing == name and definition == props:
                done[name] = existing
                return existing
        new = style_ids_for(kind).claim(name, "m")
        out_styles[kind][new] = props
        done[name] = new
        return new

    style_pool = {"paragraph": _Ids(), "character": _Ids()}

    def style_ids_for(kind: str) -> _Ids:
        return style_pool[kind]

    # ---- references / embeds / assets per instance
    out_refs: list[dict[str, Any]] = []
    out_embeds: list[dict[str, Any]] = []
    out_assets: dict[str, Any] = {}
    asset_map: dict[tuple[int, str], str] = {}
    embed_map: dict[tuple[int, str], str] = {}
    xref_lost = 0

    def need_asset(u: int, old: str) -> str:
        key = (u, old)
        if key in asset_map:
            return asset_map[key]
        asset = units[u].package.assets[old]
        for existing_id, existing in out_assets.items():
            if existing.data == asset.data:
                asset_map[key] = existing_id
                return existing_id
        new = asset_ids.claim(old, "a")
        out_assets[new] = s.Asset(new, asset.media_type, asset.data) if new != old else asset
        asset_map[key] = new
        return new

    def need_embed(u: int, old: str) -> str:
        key = (u, old)
        if key in embed_map:
            return embed_map[key]
        record = next((e for e in units[u].package.document.get("embeds", []) if e["id"] == old), None)
        if record is None:
            raise ValidationFailed(f"Embed {old!r} is declared nowhere in {units[u].name}")
        new = embed_ids.claim(old, "e")
        out_embeds.append({**copy.deepcopy(record), "id": new})
        embed_map[key] = new
        return new

    for inst in insts:
        u = inst.unit
        src_doc = units[u].package.document
        refs_src = {r["id"]: r for r in src_doc.get("references", [])}
        ref_map: dict[str, str] = {}
        for b in inst.blocks:
            b["id"] = inst.idmap[b["id"]]
            if "style" in b:
                b["style"] = need_style(u, "paragraph", b["style"])
            kept_spans = []
            for span in b.get("spans", []):
                kind = span["type"]
                if kind == "style":
                    span["style"] = need_style(u, "character", span["style"])
                elif kind == "ref":
                    old = span["ref"]
                    if old not in ref_map:
                        new = ref_ids.claim(old, "r")
                        ref_map[old] = new
                        out_refs.append({**copy.deepcopy(refs_src[old]), "id": new})
                    span["ref"] = ref_map[old]
                elif kind == "xref":
                    target = inst.idmap.get(span["target"]) or first_map.get((u, span["target"]))
                    if target is None:
                        xref_lost += 1
                        continue
                    span["target"] = target
                kept_spans.append(span)
            if "spans" in b:
                if kept_spans:
                    b["spans"] = kept_spans
                else:
                    del b["spans"]
            if b["type"] == "figure":
                b["figure"]["asset"] = need_asset(u, b["figure"]["asset"])
            elif b["type"] == "embed":
                b["embed"] = need_embed(u, b["embed"])
    if xref_lost:
        dropped["cross-references to pages that are not in the result"] = xref_lost
        warnings.append(f"{xref_lost} cross-reference(s) pointed at pages that are not included; they were turned into plain text")

    # ---- sections
    any_sections = any(u.package.document.get("sections") for u in units)
    out_sections: list[dict[str, Any]] = []
    out_blocks: list[dict[str, Any]] = []
    current_key: tuple[int, str | None] | None = None
    used_keys: dict[tuple[int, str | None], str] = {}
    for inst in insts:
        u = inst.unit
        doc = units[u].package.document
        sections = {sec["id"]: sec for sec in doc.get("sections", [])}
        eff_all = effective_sections(doc["blocks"], doc.get("sections", []))
        page = pages_of[u][inst.page]
        for offset, b in enumerate(inst.blocks):
            orig = eff_all[page.start + offset]
            if any_sections:
                key = (u, orig)
                if key != current_key:
                    sid = section_ids.claim(f"s-{units_index_tag(u)}" if orig is None else orig, "s")
                    if orig is None:
                        sec: dict[str, Any] = {"id": sid}
                    else:
                        sec = copy.deepcopy(sections[orig])
                        sec["id"] = sid
                        if key in used_keys and "numbering" in sec:
                            numbering = {k: v for k, v in sec["numbering"].items() if k != "start"}
                            if numbering:
                                sec["numbering"] = numbering
                            else:
                                del sec["numbering"]
                    for edge in ("header", "footer"):
                        if edge in sec and "style" in sec[edge]:
                            sec[edge]["style"] = need_style(u, "paragraph", sec[edge]["style"])
                    out_sections.append(sec)
                    used_keys.setdefault(key, sid)
                    current_key = key
                    b["section"] = sid
                else:
                    b.pop("section", None)
            else:
                b.pop("section", None)
        first = inst.blocks[0]
        if inst.first:
            attrs = first.get("attributes")
            if attrs and attrs.pop("page_break_before", None) is not None and not attrs:
                del first["attributes"]
        elif not inst.natural and not (any_sections and first.get("section")):
            first.setdefault("attributes", {})["page_break_before"] = True
        out_blocks.extend(inst.blocks)

    # ---- styles used by header/footer styles are already requested above; assemble the document
    first_unit = units[insts[0].unit]
    first_doc = first_unit.package.document
    languages = [units[i.unit].package.document.get("language", "und") for i in insts]
    known = sorted({l for l in languages if l != "und"})
    language = known[0] if known and languages[0] == "und" else first_doc.get("language", "und")
    if len(known) > 1:
        warnings.append("The documents are in different languages (" + ", ".join(known) + "); the result is labelled "
                        + language)
    names = [u.name for u in units]
    final_title = (title or "").strip() or (first_doc.get("title") if len(set(i.unit for i in insts)) == 1
                                             else f"{first_doc.get('title', names[0])} + {len(set(i.unit for i in insts)) - 1} more")
    document: dict[str, Any] = {
        "schema": first_doc["schema"], "id": "doc-" + __import__("uuid").uuid4().hex,
        "title": final_title[:300] or "Merged document", "language": language,
        "blocks": out_blocks,
    }
    if first_doc.get("metadata"):
        document["metadata"] = copy.deepcopy(first_doc["metadata"])
    publication = copy.deepcopy(first_doc.get("publication"))
    if publication:
        cover = publication.get("cover_asset")
        if cover:
            if cover in first_unit.package.assets:
                publication["cover_asset"] = need_asset(insts[0].unit, cover)
            else:
                publication.pop("cover_asset", None)
        document["publication"] = publication
    if any(out_styles[k] for k in out_styles):
        document["styles"] = {k: v for k, v in out_styles.items() if v}
    if out_sections:
        document["sections"] = out_sections
    if out_refs:
        document["references"] = out_refs
    if out_embeds:
        document["embeds"] = out_embeds

    # ---- provenance
    provenance: list[dict[str, Any]] = []
    used_units = sorted({i.unit for i in insts})
    for u in used_units:
        provenance.extend(copy.deepcopy(units[u].package.document.get("provenance", [])))
    provenance = provenance[-150:]
    inputs = []
    for u in used_units:
        d = units[u].package.document
        entry = {"name": units[u].name, "document_id": d["id"], "pages": sum(1 for x, _ in items if x == u)}
        if units[u].sha256:
            entry["sha256"] = units[u].sha256
        inputs.append(entry)
    provenance.append(s.provenance_entry("documaster.merge", agent=f"documaster/{__version__}",
                                         inputs=inputs, output_pages=len(items)))
    document["provenance"] = provenance

    # ---- comments of included blocks, signatures dropped
    comments: list[dict[str, Any]] = []
    by_id = {b["id"]: b for b in out_blocks}
    for u in used_units:
        for thread in units[u].package.comments:
            target = first_map.get((u, thread["anchor"]["block"]))
            if target is None or target not in by_id:
                dropped["comment threads on pages that are not in the result"] = \
                    dropped.get("comment threads on pages that are not in the result", 0) + 1
                continue
            t = copy.deepcopy(thread)
            t["id"] = thread_ids.claim(t["id"], "t")
            t["anchor"]["block"] = target
            t["anchor"]["block_sha256"] = s.block_digest(by_id[target])
            comments.append(t)
    signed = sum(len(units[u].package.signatures) for u in used_units)
    if signed:
        dropped["signatures"] = signed
        warnings.append(f"{signed} signature(s) were not carried over: a signature covers one revision of one document, and "
                        "the merged document is a new revision. It has not been re-signed.")

    package = s.SLDCPackage(document, out_assets, comments, [], first_unit.package.version)
    try:
        package = s.next_revision(package, author=author or None, note=f"Merged {len(used_units)} document(s)")
        s.validate_package(package)
    except s.SLDCError as exc:
        raise ValidationFailed(f"The merged document is not valid SLDC: {exc}") from exc
    hidden = [u.name for i, u in enumerate(units) if i not in used_units]
    if hidden:
        warnings.append("Not used (all pages excluded): " + ", ".join(hidden))
    return MergeResult(package, warnings, len(items), dropped)


def units_index_tag(u: int) -> str:
    return f"d{u + 1}"


def page_outline(unit: MergeUnit, split_headings: int = 0) -> list[dict[str, Any]]:
    return [p.public() for p in logical_pages(unit.package, split_headings)]
