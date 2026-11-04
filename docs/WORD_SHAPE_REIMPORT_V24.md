# WordprocessingShape reimport for SLAI Writer v2.4

Development branch `feature/shape-interop-v24`; draft pull request #5.
Companion Writer work: `feature/shape-workspace-v24`, draft PR #11.

This is a targeted, safe implementation for Word 2010+ DrawingML
`wps:wsp` shapes in DOCX, extending the native Writer shape exporter from
the previously merged Documaster PR #4.

## Architecture

- `slai/docmodel/read_vector_shapes.py` implements finite-bounds parsing of
  known preset geometries. No arbitrary SVG/HTML or external URI is evaluated.
- `slai/docmodel/read_docx.py` integrates the parser alongside existing
  paragraph, table and image parsing, restoring a dedicated empty paragraph
  carrying the `slai.writer.shape` namespaced extension.
- `slai/docmodel/readers.py` returns truthful structural features and
  conversion-loss metadata rather than always stating shapes were lost.
- `tests_py/test_read_vector_shapes.py` exercises geometry, dimensions, text
  spans, floating anchors, stacking, and unknown preset rejection.

## Supported import data

Known Word DrawingML preset kinds: rectangle, ellipse, line, triangle,
diamond, pentagon, hexagon, star, arrow, plus. Supported properties
include dimensions, rotation, solid fill, opacity, solid/dashed/dotted
outline, basic rich text runs, a single outer shadow, basic inline/floating
positioning and object stacking.

Unsupported effects, clipping/geometry formulas, freeform paths and
ambiguous anchoring are **not silently normalized** into an invented shape.
They are reported and/or left out of the editable import result.

Absolute Word page positions are normalized against the current section's
margins, consistent with the existing export adapter. The original Word
paragraph-level anchor identity cannot always be recovered from OOXML;
page overlay is used as the conservative fallback and any lost wrapping
semantics are disclosed.

Native DOCX import can never guarantee preservation of arbitrary Word
WordprocessingML and DrawingML constructs without a broader vocabulary.

## Evidence and restrictions

A pre-existing sample DOCX (`wps_test.docx`) was structurally inspected
outside GitHub and rendered to PNG with LibreOffice: its single star
`wps:wsp` shape and text were visible. This establishes only that a
sample native DrawingML document can render; it is not proof that the new
reimport module passes tests.

The branch contains automated test code, but GitHub Actions runs are
currently failing with no available job steps/logs. The importer remains
unverified against the full installed SLAI application and external Word
versions. Do not merge solely on the existence of this implementation.

Run from the SLAI v2.4 checkout:

```powershell
py -3.12 -m pytest applications\Documaster\tests_py\test_read_vector_shapes.py -v
py -3.12 -m pytest applications\Documaster\tests_py\test_vector_shapes.py -v
```

Next gates: reimport → save SLDC → edit in Writer → export DOCX → reimport
again with structural assertions; real LibreOffice/Microsoft Word rendering;
capture and explain all unsupported shape-property losses.
