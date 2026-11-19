# C. Capability matrix

_Measured on 2026-11-19 with `tools/probe_conversions.py` (real server, real LibreOffice 24.2.7.2, real fixtures). Raw results: `conversion-matrix.json`._

Every cell below was **executed**, not read from the capability registry. A cell is `ok` when the job succeeded, the output passed Documaster's own
verification **and** a marker word (`Zanzibar`) planted in the source is present in the output text. `ok*` = raster output (PNG/JPEG), where a text marker
cannot be checked. `–` is a clean, documented refusal (same format, or a route that does not exist). 330 pairs run: **131 ok, 199 clean refusals,
0 lost-content, 0 failed.** (Before this audit: 30 pairs failed — every RTF and DOC route plus HTML→DOCX/RTF through LibreOffice.)

## C1. Model engine (Documaster's structure-preserving engine, the default)

| source \ target | pdf | docx | odt | rtf | html | md | txt | xml | sldc | png | jpeg |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **docx** | ok | – | ok | ok | ok | ok | ok | ok | ok | – | – |
| **pdf** | – | ok | ok | ok | ok | ok | ok | ok | ok | ok* | ok* |
| **txt** | ok | ok | ok | ok | ok | ok | – | ok | ok | – | – |
| **md** | ok | ok | ok | ok | ok | – | ok | ok | ok | – | – |
| **html** | ok | ok | ok | ok | – | ok | ok | ok | ok | – | – |
| **xml** | ok | ok | ok | ok | ok | ok | ok | – | ok | – | – |
| **csv** | ok | ok | ok | ok | ok | ok | ok | ok | ok | – | – |
| **png** | ok* | – | – | – | – | – | – | – | – | – | ok* |
| **jpeg** | ok* | – | – | – | – | – | – | – | – | ok* | – |
| **odt** | ok | ok | – | ok | ok | ok | ok | ok | ok | – | – |
| **rtf** | ok | ok | ok | – | ok | ok | ok | ok | ok | – | – |
| **doc** | ok | ok | ok | ok | ok | ok | ok | ok | ok | – | – |
| **xlsx** | ok | – | – | – | – | – | – | – | – | – | – |
| **pptx** | ok | – | – | – | – | – | – | – | – | – | – |
| **odp** | ok | – | – | – | – | – | – | – | – | – | – |

## C2. LibreOffice engine (layout-preserving; only where a route exists)

| source \ target | pdf | docx | odt | rtf | html | md | txt | xml | sldc | png | jpeg |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **docx** | ok | – | ok | ok | ok | – | ok | – | – | – | – |
| **pdf** | – | – | – | – | – | – | – | – | – | – | – |
| **txt** | ok | ok | ok | ok | ok | – | – | – | – | – | – |
| **md** | ok | ok | ok | ok | ok | – | ok | – | – | – | – |
| **html** | ok | ok | ok | ok | – | – | ok | – | – | – | – |
| **xml** | – | – | – | – | – | – | – | – | – | – | – |
| **csv** | – | – | – | – | – | – | – | – | – | – | – |
| **png** | – | – | – | – | – | – | – | – | – | – | – |
| **jpeg** | – | – | – | – | – | – | – | – | – | – | – |
| **odt** | ok | ok | – | ok | ok | – | ok | – | – | – | – |
| **rtf** | ok | ok | ok | – | ok | – | ok | – | – | – | – |
| **doc** | ok | ok | ok | ok | ok | – | ok | – | – | – | – |
| **xlsx** | ok | – | – | – | – | – | – | – | – | – | – |
| **pptx** | ok | – | – | – | – | – | – | – | – | – | – |
| **odp** | ok | – | – | – | – | – | – | – | – | – | – |

Notes: the LibreOffice engine is offered for DOCX/DOC/ODT/RTF/HTML/TXT/MD sources and DOCX/ODT/RTF/HTML/TXT/PDF targets, plus XLSX/PPTX/ODP→PDF
(only engine). Everything else is a refusal by design.

## C3. Format registry (the "investigation list" from the assignment)

| Format | Read | Write | Status | Evidence / reason |
|---|---|---|---|---|
| PDF | yes | yes | supported | matrix row/column above; PDF→DOCX etc. is text/structure extraction, not layout reconstruction |
| DOCX | yes | yes | supported | DOCX→DOCX is a refusal unless "retain" is chosen (byte-exact copy, SHA-256 checked) |
| DOC | yes (via LibreOffice) | no | supported (read) | needs LibreOffice; **was broken until this audit** (see D-001) |
| ODT | yes | yes | supported | own ODF reader/writer; DOCTYPE and oversized archives refused |
| RTF | yes (via LibreOffice) | yes | supported | **was broken until this audit** |
| TXT, HTML, MD, XML, CSV | yes | TXT/HTML/MD/XML yes; CSV no (read-only) | supported | CSV becomes one table; no CSV writer exists |
| PNG, JPEG | yes (as pages) | yes (from PDF) | supported | raster engine; no OCR route for images (only for scanned PDFs) |
| SLDC | yes | yes | supported | authoritative SLAI model; encrypted SLDC supported with `cryptography` |
| XLSX, PPTX, ODP | LibreOffice only | PDF only | **added in this audit** | verified: server tests + browser e2e; content is not read by Documaster (no counting/analysis/merge) |
| XLS, PPT (legacy) | – | – | not added | OLE2 files cannot be told apart from DOC by content without parsing the compound file; not verified, so not offered |
| TIFF | – | – | not added | Pillow can read it, but multi-page handling and verification were not built; recorded as a P2 gap |
| SVG | – | – | not added | no safe rasteriser in the dependency set (Pillow cannot read SVG); LibreOffice route unverified |

## C4. Workspace capabilities (from `slai/capabilities.py`, confirmed by tests/e2e)

| Workspace | Verified in this audit | Honest limits (as the app states them) |
|---|---|---|
| Word Counter | deterministic fixtures (`CountingFixtureTests`), 2M-word text in 4.4 s, PDF/DOCX counting | rule differs from Word for `TCP/IP`, `10:30`, emoji (documented); PDFs without text layer count 0 unless OCR is on |
| Converter | full matrix above; batch ZIP + individual downloads (e2e); cancel/retry covered by `test_batch_api` | page layout is not preserved by the model engine (the LibreOffice engine is) |
| Merger & organizer | merge PDF+PDF, PDF+DOCX, reorder/exclude/rotate plan, undo of pointer drag (e2e) | SLDC "pages" are logical pages; signatures are not carried over |
| PDF editor | rotate/undo/redo, highlight, text boxes, true redaction with verification that the word is gone from the saved file (e2e + unit) | **no editing of existing PDF text in place**; digital signatures are never added |
| Reader / analysis | search, PII listing, structured report export | deterministic; agent text only when an agent is available and labelled |
| AI assistant | answers with citations, proposals need confirmation, injected instruction in a document creates no proposal (e2e) | without a working SLAI language agent it offers the same actions as buttons and says so |
