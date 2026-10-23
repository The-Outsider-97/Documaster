# Report G - Honest status and known limits

_Generated 2026-10-22 23:57 by `tools/make_reports.py` from the running code._

What is done, what is verified, what is not.

## Verified

* The six workspaces work end to end in a real browser against a real server (Report F).
* Redaction removes content: tested by re-reading the saved file for text, bytes and metadata.
* Conversions and merges are re-opened and compared; the results state what was kept and what was not carried over.
* Forms: fill and flatten verified on a real AcroForm. OCR: verified with Tesseract on a rendered scan.
* The Privacy, Handler, Observability agents and the Reader were loaded through SLAI's `AgentFactory` and answered (Report B).

## Not verified, or limited

* **Language, Safety, Quality agents:** could not be loaded on the machine where the probes ran (missing `torch`; missing Quality memory file). Documaster's use of them is tested with stand-ins only. **Planning** is not used.
* **Privacy agent behaviour:** for text with personal data the real agent answers 'modify' with a tokenised payload; Documaster treats that as 'do not forward', so agent summaries are not available for such text.
* **Windows:** developed and tested on Linux. Paths and the launcher were written to be portable but were not run on Windows by this work. The target folder is `G:\GAIA\AI\SLAI\applications\Documaster`.
* **Layout fidelity:** conversions rebuild documents from the structure model (or LibreOffice when chosen); they do not reproduce a PDF's layout. Reports list this per format.
* **Redaction cost:** redacted pages become images (text on those pages is no longer selectable; OCR can restore it).
* **PDF text extraction:** no table or image extraction from PDFs; scanned pages need OCR.
* **Personal-data detection** is pattern based with checksums; it does not find names or addresses and says so in the interface. The BSN check passes 1 in 11 random 9-digit numbers and is marked as weak.
* **Right-to-left text** is not shaped in generated PDFs; no tagged PDF / alt text.
* **Drag-and-drop** of page cards is implemented with pointer and keyboard paths; the browser test drives the keyboard path and the toolbar, not a native drag.
* **Drawing tools:** the browser tests drive highlight, redaction, rotate/undo, merging and the assistant. Underline, strike-out, rectangle, comment, ellipse, freehand, crop, text boxes, text at a point and image placement are covered by tests of the operations the client sends (the saved PDF is re-opened and inspected), but their on-page drawing gestures were not each driven in a browser.
* **Load and very large files:** limits exist (upload size, pages, text, job deadline); no performance benchmark was run.
* **No fuzzing**, no accessibility audit with a screen reader (keyboard use and labels were built in, not audited).
