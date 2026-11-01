"""Reader search API filtering on real document evidence."""
import unittest

from support import Harness, make_pdf, needs_slai


@needs_slai
class ReaderSearchFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = Harness()

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def test_whole_word_and_page_filter(self):
        doc = self.h.upload("search.pdf", make_pdf([["catalog cat Catfish"], ["cat on second page"]]))
        result = self.h.call("POST", "/api/analysis/search", {
            "docs": [doc["id"]], "query": "cat", "wholeWord": True, "case": True, "pageFrom": 2, "pageTo": 2})
        self.assertEqual(result["total"], 1)
        self.assertFalse(result["truncated"])
        self.assertEqual(result["results"][0]["hits"][0]["evidence"]["page"], 2)

    def test_invalid_page_range_is_rejected(self):
        doc = self.h.upload("search.pdf", make_pdf([["text"]]))
        from support import ApiError
        with self.assertRaises(ApiError):
            self.h.call("POST", "/api/analysis/search", {
                "docs": [doc["id"]], "query": "text", "pageFrom": 4, "pageTo": 2})


if __name__ == "__main__":
    unittest.main()
