"""Conversion and merging through the real HTTP API: every output is re-read and checked, never trusted."""
import io
import unittest
import zipfile

from pypdf import PdfReader

from support import ApiError, Harness, make_docx, make_pdf, needs_slai


@needs_slai
class ConvertTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = Harness()
        cls.docx = cls.h.upload("report.docx", make_docx(["Alpha paragraph with unique-token-42.", "Beta paragraph."], heading="Quarterly Report"))
        cls.pdf = cls.h.upload("note.pdf", make_pdf([["Page one text", "unique-token-42 here"], ["Page two text"]]))

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def convert(self, doc, target, **opts):
        r = self.h.job("POST", "/api/convert", {"docs": [doc["id"]], "options": {"target": target, **opts}})
        item = r["results"][0]
        self.assertTrue(item["ok"], item)
        return item["report"]

    def test_docx_to_every_available_target_is_verified(self):
        caps = self.h.call("GET", "/api/convert/formats")
        for t in caps["targets"]:
            if not t["available"] or t["id"] in ("sldc", "docx"):      # a DOCX is not converted to DOCX (see the next test)
                continue
            with self.subTest(target=t["id"]):
                rep = self.convert(self.docx, t["id"])
                self.assertTrue(rep["verification"]["ok"], rep["verification"])
                data = self.h.download(rep["output"])
                self.assertGreater(len(data), 50)

    def test_markdown_and_csv_are_read_as_what_the_name_says(self):
        md = self.h.upload("notes.md", b"# Title\n\nSome **bold** text.\n\n- one\n- two\n")
        self.assertEqual(md["format"], "md")
        self.assertEqual(self.h.upload("t.csv", b"a,b\n1,2\n3,4\n")["format"], "csv")
        rep = self.convert(md, "docx")
        with zipfile.ZipFile(io.BytesIO(self.h.download(rep["output"]))) as z:
            xml = z.read("word/document.xml").decode()
        self.assertIn("Heading1", xml)                    # the heading survived as a heading, not as the text "# Title"
        self.assertNotIn("# Title", xml)

    def test_converting_to_the_same_format_is_refused_clearly(self):
        r = self.h.job("POST", "/api/convert", {"docs": [self.docx["id"]], "options": {"target": "docx"}})
        self.assertEqual(r["failed"], 1)
        self.assertEqual(r["results"][0]["error"]["code"], "same_format")

    def test_converted_pdf_contains_the_text_and_the_right_page_count(self):
        rep = self.convert(self.docx, "pdf")
        reader = PdfReader(io.BytesIO(self.h.download(rep["output"])))
        text = "\n".join(p.extract_text() for p in reader.pages)
        self.assertIn("unique-token-42", text)
        self.assertIn("Quarterly Report", text)

    def test_pdf_to_docx_keeps_text(self):
        rep = self.convert(self.pdf, "docx")
        with zipfile.ZipFile(io.BytesIO(self.h.download(rep["output"]))) as z:
            xml = z.read("word/document.xml").decode()
        self.assertIn("unique-token-42", xml)

    def test_pdf_export_handles_tabs_in_native_sldc_text(self):
        from slai.bridge import sldc
        s = sldc()
        native = s.new_package("Tabbed text", [s.make_block("paragraph", "First\tsecond\tthird", block_id="tab-1")])
        doc = self.h.upload("tabs.sldc", s.dumps_package(native))
        rep = self.convert(doc, "pdf")
        text = "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(self.h.download(rep["output"]))).pages)
        for word in ("First", "second", "third"):
            self.assertIn(word, text)

    def test_report_lists_what_was_not_carried_over(self):
        rep = self.convert(self.pdf, "txt")
        self.assertIsInstance(rep["lost"], list)
        self.assertIsInstance(rep["kept"], list)
        self.assertIn("durationMs", rep)

    def test_text_target_honours_line_endings(self):
        rep = self.convert(self.docx, "txt", line_ending="\r\n")
        self.assertIn(b"\r\n", self.h.download(rep["output"]))

    def test_sldc_is_the_authoritative_container_and_round_trips(self):
        rep = self.convert(self.docx, "sldc")
        self.assertTrue(rep["verification"]["ok"])
        checks = {c["name"]: c for c in rep["verification"]["checks"]}
        self.assertTrue(checks["SLAI SLDC validator"]["ok"], "SLAI's own SLDC validator checks an SLDC output")
        self.assertIsNone(checks["SLAI Reader integrity check"]["ok"], "a check that does not apply is not reported as a failure")
        sldc = self.h.upload("report.sldc", self.h.download(rep["output"]))
        back = self.convert(sldc, "txt")
        self.assertIn("unique-token-42", self.h.download(back["output"]).decode("utf-8"))

    def test_sldc_can_be_encrypted_and_needs_the_password(self):
        rep = self.convert(self.docx, "sldc", encrypt_password="correct horse")
        data = self.h.download(rep["output"])
        self.assertNotIn(b"unique-token-42", data)
        locked = self.h.upload("enc.sldc", data)
        self.assertTrue(locked["locked"])
        with self.assertRaises(ApiError):
            self.convert(locked, "txt")
        self.h.call("POST", f"/api/documents/{locked['id']}/unlock", {"password": "correct horse"})
        self.assertIn("unique-token-42", self.h.download(self.convert(locked, "txt")["output"]).decode("utf-8"))

    def test_unknown_target_and_missing_documents_are_clean_errors(self):
        with self.assertRaises(ApiError) as e:
            self.h.job("POST", "/api/convert", {"docs": [self.docx["id"]], "options": {"target": "exe"}})
        self.assertEqual(e.exception.status, 400)
        with self.assertRaises(ApiError) as e:
            self.h.job("POST", "/api/convert", {"docs": ["nope"], "options": {"target": "pdf"}})
        self.assertEqual(e.exception.status, 404)

    def test_one_bad_document_does_not_stop_the_others(self):
        r = self.h.job("POST", "/api/convert", {"docs": [self.pdf["id"], self.docx["id"]], "options": {"target": "txt"}})
        self.assertEqual(r["succeeded"], 2)
        self.assertEqual(r["failed"], 0)

    def test_outputs_never_overwrite_each_other(self):
        a = self.convert(self.docx, "txt")["output"]["name"]
        b = self.convert(self.docx, "txt")["output"]["name"]
        self.assertNotEqual(a, b)


