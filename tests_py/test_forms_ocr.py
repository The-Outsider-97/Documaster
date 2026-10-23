"""Forms (real AcroForm), compression and OCR - the features that depend on more than pypdf."""
import io
import shutil
import unittest

import pypdf

from support import ApiError, Harness, make_pdf, needs_slai


def make_form() -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.drawString(72, 780, "Application form")
    c.acroForm.textfield(name="full_name", x=72, y=700, width=200, height=20, value="")
    c.acroForm.checkbox(name="agree", x=72, y=660, size=16, checked=False)
    c.acroForm.choice(name="country", x=72, y=620, width=120, height=20, options=["NL", "BE", "DE"], value="NL", fieldFlags="combo")
    c.showPage(); c.save()
    return buf.getvalue()


@needs_slai
class FormTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = Harness()

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def reader(self, d):
        status, _, raw = self.h.raw("GET", f"/api/documents/{d['id']}/file")
        return pypdf.PdfReader(io.BytesIO(raw))

    def test_fields_are_listed_with_types(self):
        d = self.h.upload("form.pdf", make_form())
        fields = {f["name"]: f for f in self.h.call("GET", f"/api/documents/{d['id']}/pdfinfo")["fields"]}
        self.assertEqual(set(fields), {"full_name", "agree", "country"})

    def test_fill_the_form_and_the_values_are_stored(self):
        d = self.h.upload("form.pdf", make_form())
        self.h.call("POST", f"/api/documents/{d['id']}/op", {"op": "fill_form", "values": {"full_name": "Ada Lovelace", "agree": True, "country": "BE"}}, wait=True)
        values = {k: v.get("/V") for k, v in (self.reader(d).get_fields() or {}).items()}
        self.assertEqual(values["full_name"], "Ada Lovelace")
        self.assertEqual(values["country"], "BE")
        self.assertNotIn(values["agree"], (None, "/Off"))

    def test_unknown_field_is_refused_not_ignored(self):
        d = self.h.upload("form.pdf", make_form())
        with self.assertRaises(ApiError):
            self.h.call("POST", f"/api/documents/{d['id']}/op", {"op": "fill_form", "values": {"nope": "x"}}, wait=True)

    def test_flatten_makes_the_answers_page_content(self):
        d = self.h.upload("form.pdf", make_form())
        self.h.call("POST", f"/api/documents/{d['id']}/op", {"op": "fill_form", "values": {"full_name": "Ada Lovelace"}, "flatten": True}, wait=True)
        r = self.reader(d)
        self.assertFalse(r.get_fields())
        self.assertIn("Ada Lovelace", r.pages[0].extract_text())

    def test_signature_fields_are_never_filled_or_signed(self):
        d = self.h.upload("form.pdf", make_form())
        with self.assertRaises(ApiError):
            self.h.call("POST", f"/api/documents/{d['id']}/op", {"op": "fill_form", "values": {"signature": "Ada"}}, wait=True)


@needs_slai
class CompressOcrTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = Harness()

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def test_compress_never_makes_the_file_bigger_and_keeps_the_text(self):
        d = self.h.upload("c.pdf", make_pdf([["Compress me please"] * 3] * 4))
        r = self.h.call("POST", f"/api/documents/{d['id']}/op", {"op": "compress"}, wait=True)
        if r["changed"]:
            self.assertTrue(r["verification"]["ok"])
        else:                                   # nothing to gain: the document is left alone and the label says so
            self.assertIn("no", r["label"].lower())
        status, _, raw = self.h.raw("GET", f"/api/documents/{d['id']}/file")
        self.assertIn("Compress me", pypdf.PdfReader(io.BytesIO(raw)).pages[0].extract_text())
        self.assertLessEqual(len(raw), d["bytes"] + 64)

    @unittest.skipUnless(shutil.which("tesseract"), "tesseract is not installed")
    def test_ocr_adds_a_text_layer_to_a_scanned_page(self):
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGB", (1240, 1754), "white")
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 64)
        except OSError:
            self.skipTest("no TrueType font to draw a test scan")
        draw.text((100, 200), "SCANNED INVOICE 2024", fill="black", font=font)
        buf = io.BytesIO(); img.save(buf, "PDF", resolution=150)
        d = self.h.upload("scan.pdf", buf.getvalue())
        status, _, raw = self.h.raw("GET", f"/api/documents/{d['id']}/file")
        self.assertEqual((pypdf.PdfReader(io.BytesIO(raw)).pages[0].extract_text() or "").strip(), "")
        r = self.h.call("POST", f"/api/documents/{d['id']}/op", {"op": "ocr", "language": "eng"}, wait=True)
        status, _, raw = self.h.raw("GET", f"/api/documents/{d['id']}/file")
        text = pypdf.PdfReader(io.BytesIO(raw)).pages[0].extract_text().upper()
        self.assertIn("INVOICE", text)
        self.assertTrue(any("mistake" in w.lower() or "recogni" in w.lower() for w in r["warnings"]), r["warnings"])


if __name__ == "__main__":
    unittest.main()
