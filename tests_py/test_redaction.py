"""Redaction must remove content, not paint over it - and must say so honestly when it cannot."""
import io
import unittest

from pypdf import PdfReader

from support import Harness, PII_LINES, make_pdf, needs_slai


def text_of(data: bytes) -> str:
    return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)


@needs_slai
class RedactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = Harness()

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def doc(self, pages=None, **kw):
        return self.h.upload("r.pdf", make_pdf(pages or [PII_LINES, ["Second page", "jane.doe@example.com appears again"]], **kw))

    def redact(self, doc, query, **kw):
        hits = self.h.call("POST", f"/api/documents/{doc['id']}/find", {"query": query})["hits"]
        regions = [{"page": hit["page"], "rect": r} for hit in hits for r in hit["rects"]]
        self.assertTrue(regions, "the text should have been found")
        return self.h.call("POST", f"/api/documents/{doc['id']}/op", {"op": "redact", "regions": regions, **kw}, wait=True)

    def current(self, doc):
        status, _, raw = self.h.raw("GET", f"/api/documents/{doc['id']}/file")
        self.assertEqual(status, 200)
        return raw

    def test_redacted_text_is_gone_from_the_saved_file(self):
        d = self.doc(title="Secret jane.doe@example.com", author="Jane Doe")
        r = self.redact(d, "jane.doe@example.com")
        self.assertTrue(r["verification"]["ok"], r["verification"])
        data = self.current(d)
        self.assertNotIn("jane.doe", text_of(data))
        self.assertNotIn(b"jane.doe", data)
        meta = PdfReader(io.BytesIO(data)).metadata or {}
        self.assertNotIn("Jane", " ".join(str(v) for v in meta.values()))
        self.assertNotIn("Secret", " ".join(str(v) for v in meta.values()))

    def test_other_text_on_a_redacted_page_is_rasterised_not_lost_silently(self):
        d = self.doc()
        r = self.redact(d, "jane.doe@example.com")
        self.assertTrue(any("image" in w.lower() for w in r["warnings"]), r["warnings"])

    def test_verification_lists_what_it_checked(self):
        d = self.doc()
        r = self.redact(d, "NL91 ABNA 0417 1643 00")
        names = " ".join(str(c.get("name") or c.get("label") or c) for c in r["verification"]["checks"]).lower()
        self.assertIn("text", names)
        self.assertGreaterEqual(len(r["verification"]["checks"]), 3)

    def test_a_match_that_runs_off_the_page_still_removes_the_text(self):
        d = self.h.upload("off.pdf", make_pdf([["start " + "word " * 30 + "SECRETMARK tail"]]))
        r = self.redact(d, "SECRETMARK")
        self.assertTrue(r["verification"]["ok"])
        self.assertNotIn("SECRETMARK", text_of(self.current(d)))

    def test_regions_that_lie_wholly_off_the_page_are_removed_with_the_page(self):
        d = self.doc()
        r = self.h.call("POST", f"/api/documents/{d['id']}/op", {"op": "redact", "regions": [
            {"page": 0, "rect": [0.1, 0.1, 0.4, 0.2]}, {"page": 1, "rect": [1.05, 0.2, 1.4, 0.25]}]}, wait=True)
        self.assertTrue(r["verification"]["ok"])
        self.assertTrue(any("outside the visible page" in w for w in r["warnings"]), r["warnings"])

    def test_undo_restores_the_previous_version(self):
        d = self.doc()
        self.redact(d, "jane.doe@example.com")
        self.h.call("POST", f"/api/documents/{d['id']}/undo")
        self.assertIn("jane.doe@example.com", text_of(self.current(d)))

    def test_empty_or_off_page_regions_are_rejected(self):
        d = self.doc()
        from support import ApiError
        for regions in ([], [{"page": 99, "rect": [0.1, 0.1, 0.2, 0.2]}], [{"page": 0, "rect": [0.5, 0.5, 0.5, 0.5]}]):
            with self.assertRaises(ApiError):
                self.h.call("POST", f"/api/documents/{d['id']}/op", {"op": "redact", "regions": regions}, wait=True)

    def test_personal_data_scan_reports_where_it_was_found(self):
        d = self.doc()
        r = self.h.call("POST", f"/api/documents/{d['id']}/pii", {})
        labels = {i["label"] for i in r["items"]}
        self.assertIn("Email address", labels)
        self.assertIn("Bank account (IBAN)", labels)
        self.assertIn("not detected", r["note"])          # the limits of the detector are stated

    def test_pii_without_a_text_layer_says_it_could_not_look(self):
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=A4)
        c.rect(100, 100, 200, 200, fill=1)
        c.showPage(); c.save()
        d = self.h.upload("blank.pdf", buf.getvalue())
        r = self.h.call("POST", f"/api/documents/{d['id']}/pii", {})
        self.assertEqual(r["items"], [])
        self.assertTrue("text layer" in r["note"].lower() or "no text" in r["note"].lower(), r["note"])


if __name__ == "__main__":
    unittest.main()
