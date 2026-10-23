"""Pure-Python units: counting, language, personal-data patterns, file naming, jobs, request understanding."""
import threading
import time
import unittest
from pathlib import Path

from support import APP_ROOT  # noqa: F401

from slai import counter, textstats
from slai.analysis import pii
from slai.assistant import nlu
from slai.errors import Cancelled
from slai.files import replace_file, reserve_path, safe_filename, write_atomic
from slai.jobs import JobManager


class CountingTests(unittest.TestCase):
    def test_basic_counts(self):
        s = textstats.basic_counts("Hello world. This is a test!\n\nSecond paragraph here.")
        self.assertEqual(s["words"], 9)
        self.assertEqual(s["sentences"], 3)
        self.assertEqual(s["paragraphs"], 2)
        self.assertEqual(s["characters"], len("Hello world. This is a test!\n\nSecond paragraph here."))

    def test_empty_text(self):
        s = textstats.basic_counts("")
        self.assertEqual((s["words"], s["sentences"], s["paragraphs"], s["lines"]), (0, 0, 0, 0))
        self.assertEqual(s["readingMinutes"], 0.0)

    def test_apostrophes_hyphens_numbers_and_unicode(self):
        self.assertEqual(textstats.basic_counts("don't stop well-known 3.14 café")["words"], 5)

    def test_cjk_is_not_counted_as_one_word(self):
        s = textstats.basic_counts("今日は天気がいいですね")
        self.assertGreater(s["words"], 1)
        self.assertGreater(s["cjkCharacters"], 5)

    def test_abbreviations_do_not_end_sentences(self):
        self.assertEqual(textstats.count_sentences("Dr. Smith went to Washington. He arrived at 3.30 p.m. on Monday."), 2)

    def test_language_guess_is_labelled_with_confidence(self):
        en = textstats.detect_language("The quick brown fox jumps over the lazy dog and the cat was on the mat with all of them.")
        nl = textstats.detect_language("Het is een mooie dag en we gaan naar de markt om groenten te kopen voor het eten van vanavond.")
        self.assertEqual((en["code"], nl["code"]), ("en", "nl"))
        self.assertEqual(textstats.detect_language("")["code"], "und")

    def test_goals_report_progress_without_inventing(self):
        r = counter.count_text("one two three four five", goals={"minWords": 10, "maxWords": 20})
        self.assertEqual([(g["goal"], g["value"], g["met"]) for g in r["goals"]], [("minWords", 5, False), ("maxWords", 5, True)])
        with self.assertRaises(Exception):
            counter.count_text("x", goals={"words": 1})

    def test_huge_text_is_refused_not_attempted(self):
        from slai.errors import ValidationFailed
        with self.assertRaises(ValidationFailed):
            counter.count_text("x" * 50_000_001)


class PiiTests(unittest.TestCase):
    def kinds(self, text):
        return [(i["type"], i["text"]) for i in pii.scan_text(text)]

    def test_valid_and_invalid_iban(self):
        self.assertIn(("iban", "NL91 ABNA 0417 1643 00"), self.kinds("Pay NL91 ABNA 0417 1643 00 today"))
        self.assertEqual([k for k in self.kinds("Pay NL91 ABNA 0417 1643 01 today") if k[0] == "iban"], [])

    def test_card_numbers_need_a_valid_luhn_checksum(self):
        self.assertIn("card", [k for k, _ in self.kinds("Card 4111 1111 1111 1111 expires")])
        self.assertNotIn("card", [k for k, _ in self.kinds("Order 4111 1111 1111 1112 shipped")])

    def test_emails_including_plus_and_subdomains(self):
        found = [t for k, t in self.kinds("a.b+tag@mail.example.co.uk and x@y.io.") if k == "email"]
        self.assertEqual(found, ["a.b+tag@mail.example.co.uk", "x@y.io"])

    def test_phone_numbers(self):
        for text in ("+31 6 12345678", "+44 20 7946 0958", "(555) 123-4567", "06-12345678"):
            self.assertIn("phone", [k for k, _ in self.kinds(f"Call {text} now")], text)

    def test_plain_numbers_and_dates_are_not_phone_numbers(self):
        for text in ("In 2024 we sold 1,250,000 units", "Version 3.2.1 released 2024-05-17", "Total: 12345.67 EUR", "Pages 123-456"):
            self.assertNotIn("phone", [k for k, _ in self.kinds(text)], text)

    def test_bsn_uses_the_eleven_test_and_is_marked_weak(self):
        self.assertIn("bsn", [k for k, _ in self.kinds("BSN 111222333")])
        self.assertNotIn("bsn", [k for k, _ in self.kinds("number 123456789")])

    def test_overlapping_matches_are_reported_once(self):
        kinds = self.kinds("NL91 ABNA 0417 1643 00")
        self.assertEqual(len(kinds), 1)

    def test_ipv4(self):
        self.assertIn("ipv4", [k for k, _ in self.kinds("host 192.168.1.20 up")])
        self.assertNotIn("ipv4", [k for k, _ in self.kinds("version 999.1.1.1")])


