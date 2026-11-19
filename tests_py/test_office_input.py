"""Reading DOC / RTF (through LibreOffice) and the LibreOffice engine's HTML routes. These all failed before: every DOC and RTF read raised
``ValueError: WD_PARAGRAPH_ALIGNMENT has no XML mapping for 'start'`` and HTML -> DOCX / RTF had no export filter."""
import io
import json
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from support import Harness, make_docx, needs_slai
from slai.services import Tools

HAVE_SOFFICE = bool(Tools.discover().soffice)


def via_soffice(data: bytes, name: str, ext: str) -> bytes:
    with tempfile.TemporaryDirectory() as t:
        src = Path(t) / name
        src.write_bytes(data)
        subprocess.run([shutil.which("soffice") or "soffice", f"-env:UserInstallation={Path(t, 'p').as_uri()}", "--headless",
                        "--convert-to", ext, "--outdir", t, str(src)], capture_output=True, timeout=180, check=True)
        return (Path(t) / (src.stem + "." + ext)).read_bytes()


class JcTests(unittest.TestCase):
    """The reader must accept every w:jc value, not only the ones python-docx names."""

    def paragraph_align(self, jc: str):
        import docx
        from slai.docmodel.read_docx import _alignment
        doc = docx.Document()
        p = doc.add_paragraph("text")
        ppr = p._p.get_or_add_pPr()
        from docx.oxml import parse_xml
        ppr.append(parse_xml(f'<w:jc xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" w:val="{jc}"/>'))
        return _alignment(p.paragraph_format)

    def test_every_jc_value(self):
        for jc, expected in (("left", 0), ("start", 0), ("center", 1), ("right", 2), ("end", 2), ("both", 3), ("distribute", 4), ("unknown-value", None)):
            with self.subTest(jc=jc):
                self.assertEqual(self.paragraph_align(jc), expected)


@needs_slai
@unittest.skipUnless(HAVE_SOFFICE, "LibreOffice is needed to make and read DOC / RTF")
class OfficeInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = Harness()
        cls.docx = make_docx(["Zanzibar is the marker word.", "A second paragraph."], heading="Probe")

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def convert(self, name: str, data: bytes, target: str, engine: str = "model") -> dict:
        doc = self.h.upload(name, data)
        return self.h.job("POST", "/api/convert", {"docs": [doc["id"]], "options": {"target": target, "engine": engine}}, timeout=240)["results"][0]

    def text(self, res: dict, fmt: str) -> str:
        data = self.h.download(res["report"]["output"])
        if fmt == "pdf":
            from pypdf import PdfReader
            return "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages)
        if fmt == "docx":
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                return z.read("word/document.xml").decode()
        return data.decode("utf-8", "replace")

    def test_rtf_and_doc_convert_to_text_pdf_and_docx(self):
        for ext in ("rtf", "doc"):
            data = via_soffice(self.docx, "probe.docx", ext)
            for target in ("txt", "pdf", "docx", "md"):
                with self.subTest(source=ext, target=target):
                    res = self.convert(f"probe.{ext}", data, target)
                    self.assertTrue(res["ok"], res)
                    self.assertIn("Zanzibar", self.text(res, target))

    def test_html_through_the_libreoffice_engine(self):
        html = b"<html><body><h1>Probe</h1><p>Zanzibar is the marker word.</p></body></html>"
        for target in ("docx", "rtf", "odt", "pdf", "txt"):
            with self.subTest(target=target):
                res = self.convert("probe.html", html, target, engine="libreoffice")
                self.assertTrue(res["ok"], res)
                self.assertEqual(res["report"]["engine"], "libreoffice")


if __name__ == "__main__":
    unittest.main()
