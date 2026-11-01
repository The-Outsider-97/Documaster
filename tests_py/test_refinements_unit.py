"""Focused tests that do not need an installed SLAI checkout or a running server."""
import io
import unittest

from slai.analysis.insights import search
from slai.analysis.model import Unit
from slai.pdfops import annotate
from slai.errors import ValidationFailed
from support import make_pdf


class ReaderSearchTests(unittest.TestCase):
    def setUp(self):
        self.units = [
            Unit("d", "Sample", "b1", 0, 1, 1, "paragraph", "A catalog contains a cat, not a Catfish."),
            Unit("d", "Sample", "b2", 1, 2, 2, "paragraph", "A separate cat appears on page two."),
        ]

    def test_whole_word_and_case(self):
        matches = search(self.units, "cat", whole_word=True, case=True)
        self.assertEqual([m["match"] for m in matches], ["cat", "cat"])
        self.assertEqual(matches[0]["evidence"]["page"], 1)

    def test_page_range_and_evidence(self):
        matches = search(self.units, "cat", whole_word=True, page_from=2, page_to=2)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["evidence"]["page"], 2)

    def test_bounded_regex(self):
        try:
            import regex  # noqa: F401
        except ImportError:
            self.skipTest("optional regex dependency not installed")
        matches = search(self.units, r"cat\w*", regex=True, case=False)
        self.assertEqual(len(matches), 4)
        with self.assertRaises((TimeoutError, ValueError)):
            search([Unit("d", "Sample", "b", 0, 1, 1, "paragraph", "a" * 6000 + "!")],
                   r"(a+)+$", regex=True)


class AnnotationEditTests(unittest.TestCase):
    def test_edit_existing_note_preserves_identity_and_refuses_other_kinds(self):
        from pypdf import PdfReader, PdfWriter

        w = PdfWriter(clone_from=PdfReader(io.BytesIO(make_pdf([["Document"]]))))
        note_id = annotate.add(w, {"type": "note", "page": 1, "point": [0.4, 0.4], "text": "Original"}, 1, "Tester")[0]
        self.assertTrue(annotate.edit_note(w, {"id": note_id}, "Revised note"))
        self.assertFalse(annotate.edit_note(w, {"id": note_id}, "Revised note"))
        buf = io.BytesIO()
        w.write(buf)
        found = annotate.listing(PdfReader(io.BytesIO(buf.getvalue())))
        self.assertEqual(found[0]["id"], note_id)
        self.assertEqual(found[0]["text"], "Revised note")
        with self.assertRaises(ValidationFailed):
            annotate.edit_note(w, {"id": note_id}, "")
        rect_id = annotate.add(w, {"type": "rect", "page": 1, "rect": [0.1, 0.1, 0.2, 0.2]}, 1, "Tester")[0]
        with self.assertRaises(ValidationFailed):
            annotate.edit_note(w, {"id": rect_id}, "not a note")


if __name__ == "__main__":
    unittest.main()