class FileNameTests(unittest.TestCase):
    def test_names_are_made_safe(self):
        for raw in ("../../etc/passwd", "a/b\\c.pdf", "con.pdf", "..", "", "x" * 400 + ".pdf", "report\x00.pdf", "  spaced name .pdf ", "naïve résumé.pdf"):
            safe = safe_filename(raw)
            self.assertNotIn("/", safe); self.assertNotIn("\\", safe); self.assertNotIn("\x00", safe)
            self.assertNotEqual(safe, "..")
            self.assertTrue(safe)
            self.assertLessEqual(len(safe), 120)

    def test_outputs_are_numbered_not_overwritten(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            a = write_atomic(Path(d), "doc", ".txt", b"one")
            b = write_atomic(Path(d), "doc", ".txt", b"two")
            self.assertNotEqual(a, b)
            self.assertEqual(a.read_bytes(), b"one")
            self.assertEqual(b.read_bytes(), b"two")

    def test_a_failed_write_leaves_no_partial_file(self):
        import tempfile
        from slai.files import atomic_output
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(RuntimeError):
                with atomic_output(Path(d), "x", ".bin") as out:
                    Path(out.temp).write_bytes(b"half")
                    raise RuntimeError("boom")
            self.assertEqual(list(Path(d).iterdir()), [])


class ReplaceFileTests(unittest.TestCase):
    def test_falls_back_to_copy_when_replace_is_refused(self):
        import os, tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            src, dst = Path(d, "a.bin"), Path(d, "a.pdf")
            src.write_bytes(b"content")
            with mock.patch("slai.files.os.replace", side_effect=OSError(17, "different drive")):
                replace_file(src, dst)
            self.assertEqual(dst.read_bytes(), b"content")
            self.assertFalse(src.exists())

    def test_a_failing_flush_does_not_fail_the_copy(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            src, dst = Path(d, "a.bin"), Path(d, "a.pdf")
            src.write_bytes(b"content")
            with mock.patch("slai.files.os.replace", side_effect=OSError(17, "different drive")), \
                    mock.patch("slai.files.os.fsync", side_effect=OSError(9, "Bad file descriptor")):
                replace_file(src, dst)
            self.assertEqual(dst.read_bytes(), b"content")

    def test_reports_both_errors_when_nothing_works(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            src = Path(d, "missing.bin")
            with mock.patch("slai.files.os.replace", side_effect=OSError(17, "different drive")):
                with self.assertRaises(OSError):
                    replace_file(src, Path(d, "x.pdf"))


class JobTests(unittest.TestCase):
    def test_a_job_reports_progress_and_result(self):
        m = JobManager(30)
        j = m.submit("t", "t", lambda c: (c.report(0.5, "half"), 42)[1])
        for _ in range(100):
            if j.state == "done":
                break
            time.sleep(0.02)
        self.assertEqual((j.state, j.result, j.progress), ("done", 42, 1.0))
        m.shutdown()

    def test_a_running_job_can_be_cancelled(self):
        m = JobManager(30)
        started = threading.Event()

        def work(c):
            started.set()
            while True:
                c.check()
                time.sleep(0.01)

        j = m.submit("t", "t", work)
        started.wait(2)
        m.cancel(j.id)
        for _ in range(100):
            if j.state == "cancelled":
                break
            time.sleep(0.02)
        self.assertEqual(j.state, "cancelled")
        m.shutdown()

    def test_a_job_that_overruns_its_deadline_fails_cleanly(self):
        m = JobManager(1)

        def work(c):
            while True:
                c.check()
                time.sleep(0.01)

        j = m.submit("t", "t", work)
        for _ in range(300):
            if j.state in ("failed", "cancelled"):
                break
            time.sleep(0.02)
        self.assertEqual(j.state, "failed")
        self.assertIn(j.error["code"], ("timeout", "timed_out"))
        m.shutdown()

    def test_an_unexpected_exception_becomes_a_failed_job(self):
        m = JobManager(30)
        j = m.submit("t", "t", lambda c: 1 / 0)
        for _ in range(100):
            if j.state == "failed":
                break
            time.sleep(0.02)
        self.assertEqual(j.state, "failed")
        self.assertEqual(j.error["code"], "internal_error")
        m.shutdown()


class UnderstandingTests(unittest.TestCase):
    NAMES = ["report.pdf", "notes.docx", "pii.pdf"]

    def tool(self, text):
        return nlu.parse(text, self.NAMES).tool

    def test_english_and_dutch(self):
        cases = {"count the words in report.pdf": "count_words", "tel de woorden": "count_words", "merge report.pdf and notes.docx": "merge",
                 "voeg deze samen": "merge", "convert notes.docx to pdf": "convert", "rotate page 2 by 90 degrees": "rotate",
                 "compress this pdf": "compress", "redact the iban numbers": "redact_pii", "does report.pdf have a summary section?": "check_sections",
                 "add a watermark \"DRAFT\"": "watermark", "find personal data": "find_pii", "vergelijk report.pdf met notes.docx": "compare"}
        for text, tool in cases.items():
            self.assertEqual(self.tool(text), tool, text)

    def test_file_names_do_not_trigger_rules(self):
        self.assertEqual(self.tool("summarise pii.pdf"), "summarize")
        self.assertEqual(self.tool("how many words are in pii.pdf?"), "count_words")

    def test_arguments(self):
        i = nlu.parse("convert notes.docx to markdown", self.NAMES)
        self.assertEqual(i.args["target"], "md")
        self.assertEqual(nlu.parse("rotate report.pdf 90 degrees counterclockwise", self.NAMES).args["degrees"], 270)
        self.assertEqual(nlu.parse("delete pages 2-3 of report.pdf", self.NAMES).args["pages"], "2-3")
        self.assertEqual(nlu.parse('watermark "SECRET"', self.NAMES).args["text"], "SECRET")
        self.assertEqual(nlu.parse("redact email addresses and phone numbers", self.NAMES).args["types"], ["email", "phone"])

    def test_unknown_requests_are_not_guessed(self):
        self.assertIsNone(self.tool("blorp wibble"))
        self.assertIsNone(self.tool("please do the thing"))

    def test_a_name_that_is_a_prefix_of_another_is_not_mixed_up(self):
        i = nlu.parse("summarise report.pdf", ["report.pdf", "report.pdf.bak", "a.pdf"])
        self.assertEqual(i.doc_mentions, ["report.pdf"])


if __name__ == "__main__":
    unittest.main()
