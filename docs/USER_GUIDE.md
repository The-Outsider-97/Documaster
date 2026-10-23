# Documaster user guide

## Starting

Run `python -m slai` from the Documaster folder (see the README). Open the link it prints. Documaster only answers on your own computer. Use the switch at the bottom of the sidebar to change between the dark and light theme; the choice is remembered.

Files you open stay open in all workspaces: the "Add an open document…" list at the top of each workspace picks up what you opened elsewhere.

## Word Counter

1. Type or paste text, or drop files (PDF, Word, ODT, RTF, HTML, Markdown, text, SLDC).
2. The numbers update as you type. Open "How is this counted?" for the rules (for example, each Han or kana character counts as one word, as in Word).
3. Set length goals (minimum or maximum words, characters, sentences) to see whether you meet them.
4. For a scanned PDF turn on OCR; the text is recognised with Tesseract and can contain mistakes (Documaster says so).

## Converter

1. Drop one or more files. Reorder or remove them in the list.
2. Choose **Convert to:**. "What to expect from this format" lists what that format cannot carry. Formats that cannot be written on this computer are marked "(unavailable)".
3. Press **Convert**. Each result card says **Checked** when the new file was re-opened and compared with the source, lists what was kept and what was not carried over, and has the download link. Files are saved in your output folder under a free name; nothing is overwritten.
4. Options: title, page size, font size, page numbers, footnotes, OCR, the conversion engine (Documaster's own, or LibreOffice to keep page layout), line endings for text, and a password for SLDC.

Converting a file to its own format is refused (there is nothing to do).

## Merger & Page Organizer

1. Drop files. Every page appears as a card, coloured by its source file.
2. Click a card to select it (Ctrl/Shift-click for several). **Alt+↑/↓** or the arrow buttons move the selection; drag cards to reorder; **Exclude/Include** toggles pages; rotate, duplicate, insert a blank page.
3. Choose what to merge into (PDF keeps the PDF pages unchanged) and press **Merge Files**. The result is verified and offered for download. **Open in PDF Editor** continues with it.
4. **Edit Files** opens the first selected PDF in the editor. **Clear** empties the list.

If a PDF is password protected you are asked for the password; it is kept in memory only.

## PDF Editor

Open a PDF (Open PDF…, or from another workspace). The strip on the left shows pages; the viewer in the middle; tools on the right.

* **Pages** - rotate, move, delete, duplicate, insert a blank page, extract selected pages, split into one file per page, insert pages from another open PDF, crop.
* **Annotate** - choose a tool (highlight, underline, strike-out, comment, rectangle, ellipse, freehand) and draw. Marks stay as **drafts** until you press "Add annotations to the PDF"; they then become standard PDF annotations other viewers show. Existing annotations are listed and can be removed.
* **Add** - watermark, page numbers, text at a point, text boxes, images.
* **Redact** - find text, scan for personal data, or draw areas; mark them, then **Permanently redact**. The marked pages become images with black boxes; the saved file is checked for leftover text and metadata and discarded if the check fails. Text outside the visible page area on a redacted page is removed with the page. After redaction the page text is no longer selectable (run OCR if you need it back).
* **Forms** - fill fields; optionally flatten. Signature fields are never filled or signed.
* **Document** - properties, bookmarks, password (set, change, remove), clean-up (remove hidden data), compress, OCR.
* **Find** - search the text of the open PDF.

**Undo/Redo** (top right) step through the revisions of this document; the revision list lets you jump back. **Save a copy** writes the current version to your output folder. If the PDF's permissions do not allow an action, the panel tells you and asks for the owner password.

## Reader & Analysis

Drop a document and use the tabs: Overview, Structure (outline and problems found), Words & readability, Summary (extractive: sentences quoted from the document), Personal data (what was found, with page links; "Redact these in the PDF Editor…"), Sections (check against a preset or your own list), Compare (two documents), Search.

SLAI agents may read document text by default; they run on this computer. The Privacy agent decides first, on every use, whether the text may be handed on (text with personal data is not), the Safety agent reviews it, and the Language agent can add a machine-written summary - which is discarded unless its words really come from the document. Every answer says which agent contributed. To keep agents away from one document, switch off "Let SLAI agents read this document"; to switch them off for everything use the System page or start with `--agents none`. On the Structure tab of a PDF the Quality agent reviews per-page extraction statistics (no text is sent).

The System page has "Test agents now": one real call per agent, with the result shown beside it.

## AI Assistant

Ask in plain language, for example: "summarise report.pdf", "how many words are in notes.docx?", "what does the contract say about notice periods?", "find personal data in invoice.pdf", "redact the email addresses in invoice.pdf", "merge a.pdf and b.pdf into one PDF", "convert notes.docx to Markdown", "hoeveel woorden staan er in verslag.pdf?".

* Reading answers appear at once, with the passages they come from (document, page). If the answer is not in the document, the assistant says so.
* Anything that changes a file shows a **proposal** with its consequences. Nothing happens until you press **Confirm**. A proposal works once, for 15 minutes, and only for the document version it was made for.
* Use "Working with" when several open documents share a name.
* "What can you do?" lists every tool and whether it asks first.

## When something is missing

The **System** page shows what is installed (LibreOffice, Tesseract, qpdf, Python packages) and the state of each SLAI agent. Features that need something missing are greyed out with the reason.
