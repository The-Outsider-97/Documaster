# Report D - SLDC integration

_Generated 2026-10-22 23:57 by `tools/make_reports.py` from the running code._

SLDC (`src/utils/sldc.py`) is the authoritative document model. Documaster defines no competing schema.

## Pipeline

`read` -> `IRResult(SLDCPackage)` -> `writer` -> `verify_written` -> atomic commit (a file appears in the output folder only after it passed).

* `slai/docmodel/ir.py` builds and reads SLDC structures through the documented fields only (`blocks`, `spans`, `styles`, `sections`, `references`, ...).
* `slai/bridge/__init__.py` imports `src.utils.sldc` from the SLAI checkout; if it is missing Documaster refuses to start and says so.
* SLDC is both a source and a target of the converter; an encrypted SLDC needs its password; the encryption is SLDC's own.

## Merging and page arrangement

SLDC has no fixed pages. A *logical page* is a run of blocks starting at the document start, a block marked `page_break_before` (PDF imports mark every source page), a section change, or - when asked - a heading.
The merge follows the format's rules: colliding block ids are renamed and `xref` spans follow; identical styles are shared, different ones renamed; sections stay contiguous (cloned when an arrangement splits them);
notes, comments and images travel only with the blocks that use them, so an excluded page leaves nothing behind; provenance is kept and a `documaster.merge` entry records every input with its SHA-256.

## Signatures

Signatures sign one revision of one document. A merge or edit creates a new revision, so signatures are **not carried over**, and Documaster **never signs or re-signs** anything: there is no such operation (`tests_py/test_pdf_ops.py::test_documents_are_not_re_signed`) and signature form fields are refused (`test_forms_ocr.py`).

## Evidence

`tests_py/test_convert_merge.py`: SLDC round trip, encrypted SLDC, markdown headings surviving as headings.
