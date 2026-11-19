"""XLSX / PPTX / ODP are converted to PDF by LibreOffice only. These tests run the real LibreOffice through the real server."""
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from support import ApiError, Harness, needs_slai

try:
    import openpyxl
except ImportError:                                           # pragma: no cover
    openpyxl = None
try:
    import pptx
except ImportError:                                           # pragma: no cover
    pptx = None
from slai.services import Tools

HAVE_SOFFICE = bool(Tools.discover().soffice)


def xlsx_bytes() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sales"
    ws.append(["Region", "Quarter one"])
    ws.append(["Northland", 10])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def pptx_bytes() -> bytes:
    prs = pptx.Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "Quarterly Review"
    slide.placeholders[1].text = "Zebrafish migration"
    out = io.BytesIO()
    prs.save(out)
    return out.getvalue()


@needs_slai
class LayoutOnlyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = Harness()

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def upload(self, name: str, data: bytes, flag: bool = True) -> dict:
        status, _, raw = self.h.raw("POST", f"/api/documents?name={name}" + ("&images=1" if flag else ""), data,
                                    {"Content-Type": "application/octet-stream"})
        payload = json.loads(raw)
        if status >= 400:
            raise ApiError(status, payload)
        return payload["data"]

    def convert(self, doc: dict, target: str = "pdf") -> dict:
        return self.h.job("POST", "/api/convert", {"docs": [doc["id"]], "options": {"target": target, "engine": "model"}}, timeout=240)["results"][0]

    def pdf_text(self, result: dict) -> str:
        from pypdf import PdfReader
        data = self.h.download(result["report"]["output"])
        return "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages)

    @unittest.skipUnless(openpyxl and HAVE_SOFFICE, "openpyxl and LibreOffice are needed")
    def test_xlsx_to_pdf_carries_the_cell_text(self):
        doc = self.upload("sales.xlsx", xlsx_bytes())
        self.assertEqual(doc["format"], "xlsx")
        result = self.convert(doc)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["report"]["engine"], "libreoffice")        # forced: there is no model route
        text = self.pdf_text(result)
        for word in ("Region", "Northland", "Quarter"):
            self.assertIn(word, text)

    @unittest.skipUnless(pptx and HAVE_SOFFICE, "python-pptx and LibreOffice are needed")
    def test_pptx_and_odp_to_pdf(self):
        doc = self.upload("review.pptx", pptx_bytes())
        self.assertEqual(doc["format"], "pptx")
        first = self.convert(doc)
        self.assertTrue(first["ok"], first)
        self.assertIn("Zebrafish", self.pdf_text(first))
        with tempfile.TemporaryDirectory() as t:                          # ODP made by LibreOffice itself from the PPTX
            src = Path(t) / "review.pptx"
            src.write_bytes(pptx_bytes())
            subprocess.run([shutil.which("soffice") or "soffice", f"-env:UserInstallation={Path(t, 'p').as_uri()}", "--headless",
                            "--convert-to", "odp", "--outdir", t, str(src)], capture_output=True, timeout=180, check=True)
            odp = (Path(t) / "review.odp").read_bytes()
        doc = self.upload("review.odp", odp)
        self.assertEqual(doc["format"], "odp")
        second = self.convert(doc)
        self.assertTrue(second["ok"], second)
        self.assertIn("Zebrafish", self.pdf_text(second))

    @unittest.skipUnless(openpyxl, "openpyxl is needed")
    def test_only_pdf_is_offered_and_other_targets_fail_cleanly(self):
        doc = self.upload("sales.xlsx", xlsx_bytes())
        routes = [r for r in self.h.call("GET", "/api/convert/formats")["routes"] if r["from"] == "xlsx"]
        self.assertEqual([r["to"] for r in routes], ["pdf"])
        self.assertEqual(routes[0]["engineOnly"], "libreoffice")
        self.assertFalse(routes[0]["model"]["available"])
        bad = self.convert(doc, "docx")
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error"]["code"], "unsupported")

    @unittest.skipUnless(openpyxl, "openpyxl is needed")
    def test_other_tools_refuse_these_files_with_a_reason(self):
        with self.assertRaises(ApiError) as caught:
            self.upload("sales.xlsx", xlsx_bytes(), flag=False)
        self.assertEqual(caught.exception.status, 422)
        self.assertIn("converted to PDF", json.dumps(caught.exception.body))

    @unittest.skipUnless(openpyxl, "openpyxl is needed")
    def test_counting_a_layout_only_file_is_a_clean_error_not_a_crash(self):
        doc = self.upload("sales.xlsx", xlsx_bytes())
        out = self.h.job("POST", "/api/count/documents", {"docs": [doc["id"]]}, timeout=60)
        self.assertFalse(out["results"][0]["ok"])
        self.assertEqual(out["results"][0]["error"]["code"], "unsupported")

    def test_a_broken_archive_with_an_office_name_is_rejected(self):
        for name in ("empty.pptx", "broken.xlsx", "broken.odp"):
            with self.subTest(name=name), self.assertRaises(ApiError) as caught:
                self.upload(name, b"PK\x03\x04 broken archive")
            self.assertGreaterEqual(caught.exception.status, 400)
            self.assertLess(caught.exception.status, 500)

    def test_plain_text_named_xlsx_is_treated_as_text_and_flagged(self):
        doc = self.upload("fake.xlsx", b"this is plain text, not a workbook")
        self.assertEqual(doc["format"], "txt")                          # identified from its bytes, never trusted by name
        self.assertTrue(any("named like" in w for w in doc["warnings"]), doc["warnings"])


if __name__ == "__main__":
    unittest.main()
