"""Wrong input is the caller's mistake: it must produce a 4xx with a message, never a 500 and never a traceback in the log."""
import json
import unittest

from support import Harness, make_pdf, needs_slai


@needs_slai
class BadInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = Harness()
        cls.id = cls.h.upload("b.pdf", make_pdf([[f"page {i}"] for i in range(5)]))["id"]

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def send(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        status, _, raw = self.h.raw(method, path, data, {"Content-Type": "application/json"} if data is not None else None)
        return status, raw

    def assertClientError(self, method, path, body=None):
        status, raw = self.send(method, path, body)
        self.assertTrue(400 <= status < 500, f"{method} {path} {body!r} -> {status} {raw[:150]!r}")
        self.assertFalse(json.loads(raw)["ok"])

    def test_page_image_parameters(self):
        for tail in ("0/image?w=abc", "0/image?w=1e9", "abc/image", "1.5/image", "-1/image", "99999/image"):
            with self.subTest(tail=tail):
                self.assertClientError("GET", f"/api/documents/{self.id}/pages/{tail}")

    def test_count_text_parameters(self):
        for body in ({"text": "x", "top": "abc"}, {"text": "x", "top": 0}, {"text": "x", "top": 10 ** 9}, {"text": "x", "goals": "zzz"},
                     {"text": "x", "goals": {"minWords": "a"}}, {"text": "x", "goals": {"minWords": -1}}, {"text": None}):
            with self.subTest(body=body):
                self.assertClientError("POST", "/api/count/text", body)

    def test_document_lists_must_be_lists_of_identifiers(self):
        for path in ("/api/count/documents", "/api/convert", "/api/merge/outline"):
            for docs in ([{"a": 1}], "x", [], [None], [[1]], [1, 2], None):
                with self.subTest(path=path, docs=docs):
                    self.assertClientError("POST", path, {"docs": docs, "options": {"target": "pdf"}, "target": "pdf"})

    def test_convert_options(self):
        for options in ({"target": "pdf", "font_size_pt": "abc"}, {"target": "pdf", "margins_mm": "x"}, {"target": {}}, {"target": ["pdf"]},
                        {"target": "pdf", "font_size_pt": 1e9}, {"target": "pdf", "font_size_pt": -5}, {"target": "pdf", "font_size_pt": True}):
            with self.subTest(options=options):
                self.assertClientError("POST", "/api/convert", {"docs": [self.id], "options": options})

    def test_merge_and_outline_parameters(self):
        for body in ({"docs": [self.id], "splitHeadings": "x"}, {"docs": [self.id], "splitHeadings": 99}, {"docs": [self.id], "splitHeadings": -1}):
            with self.subTest(body=body):
                self.assertClientError("POST", "/api/merge/outline", body)

    def test_revision_number(self):
        for n in ("abc", "99", "-1", "1.5"):
            with self.subTest(n=n):
                self.assertClientError("POST", f"/api/documents/{self.id}/revision/{n}")

    def test_valid_requests_still_work(self):
        status, raw = self.send("GET", f"/api/documents/{self.id}/pages/0/image?w=300")
        self.assertEqual(status, 200)
        status, raw = self.send("POST", "/api/count/text", {"text": "one two three", "top": 5, "goals": {"minWords": 2}})
        self.assertEqual(json.loads(raw)["data"]["words"], 3)
        status, raw = self.send("POST", "/api/merge/outline", {"docs": [self.id], "splitHeadings": 0})
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
