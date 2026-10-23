"""PDF editing operations: each one is applied through the API, then the resulting file is opened and inspected."""
import io
import unittest

import pypdf

from support import ApiError, Harness, make_pdf, needs_slai


@needs_slai
class PdfOpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = Harness()

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def setUp(self):
        self.d = self.h.upload("o.pdf", make_pdf([["First page"], ["Second page"], ["Third page"]], title="Orig", author="Me"))

    def op(self, spec, doc=None):
        return self.h.call("POST", f"/api/documents/{(doc or self.d)['id']}/op", spec, wait=True)

    def reader(self, doc=None, password=None):
        status, _, raw = self.h.raw("GET", f"/api/documents/{(doc or self.d)['id']}/file")
        self.assertEqual(status, 200)
        r = pypdf.PdfReader(io.BytesIO(raw))
        if r.is_encrypted:
            r.decrypt(password or "")
        return r

    def info(self, doc=None):
        want = (doc or self.d)["id"]
        return next(d for d in self.h.call("GET", "/api/documents") if d["id"] == want)

    # ---------------------------------------------------------------- pages
    def test_rotate_selected_pages_only(self):
        r = self.op({"op": "rotate", "pages": "2", "degrees": 90})
        self.assertTrue(r["changed"])
        pages = self.reader().pages
        self.assertEqual([p.get("/Rotate", 0) % 360 for p in pages], [0, 90, 0])

    def test_rotation_adds_up(self):
        self.op({"op": "rotate", "pages": "1", "degrees": 90})
        self.op({"op": "rotate", "pages": "1", "degrees": 90})
        self.assertEqual(self.reader().pages[0].get("/Rotate", 0) % 360, 180)

    def test_delete_and_undo_and_redo(self):
        self.op({"op": "delete", "pages": "2"})
        self.assertEqual(len(self.reader().pages), 2)
        self.assertEqual(self.info()["revision"]["canUndo"], True)
        self.h.call("POST", f"/api/documents/{self.d['id']}/undo")
        self.assertEqual(len(self.reader().pages), 3)
        self.h.call("POST", f"/api/documents/{self.d['id']}/redo")
        self.assertEqual(len(self.reader().pages), 2)

    def test_deleting_every_page_is_refused(self):
        with self.assertRaises(ApiError):
            self.op({"op": "delete", "pages": "1-3"})
        self.assertEqual(len(self.reader().pages), 3)

    def test_arrange_reorders_duplicates_and_adds_blank_pages(self):
        plan = [{"page": 2, "rotation": 0, "included": True}, {"page": 0, "rotation": 0, "included": True}, {"page": 0, "rotation": 0, "included": True},
                {"blank": True, "included": True, "width": 595, "height": 842}]
        self.op({"op": "arrange", "plan": plan})
        texts = [p.extract_text().strip() for p in self.reader().pages]
        self.assertEqual(texts, ["Third page", "First page", "First page", ""])

    def test_export_selected_pages_writes_a_new_file_and_leaves_the_document_alone(self):
        r = self.h.job("POST", f"/api/documents/{self.d['id']}/export", {"pages": "1,3"})
        out = pypdf.PdfReader(io.BytesIO(self.h.download(r["outputs"][0])))
        self.assertEqual([p.extract_text().strip() for p in out.pages], ["First page", "Third page"])
        self.assertEqual(len(self.reader().pages), 3)

    def test_split_each_page_into_its_own_file(self):
        r = self.h.job("POST", f"/api/documents/{self.d['id']}/export", {"pages": "all", "splitEach": True})
        self.assertEqual(len(r["outputs"]), 3)

    def test_insert_pages_from_another_pdf(self):
        other = self.h.upload("other.pdf", make_pdf([["Inserted A"], ["Inserted B"]]))
        self.op({"op": "insert", "from": other["id"], "after": 1})
        texts = [p.extract_text().strip() for p in self.reader().pages]
        self.assertEqual(texts, ["First page", "Inserted A", "Inserted B", "Second page", "Third page"])

    def test_crop_changes_the_visible_area(self):
        before = self.reader().pages[0].cropbox
        self.op({"op": "crop", "rect": [0.1, 0.1, 0.6, 0.6], "pages": [1]})
        after = self.reader().pages[0].cropbox
        self.assertLess(float(after.width), float(before.width))

    # ---------------------------------------------------------------- content
    def test_watermark_and_page_numbers_become_page_content(self):
        self.op({"op": "stamp", "kind": "watermark", "text": "CONFIDENTIAL-X", "pages": "all"})
        self.op({"op": "stamp", "kind": "page_numbers", "format": "Page {n} of {N}", "position": "bottom-center", "pages": "all"})
        text = self.reader().pages[1].extract_text()
        self.assertIn("CONFIDENTIAL-X", text)
        self.assertIn("Page 2 of 3", text)

    def test_stamp_only_the_pages_asked_for(self):
        self.op({"op": "stamp", "kind": "watermark", "text": "ODDONLY", "pages": "odd"})
        t = [p.extract_text() for p in self.reader().pages]
        self.assertIn("ODDONLY", t[0]); self.assertNotIn("ODDONLY", t[1]); self.assertIn("ODDONLY", t[2])

    def test_annotations_are_standard_pdf_annotations_and_removable(self):
        self.op({"op": "annotate", "annotations": [
            {"type": "highlight", "page": 1, "rects": [[0.1, 0.1, 0.4, 0.14]], "color": "#ffee00"},
            {"type": "note", "page": 1, "rect": [0.5, 0.1, 0.55, 0.14], "text": "Check this"},
            {"type": "rect", "page": 2, "rect": [0.2, 0.2, 0.5, 0.4]}]})
        types = [str(a.get_object()["/Subtype"]) for p in self.reader().pages for a in (p.get("/Annots") or [])]
        self.assertEqual(sorted(types), ["/Highlight", "/Square", "/Text"])
        listed = self.h.call("GET", f"/api/documents/{self.d['id']}/pdfinfo")["annotations"]
        self.assertEqual(len(listed), 3)
        self.op({"op": "remove_annotations", "selectors": [{"page": 0, "index": 0}]})
        self.assertEqual(len(self.h.call("GET", f"/api/documents/{self.d['id']}/pdfinfo")["annotations"]), 2)

    def test_every_annotation_kind_is_written_with_an_appearance_so_viewers_draw_it(self):
        specs = [{"type": "underline", "page": 1, "rects": [[0.1, 0.1, 0.4, 0.13]]}, {"type": "strikeout", "page": 1, "rects": [[0.1, 0.2, 0.4, 0.23]]},
                 {"type": "ellipse", "page": 2, "rect": [0.2, 0.2, 0.5, 0.4]},
                 {"type": "ink", "page": 3, "strokes": [[[0.1, 0.1], [0.2, 0.15], [0.3, 0.1]]]}]
        self.op({"op": "annotate", "annotations": specs})
        found = {}
        for p in self.reader().pages:
            for a in p.get("/Annots") or []:
                o = a.get_object()
                found[str(o["/Subtype"])] = "/AP" in o
        self.assertEqual(found, {"/Underline": True, "/StrikeOut": True, "/Circle": True, "/Ink": True})

    def test_text_at_a_point_text_boxes_and_images_become_page_content(self):
        self.op({"op": "stamp", "kind": "text", "text": "APPROVED-X", "size": 24, "point": [0.3, 0.3], "pages": [1]})
        self.op({"op": "stamp", "kind": "textbox", "rect": [0.1, 0.5, 0.6, 0.6], "text": "BOXED-TEXT", "size": 14, "pages": [2]})
        texts = [p.extract_text() for p in self.reader().pages]
        self.assertIn("APPROVED-X", texts[0])
        self.assertIn("BOXED-TEXT", texts[1])
        from PIL import Image
        buf = io.BytesIO(); Image.new("RGB", (40, 40), (200, 30, 30)).save(buf, "PNG")
        status, _, raw = self.h.raw("POST", "/api/assets?name=logo.png", buf.getvalue(), {"Content-Type": "application/octet-stream"})
        import json
        asset = json.loads(raw)["data"]
        self.op({"op": "stamp", "kind": "image", "asset": asset["id"], "rect": [0.1, 0.1, 0.3, 0.2], "pages": [3]})
        images = list(self.reader().pages[2].images)
        self.assertEqual(len(images), 1)

    def test_annotation_on_a_missing_page_is_refused(self):
        with self.assertRaises(ApiError):
            self.op({"op": "annotate", "annotations": [{"type": "highlight", "page": 9, "rects": [[0.1, 0.1, 0.2, 0.2]]}]})

    def test_metadata_and_bookmarks(self):
        self.op({"op": "metadata", "values": {"title": "New title", "author": "Someone"}})
        self.assertEqual(self.reader().metadata.title, "New title")
        self.op({"op": "bookmarks", "entries": [{"title": "Intro", "page": 0, "children": [{"title": "Sub", "page": 1, "children": []}]}]})
        outline = self.reader().outline
        self.assertEqual(outline[0].title, "Intro")
        self.assertEqual(outline[1][0].title, "Sub")

    # ---------------------------------------------------------------- security
    def test_password_protect_then_open_with_it(self):
        self.op({"op": "password", "new": "s3cret-pass"})
        info = self.info()
        self.assertTrue(info["encrypted"])
        status, _, raw = self.h.raw("GET", f"/api/documents/{self.d['id']}/file")
        r = pypdf.PdfReader(io.BytesIO(raw))
        self.assertTrue(r.is_encrypted)
        self.assertEqual(r.decrypt("wrong"), 0)
        self.assertNotEqual(r.decrypt("s3cret-pass"), 0)
        self.assertIn("First page", r.pages[0].extract_text())

    def test_remove_password(self):
        self.op({"op": "password", "new": "s3cret-pass"})
        self.op({"op": "password", "new": ""})
        self.assertFalse(self.info()["encrypted"])

    def test_weak_password_is_refused(self):
        with self.assertRaises(ApiError):
            self.op({"op": "password", "new": "ab"})

    def test_a_locked_pdf_stays_locked_until_the_right_password(self):
        w = pypdf.PdfWriter(clone_from=io.BytesIO(make_pdf([["locked text"]])))
        w.encrypt("pw-1234", algorithm="AES-256")
        buf = io.BytesIO(); w.write(buf)
        d = self.h.upload("lock.pdf", buf.getvalue())
        self.assertTrue(d["locked"])
        with self.assertRaises(ApiError):
            self.op({"op": "rotate", "pages": "1", "degrees": 90}, d)
        with self.assertRaises(ApiError):
            self.h.call("POST", f"/api/documents/{d['id']}/unlock", {"password": "nope"})
        self.h.call("POST", f"/api/documents/{d['id']}/unlock", {"password": "pw-1234"})
        self.op({"op": "rotate", "pages": "1", "degrees": 90}, d)
        self.assertEqual(self.reader(d, "pw-1234").pages[0].get("/Rotate", 0) % 360, 90)

    def test_documents_are_not_re_signed(self):
        """There is no operation that signs, and flattening/editing never claims a signature is still valid."""
        caps = self.h.call("GET", "/api/capabilities")
        self.assertNotIn("sign", str(caps.get("pdfOps", "")).lower())
        with self.assertRaises(ApiError):
            self.op({"op": "sign"})

    def test_unknown_operation_is_a_clean_error(self):
        with self.assertRaises(ApiError) as e:
            self.op({"op": "does_not_exist"})
        self.assertEqual(e.exception.status, 400)

    def test_every_change_is_verified_and_recorded_as_a_revision(self):
        r = self.op({"op": "rotate", "pages": "1", "degrees": 90})
        self.assertTrue(r["verification"]["ok"])
        self.assertEqual(self.info()["revision"]["count"], 2)

    def test_a_failed_check_leaves_the_document_unchanged(self):
        before = self.reader().pages[0].extract_text()
        with self.assertRaises(ApiError):
            self.op({"op": "rotate", "pages": "99", "degrees": 90})
        self.assertEqual(self.reader().pages[0].extract_text(), before)
        self.assertEqual(self.info()["revision"]["count"], 1)

    def test_revision_history_can_be_jumped_to(self):
        self.op({"op": "rotate", "pages": "1", "degrees": 90})
        self.op({"op": "delete", "pages": "3"})
        self.h.call("POST", f"/api/documents/{self.d['id']}/revision/0")
        self.assertEqual(len(self.reader().pages), 3)
        self.assertEqual(self.reader().pages[0].get("/Rotate", 0) % 360, 0)


if __name__ == "__main__":
    unittest.main()


@needs_slai
class ConcurrentRenderTests(unittest.TestCase):
    def test_many_pages_can_be_drawn_at_once_without_crashing_pdfium(self):
        """The viewer asks for a dozen thumbnails at the same time; pdfium is not thread-safe, so Documaster serialises it."""
        import threading
        h = Harness()
        self.addCleanup(h.close)
        doc = h.upload("many.pdf", make_pdf([[f"Page {i}", "text " * 40] for i in range(12)]))
        results, errors = [], []

        def fetch(i):
            try:
                status, _, raw = h.raw("GET", f"/api/documents/{doc['id']}/pages/{i % 12}/image?w=200")
                results.append((status, raw[:4]))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=fetch, args=(i,)) for i in range(48)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 48)
        self.assertTrue(all(s == 200 and r == b"\x89PNG" for s, r in results))
