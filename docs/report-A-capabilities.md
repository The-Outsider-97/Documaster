# Report A - Capability matrix

_Generated 2026-10-22 23:57 by `tools/make_reports.py` from the running code._

What each workspace can do on **this** machine. Availability is computed from what is installed; nothing is promised that the machine cannot do. A feature is listed with the code that does it and the test that exercises it.

## Environment

| Component | State |
|---|---|
| LibreOffice (layout-preserving Office conversions, RTF/DOC reading) | /usr/bin/soffice |
| Tesseract OCR | /usr/bin/tesseract (languages: eng, osd) |
| qpdf | /usr/bin/qpdf |
| Python module `pypdf` | yes |
| Python module `pypdfium2` | yes |
| Python module `reportlab` | yes |
| Python module `docx` | yes |
| Python module `PIL` | yes |
| Python module `cryptography` | yes |
| Python module `yaml` | yes |

## Workspaces

| Workspace | Features | Evidence |
|---|---|---|
| Word Counter | Words, characters, sentences, paragraphs, reading/speaking time, language guess (labelled as a guess), top terms, goals; typed text or files; OCR for scanned PDFs when Tesseract is present; CJK counted per character, counting rules shown | tests_py/test_units.py (CountingTests); tests_e2e |
| Converter | Any supported source to any supported target through the SLDC document model or LibreOffice; every output re-opened and compared; kept/lost lists per file; encrypted SLDC; nothing overwritten | tests_py/test_convert_merge.py (ConvertTests) |
| Merger & Page Organizer | Merge PDF/Word/ODT/RTF/HTML/Markdown/text/SLDC/PNG/JPEG; page cards with drag-and-drop, keyboard moves, exclude, rotate, duplicate, blank pages; PDF pages copied unchanged; merge into other formats through logical pages | tests_py/test_convert_merge.py (MergeTests); tests/organizer.test.ts; tests_e2e |
| PDF Editor | View, thumbnails, page operations, annotations, text/image stamps, watermark, page numbers, forms (fill, flatten), bookmarks, metadata, password, crop, compress, OCR, true redaction; per-document undo/redo; save a copy | tests_py/test_pdf_ops.py, test_redaction.py, test_forms_ocr.py; tests_e2e |
| Reader & Analysis | Outline, structure checks, keywords, readability, repeated text, required sections (presets), personal-data scan, comparison of two documents, search with evidence, extractive summary | tests_py/test_units.py (PiiTests); tests_e2e |
| AI Assistant | Read-only requests answered immediately with evidence; every change is a confirmed, single-use, expiring proposal | tests_py/test_assistant.py; tests_e2e |

## PDF operations (from the operation registry)

| Operation | Permission it needs when the PDF is protected |
|---|---|
| `annotate` | annotate |
| `arrange` | assemble |
| `blank` | assemble |
| `bookmarks` | modify |
| `compress` | - |
| `crop` | assemble |
| `delete` | assemble |
| `fill_form` | fillForms |
| `flatten` | modify |
| `insert` | assemble |
| `metadata` | modify |
| `ocr` | modify |
| `password` | - |
| `redact` | modify |
| `remove_annotations` | annotate |
| `rotate` | assemble |
| `sanitize` | modify |
| `stamp` | modify |

## Assistant tools

| Tool | Changes files? | What it does |
|---|---|---|
| `list_documents` | no (immediate) | List the documents that are open. |
| `capabilities` | no (immediate) | Explain the assistant's abilities. |
| `describe_document` | no (immediate) | Format, size, pages and basic structure of a document. |
| `count_words` | no (immediate) | Words, characters, sentences and reading time of a document. |
| `outline` | no (immediate) | The heading structure of a document. |
| `search_text` | no (immediate) | Find text in a document, with page and position. |
| `summarize` | no (immediate) | An extractive summary: the most representative sentences of a document, with where they come from. |
| `keywords` | no (immediate) | Main terms of a document with where they appear. |
| `readability` | no (immediate) | Reading ease and sentence length. |
| `check_structure` | no (immediate) | Headings, images without descriptions, tables, links and scanned pages. |
| `find_pii` | no (immediate) | Look for email addresses, phone numbers, IBANs, card numbers and similar. |
| `find_duplicates` | no (immediate) | Paragraphs that appear more than once or are nearly identical. |
| `compare` | no (immediate) | What was added, removed or changed between two documents. |
| `check_sections` | no (immediate) | Whether a document has the sections a kind of document should have. |
| `answer_question` | no (immediate) | Answer from the text of the open documents, quoting the passages used. |
| `convert` | asks first | Convert documents to another format. |
| `merge` | asks first | Merge documents into one. |
| `rotate` | asks first | Rotate pages of a PDF. |
| `delete_pages` | asks first | Delete pages from a PDF. |
| `watermark` | asks first | Add diagonal watermark text to all pages of a PDF. |
| `page_numbers` | asks first | Add page numbers to a PDF. |
| `set_password` | asks first | Protect a PDF with a password, or remove it. |
| `compress` | asks first | Make a PDF smaller without losing quality. |
| `ocr` | asks first | Make scanned PDF pages searchable. |
| `redact_pii` | asks first | Permanently remove personal data (emails, phone numbers, IBANs, cards) from a PDF. |

## Analysis checks

`outline`, `structure`, `keywords`, `readability`, `pii`, `duplicates`, `sections`, `summary`