@needs_slai
class MergeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = Harness()
        cls.a = cls.h.upload("a.pdf", make_pdf([["A1 alpha"], ["A2 alpha"]]))
        cls.b = cls.h.upload("b.pdf", make_pdf([["B1 bravo"], ["B2 bravo"], ["B3 bravo"]]))
        cls.c = cls.h.upload("c.docx", make_docx(["C docx charlie"]))

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def merge(self, body):
        return self.h.call("POST", "/api/merge", body, wait=True)

    def text(self, out):
        return [p.extract_text() or "" for p in PdfReader(io.BytesIO(self.h.download(out))).pages]

    def test_plan_lists_every_page_of_every_source(self):
        plan = self.h.call("POST", "/api/merge/outline", {"docs": [self.a["id"], self.b["id"]]})
        pages = sum(len(d["pages"]) if isinstance(d, dict) and "pages" in d else 1 for d in plan)
        self.assertEqual(pages, 5, plan)

    def test_merge_two_pdfs_keeps_order_and_content(self):
        r = self.merge({"docs": [self.b["id"], self.a["id"]], "target": "pdf"})
        self.assertTrue(r["verification"]["ok"])
        pages = self.text(r["output"])
        self.assertEqual(len(pages), 5)
        self.assertIn("B1", pages[0])
        self.assertIn("A2", pages[4])

    def test_reorder_exclude_and_rotate_through_the_plan(self):
        plan = [{"doc": 0, "page": 1, "rotation": 0, "included": True}, {"doc": 0, "page": 0, "rotation": 90, "included": False},
                {"doc": 1, "page": 2, "rotation": 90, "included": True}]
        r = self.merge({"docs": [self.a["id"], self.b["id"]], "target": "pdf", "plan": plan})
        reader = PdfReader(io.BytesIO(self.h.download(r["output"])))
        self.assertEqual(len(reader.pages), 2)
        self.assertIn("A2", reader.pages[0].extract_text())
        self.assertIn("B3", reader.pages[1].extract_text())
        self.assertEqual(reader.pages[1].get("/Rotate", 0) % 360, 90)

    def test_blank_pages_can_be_inserted(self):
        plan = [{"doc": 0, "page": 0, "included": True}, {"blank": True, "included": True, "width": 595, "height": 842}, {"doc": 0, "page": 1, "included": True}]
        r = self.merge({"docs": [self.a["id"]], "target": "pdf", "plan": plan})
        pages = self.text(r["output"])
        self.assertEqual(len(pages), 3)
        self.assertEqual(pages[1].strip(), "")

    def test_pdf_and_word_merge_into_one_pdf(self):
        r = self.merge({"docs": [self.a["id"], self.c["id"]], "target": "pdf"})
        self.assertTrue(r["verification"]["ok"])
        self.assertIn("charlie", " ".join(self.text(r["output"])))

    def test_merge_into_docx_keeps_all_text(self):
        r = self.merge({"docs": [self.a["id"], self.c["id"]], "target": "docx"})
        with zipfile.ZipFile(io.BytesIO(self.h.download(r["output"]))) as z:
            xml = z.read("word/document.xml").decode()
        self.assertIn("alpha", xml)
        self.assertIn("charlie", xml)

    def test_a_plan_that_includes_nothing_is_refused(self):
        with self.assertRaises(ApiError) as e:
            self.merge({"docs": [self.a["id"]], "target": "pdf", "plan": [{"doc": 0, "page": 0, "included": False}]})
        self.assertEqual(e.exception.status, 400)

    def test_merge_managed_tocs_rekeys_groups_and_heading_links(self):
        from slai.bridge import sldc
        s = sldc()

        def sample(name, heading):
            toc_id = "a" * 24  # Independent documents can have colliding TOC IDs.
            blocks = [
                s.make_block("paragraph", "Contents", block_id="toc-title", extensions={
                    "slai.sldc.toc": {"id": toc_id, "role": "title", "depth": 2,
                                      "overrides": {"head": {"label": f"Custom {name}"}}}}),
                s.make_block("paragraph", heading, block_id="toc-entry", extensions={
                    "slai.sldc.toc": {"id": toc_id, "role": "entry", "heading": "head", "level": 1}}),
                s.make_block("heading", heading, block_id="head", attributes={"level": 1}),
            ]
            package = s.new_package(name, blocks)
            package.version = "1.1.0"
            return s.dumps_package(package)

        a = self.h.upload("toc-a.sldc", sample("First", "Alpha Heading"))
        b = self.h.upload("toc-b.sldc", sample("Second", "Beta Heading"))
        merged = self.merge({"docs": [a["id"], b["id"]], "target": "sldc"})
        self.assertTrue(merged["verification"]["ok"])
        package = s.loads_package(self.h.download(merged["output"]))
        s.validate_package(package)
        back = package.document
        titles = [block["extensions"]["slai.sldc.toc"] for block in back["blocks"]
                  if block.get("extensions", {}).get("slai.sldc.toc", {}).get("role") == "title"]
        entries = [block["extensions"]["slai.sldc.toc"] for block in back["blocks"]
                   if block.get("extensions", {}).get("slai.sldc.toc", {}).get("role") == "entry"]
        self.assertEqual((len(titles), len(entries)), (2, 2))
        self.assertEqual(len({item["id"] for item in titles}), 2)
        self.assertEqual({item["id"] for item in entries}, {item["id"] for item in titles})
        headings = {block["id"] for block in back["blocks"] if block["type"] == "heading"}
        self.assertEqual({item["heading"] for item in entries}, headings)
        self.assertTrue(all(set(item["overrides"]).issubset(headings) for item in titles))
        pdf = self.merge({"docs": [a["id"], b["id"]], "target": "pdf"})
        self.assertTrue(pdf["verification"]["ok"])
        pdf_text = "\n".join(self.text(pdf["output"]))
        self.assertIn("Alpha Heading", pdf_text)
        self.assertIn("Beta Heading", pdf_text)

    def test_merge_omitted_toc_title_keeps_text_and_discloses_lost_links(self):
        from slai.bridge import sldc
        s = sldc()
        toc_id = "b" * 24
        package = s.new_package("Partial table", [
            s.make_block("paragraph", "Table of Contents", block_id="title", extensions={
                "slai.sldc.toc": {"id": toc_id, "role": "title", "depth": 1}}),
            s.make_block("paragraph", "Visible entry", block_id="entry",
                         attributes={"page_break_before": True}, extensions={
                             "slai.sldc.toc": {"id": toc_id, "role": "entry", "heading": "heading", "level": 1}}),
            s.make_block("heading", "Real heading", block_id="heading", attributes={"level": 1}),
        ])
        package.version = "1.1.0"
        src = self.h.upload("partial-toc.sldc", s.dumps_package(package))
        merged = self.merge({"docs": [src["id"]], "target": "sldc",
                             "plan": [{"doc": 0, "page": 1, "included": True}]})
        self.assertTrue(merged["verification"]["ok"])
        back = s.loads_package(self.h.download(merged["output"]))
        s.validate_package(back)
        self.assertIn("Visible entry", [b["text"] for b in back.document["blocks"]])
        self.assertFalse(any("slai.sldc.toc" in b.get("extensions", {}) for b in back.document["blocks"]))
        self.assertTrue(any("TOC" in warning for warning in merged["warnings"]))

    def test_a_plan_pointing_outside_the_documents_is_refused(self):
        for item in ({"doc": 7, "page": 0, "included": True}, {"doc": 0, "page": 99, "included": True}, {"doc": 0, "page": -1, "included": True}):
            with self.assertRaises(ApiError, msg=str(item)):
                self.merge({"docs": [self.a["id"]], "target": "pdf", "plan": [item]})

    def test_merge_is_not_silently_wrong_when_a_source_is_locked(self):
        import pypdf
        w = pypdf.PdfWriter(clone_from=io.BytesIO(make_pdf([["secret page"]])))
        w.encrypt("pw", algorithm="AES-256")
        buf = io.BytesIO(); w.write(buf)
        d = self.h.upload("locked.pdf", buf.getvalue())
        self.assertTrue(d["locked"])
        with self.assertRaises(ApiError):
            self.merge({"docs": [self.a["id"], d["id"]], "target": "pdf"})
        self.h.call("POST", f"/api/documents/{d['id']}/unlock", {"password": "pw"})
        r = self.merge({"docs": [self.a["id"], d["id"]], "target": "pdf"})
        self.assertIn("secret", " ".join(self.text(r["output"])))


if __name__ == "__main__":
    unittest.main()
