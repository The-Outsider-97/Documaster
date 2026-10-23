# Report C - Format matrix

_Generated 2026-10-22 23:57 by `tools/make_reports.py` from the running code._

Reading and writing routes, with the backend that does the work and what each route cannot do.

## Reading

| Format | Available | Backend | Limits |
|---|---|---|---|
| PDF | yes | pypdf (text layer, page by page) | No layout reconstruction: headings are inferred from text shape, tables and images are not extracted; Scanned pages need OCR (Tesseract) |
| SLDC | yes | SLAI sldc.py (authoritative) | Encrypted documents need their password |
| Word (DOCX) | yes | python-docx | Text boxes, equations and tracked changes are not read |
| Word 97-2003 (DOC) | yes | LibreOffice (headless) -> DOCX -> python-docx | Requires LibreOffice to be installed |
| OpenDocument Text | yes | Documaster ODF reader | Equations, drawings and tracked changes are not read |
| Rich Text | yes | LibreOffice (headless) -> DOCX -> python-docx | Requires LibreOffice to be installed |
| HTML | yes | Documaster HTML reader (scripts ignored, never executed) | Only embedded PNG/JPEG data-URI images are kept; CSS layout is not applied |
| Markdown | yes | SLAI Reader ParserEngine | Markdown: headings, lists, pipe tables and inline emphasis are read |
| Plain text | yes | SLAI Reader ParserEngine | Structure follows the text |
| XML | yes | SLAI Reader ParserEngine | Structure follows the text |
| CSV | yes | Documaster CSV reader | Becomes one table |

## Writing

| Format | Available | Backend | Limits |
|---|---|---|---|
| PDF | yes | ReportLab (embedded TrueType fonts) | No tagged PDF / alt text; Right-to-left text is not shaped; Footnotes appear as a notes list; Fixed layout: re-importing a PDF returns text only |
| Word (DOCX) | yes | python-docx | Footnotes appear as a notes list; Comments and revision history are not written |
| OpenDocument Text | yes | Documaster ODF writer | Comments and revision history are not written |
| Rich Text | yes | Documaster RTF writer | Fonts are named, not embedded |
| HTML | yes | Documaster HTML writer (self-contained, no scripts) | Page layout is not reproduced |
| Markdown | yes | SLDC to_markdown | Images are written next to the file in <name>_files/; No page layout |
| Plain text | yes | SLDC extract_text | All formatting is lost; tables become aligned text |
| XML | yes | Documaster XML schema urn:slai:documaster:document:1 | A plain structural export |
| SLDC | yes | SLAI sldc.py dumps_package | Password encryption needs the 'cryptography' package |

## How outputs are checked

1. SLAI Reader `IntegrityEngine.verify_output` where the Reader knows the format.
2. The file is re-opened with a real parser for its format.
3. The words of the re-opened file are compared with the words of the document model that was written (coverage and order).

Layout, fonts and appearance are not judged and the reports say so. A failed check deletes the output.
