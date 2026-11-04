# SLAI Writer vector shapes — Documaster export adapter

Companion work for [Writer PR #9](https://github.com/The-Outsider-97/slai_writer/pull/9).
Draft work in [Documaster PR #4](https://github.com/The-Outsider-97/Documaster/pull/4).

This adapter consumes the inert `slai.writer.shape` SLDC extension that
SLAI Writer stores in dedicated paragraph carriers; it does not modify the
shared SLDC schema or execute any user-provided SVG markup.

## Code

- `slai/docmodel/writers/vector_shapes.py`: fixed native vector presets,
  ReportLab canvas paths, shape text as searchable PDF text, bounded layered
  shadows, Word 2010+ `wps:wsp` editable shapes with text, outlines, fills,
  shadow, rotation, and inline/floating anchors.
- `slai/docmodel/writers/write_pdf.py`: uses vector shape flowables in its
  existing story and measured-page tracking machinery.
- `slai/docmodel/writers/write_docx.py`: emits Word DrawingML through its
  existing Word document writer.
- `tests_py/test_vector_shapes.py`: format and interoperability smoke tests.
- `.github/workflows/writer-vector.yml`: runnable compile/PDF/DOCX test job.

## Fidelity policy

Native PDF preserves ordinary geometry as vector paths and draws text as
PDF text. Blurred shadows are **approximated**, not analytically identical
to SVG Gaussian blur; Writer may elect high-resolution raster fallback for
blurred or composite-opacity objects.

Word DrawingML supports all ten preset kinds as native editable objects,
basic rich text, shape rotation, fills, outlines, a single outer shadow and
certain wrap modes. Distinct independent rectangle corners and multiple
independent shadow layers are **not** fully representable using this mapping.
Writer is responsible for selecting an honest visual fallback rather than
claiming native vector editability in unsupported cases.

The adapter is not a shape import implementation. Round-trip reimport of
DrawingML as native Writer shapes is not yet available.

## Verification

The native exporter source and tests are committed to GitHub; build, tests
and visual inspection with Microsoft Word/LibreOffice have not yet passed in
this session. GitHub Actions workflow jobs have remained queued.
**Do not merge as a completed exporter until verification is green.**

Run locally, within the SLAI v2.4 installation:

```powershell
py -3.12 -m pytest applications\Documaster\tests_py\test_vector_shapes.py -v
```

Test real PDF and Word rendering, text extraction, rotation, floating anchors,
overlaps, shadow opacity and document compatibility before release.
