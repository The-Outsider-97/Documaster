"""Regression tests for preservation of editable layout during DOCX/PDF import.

Run: SLAI_ROOT=/path/to/SLAI python -m unittest discover -s tests -p 'test_import_fidelity.py' -v
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from slai.bridge import activate, sldc
from slai.docmodel.ir import build_package
from slai.docmodel.read_docx import read_docx
from slai.docmodel.read_pdf_layout import read_pdf_layout
from slai.docmodel.writers import WriteOptions
from slai.docmodel.writers.write_docx import write_docx
from slai.docmodel.writers.write_pdf import write_pdf


class ImportFidelityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = os.environ.get("SLAI_ROOT")
        if not root:
            raise unittest.SkipTest("Set SLAI_ROOT to the SLAI v2.4 checkout")
        activate(Path(root))

    def test_docx_preserves_font_size_page_geometry_headers_footer_table(self) -> None:
        from docx import Document
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Cm, Pt, RGBColor

        original = Document()
        section = original.sections[0]
        section.top_margin = Cm(2.3)
        section.bottom_margin = Cm(2.7)
        section.left_margin = Cm(3.2)
        section.right_margin = Cm(1.8)
        section.header.paragraphs[0].text = "SLAI Writer"
        section.footer.paragraphs[0].text = "20 October 2026"
        para = original.add_paragraph(style="Heading 1")
        para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = para.add_run("Information Page")
        run.font.name = "Georgia"
        run.font.size = Pt(19)
        run.font.color.rgb = RGBColor(34, 51, 68)
        run.bold = True
        body = original.add_paragraph()
        body.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        run = body.add_run("Bold ")
        run.bold = True
        run = body.add_run("italic")
        run.italic = True
        run.underline = True
        tbl = original.add_table(rows=2, cols=2)
        tbl.cell(0, 0).text = "Field"
        tbl.cell(0, 1).text = "Publication record"
        tbl.cell(1, 0).text = "Author"
        tbl.cell(1, 1).text = "Remy"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "whitepaper.docx"
            original.save(path)
            result = read_docx(str(path))
        package = build_package("sample", result["blocks"], sections=result["sections"])
        sldc().validate_package(package)
        model = package.document
        self.assertAlmostEqual(model["sections"][0]["page"]["margins_mm"]["left"], 32, delta=.1)
        self.assertEqual(model["sections"][0]["header"]["left"], "SLAI Writer")
        self.assertEqual(model["sections"][0]["footer"]["left"], "20 October 2026")
        heading = model["blocks"][0]
        self.assertEqual(heading["attributes"]["align"], "center")
        self.assertTrue(any(s.get("font_family") == "Georgia" and s.get("font_size_pt") == 19
                            and s.get("bold") is True for s in heading["spans"]))
        self.assertTrue(any(s.get("color") == "#223344" for s in heading["spans"]))
        self.assertEqual(model["blocks"][1]["attributes"]["align"], "justify")
        self.assertTrue(any(s.get("underline") for s in model["blocks"][1]["spans"]))
        self.assertEqual(model["blocks"][2]["type"], "table")
        self.assertEqual(model["blocks"][2]["table"]["rows"][1][1]["text"], "Remy")

    def test_pdf_positioned_text_preserves_header_footer_fonts_and_page_breaks(self) -> None:
        try:
            from reportlab.pdfgen import canvas
        except ImportError:
            self.skipTest("reportlab not installed")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "whitepaper.pdf"
            draw = canvas.Canvas(str(path))
            for page in (1, 2):
                draw.setFont("Helvetica", 9)
                draw.drawString(72, 795, "SLAI Writer")
                draw.drawString(72, 40, "20 October 2026")
                draw.drawRightString(520, 40, str(page))
                draw.setFont("Helvetica-Bold", 18)
                draw.drawString(72, 710, "Information Page" if page == 1 else "Production Note")
                draw.setFont("Helvetica", 11)
                draw.drawString(72, 680, "A normal paragraph with regular formatting.")
                draw.showPage()
            draw.save()
            data = read_pdf_layout(path, "whitepaper.pdf")
        self.assertIsNotNone(data)
        assert data is not None
        package = build_package("sample", data["blocks"], sections=data["sections"])
        sldc().validate_package(package)
        section = package.document["sections"][0]
        self.assertEqual(section["header"]["left"], "SLAI Writer")
        self.assertEqual(section["footer"]["right"], "{page}")
        self.assertLess(section["page"]["margins_mm"]["bottom"], 45)
        self.assertEqual(len(package.document["blocks"]), 4)
        self.assertTrue(package.document["blocks"][2]["attributes"]["page_break_before"])
        self.assertTrue(any(s.get("font_size_pt") == 18 for s in package.document["blocks"][0]["spans"]))

    def test_mixed_header_formatting_round_trips_to_docx_and_pdf(self) -> None:
        from docx import Document

        section = {"id": "import_section_1", "page": {"size": "A4"},
                   "header": {"left": "SLAI Writer", "runs": {"left": [
                       {"text": "SLAI ", "format": {"font_size_pt": 9}},
                       {"text": "Writer", "format": {"bold": True, "color": "#ff0000"}}]}},
                   "footer": {"right": "{page}"}}
        pkg = build_package("Test", [sldc().make_block("paragraph", "Document content")], sections=[section])
        sldc().validate_package(pkg)
        with tempfile.TemporaryDirectory() as tmp:
            docx_path = Path(tmp) / "mixed.docx"
            pdf_path = Path(tmp) / "mixed.pdf"
            write_docx(pkg, docx_path, WriteOptions())
            write_pdf(pkg, pdf_path, WriteOptions())
            self.assertGreater(pdf_path.stat().st_size, 200)
            runs = Document(docx_path).sections[0].header.paragraphs[0].runs
            self.assertEqual(runs[0].text, "SLAI ")
            self.assertEqual(runs[1].text, "Writer")
            self.assertTrue(runs[1].bold)
            self.assertEqual(str(runs[1].font.color.rgb), "FF0000")
            imported = read_docx(str(docx_path))
            self.assertEqual(imported["sections"][0]["header"]["left"], "SLAI Writer")
            self.assertEqual([r["text"] for r in imported["sections"][0]["header"]["runs"]["left"][:2]], ["SLAI ", "Writer"])
            self.assertEqual(imported["sections"][0]["footer"]["right"], "{page}")

    def test_table_cell_character_styles_survive_docx_import_and_export(self) -> None:
        from docx import Document
        from docx.shared import Pt, RGBColor

        source = Document()
        table = source.add_table(rows=1, cols=2)
        p = table.cell(0, 0).paragraphs[0]
        a = p.add_run("Important ")
        a.bold = True
        b = p.add_run("detail")
        b.italic = True
        b.underline = True
        b.font.size = Pt(12)
        b.font.name = "Georgia"
        b.font.color.rgb = RGBColor(0x20, 0x30, 0x40)
        table.cell(0, 1).text = "Ordinary"
        with tempfile.TemporaryDirectory() as tmp:
            source_path = Path(tmp) / "source.docx"
            exported_path = Path(tmp) / "exported.docx"
            source.save(source_path)
            imported = read_docx(str(source_path))
            package = build_package("table", imported["blocks"], sections=imported["sections"])
            sldc().validate_package(package)
            first = package.document["blocks"][0]["table"]["rows"][0][0]
            self.assertEqual(first["text"], "Important detail")
            self.assertTrue(any(span.get("bold") for span in first.get("spans", [])))
            self.assertTrue(any(span.get("italic") and span.get("underline") for span in first.get("spans", [])))
            self.assertTrue(any(span.get("font_size_pt") == 12 and span.get("font_family") == "Georgia"
                                for span in first.get("spans", [])))
            write_docx(package, exported_path, WriteOptions())
            result_runs = Document(exported_path).tables[0].cell(0, 0).paragraphs[0].runs
            self.assertTrue(any(run.bold for run in result_runs))
            self.assertTrue(any(run.italic and run.underline for run in result_runs))
            self.assertTrue(any(run.font.size and round(run.font.size.pt) == 12 for run in result_runs))

    def test_pdf_table_grid_is_reconstructed_as_editable_cells(self) -> None:
        from reportlab.pdfgen import canvas

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "table.pdf"
            c = canvas.Canvas(str(path))
            c.setFont("Helvetica", 10)
            for y in (700, 670, 640, 610):
                c.line(75, y, 525, y)
            for x in (75, 260, 525):
                c.line(x, 610, x, 700)
            for text, x, y in (("Field", 85, 681), ("Publication record", 270, 681),
                               ("Author", 85, 651), ("Jean-Erolle Remy", 270, 651),
                               ("Version", 85, 621), ("1.0", 270, 621)):
                c.drawString(x, y, text)
            c.save()
            data = read_pdf_layout(path, "table.pdf")
        self.assertIsNotNone(data)
        assert data is not None
        tables = [b for b in data["blocks"] if b["type"] == "table"]
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0]["table"]["columns"], 2)
        self.assertEqual(tables[0]["table"]["rows"][1][1]["text"], "Jean-Erolle Remy")
        sldc().validate_package(build_package("table", data["blocks"], sections=data["sections"]))

    def test_references_semantic_page_break_exports_to_pdf_and_docx(self) -> None:
        from docx import Document
        import fitz

        blocks = [
            sldc().make_block("paragraph", "A conclusion with space left on the page."),
            sldc().make_block("heading", "References", attributes={"level": 1, "align": "center",
                                                              "page_break_before": True}),
            sldc().make_block("paragraph", "Remy, J.-E. (2026). Research work.",
                              attributes={"line_height": 2, "indent_mm": 12.7,
                                          "first_line_indent_mm": -12.7}),
        ]
        package = build_package("academic", blocks)
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "references.pdf"
            docx_path = Path(tmp) / "references.docx"
            write_pdf(package, pdf_path, WriteOptions())
            write_docx(package, docx_path, WriteOptions())
            with fitz.open(str(pdf_path)) as pdf:
                self.assertGreaterEqual(pdf.page_count, 2)
                self.assertIn("A conclusion", pdf[0].get_text())
                self.assertNotIn("References", pdf[0].get_text())
                self.assertIn("References", pdf[1].get_text())
            paragraphs = Document(docx_path).paragraphs
            heading = next(p for p in paragraphs if p.text == "References")
            self.assertTrue(heading.paragraph_format.page_break_before)


if __name__ == "__main__":
    unittest.main()
