"""The desktop shell's protocol code (src-tauri/src/protocol.rs) against the engine that speaks it (slai/engine.py).

The Rust file uses only the standard library, so plain ``rustc`` builds it here; the Tauri crate itself is NOT built by these tests.
Skipped, not faked, when no Rust compiler is installed.
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from support import APP_ROOT
from slai import engine as EN

RUSTC = shutil.which("rustc")
PROTOCOL = APP_ROOT / "src-tauri" / "src" / "protocol.rs"
CLI = APP_ROOT / "tests_py" / "fixtures" / "protocol_cli.rs"


@unittest.skipUnless(RUSTC, "rustc is not installed")
class ShellProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="dm-rust-"))
        cls.addClassCleanup(shutil.rmtree, cls.tmp, ignore_errors=True)

    def build(self, *args: str) -> None:
        done = subprocess.run([RUSTC, "--edition", "2021", *args], capture_output=True, text=True, timeout=300)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_the_rust_unit_tests_pass(self):
        binary = self.tmp / "protocol_tests"
        self.build("--test", str(PROTOCOL), "-o", str(binary))
        done = subprocess.run([str(binary)], capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertRegex(done.stdout, r"test result: ok\. \d+ passed; 0 failed")

    def parse_with_rust(self, lines: list[str]) -> list[list[str]]:
        binary = self.tmp / "protocol_cli"
        if not binary.exists():
            self.build(str(CLI), "-o", str(binary))
        done = subprocess.run([str(binary)], input="\n".join(lines) + "\n", capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        return [row.split("\t") for row in done.stdout.splitlines()]

    def test_what_python_writes_is_what_rust_reads(self):
        urls = ["http://127.0.0.1:47870/?token=Fn93xDfgH4QZy7OJt3F3WzLdbQScTvEf",
                "http://127.0.0.1:1/?token=a-b_c", "http://localhost:65535/", "http://[::1]:8766/?x=1&y=%22"]
        lines = [EN.ready_line(u, port=1, version="2.4.0", bundled=b) for u, b in zip(urls, (True, False, True, False))]
        rows = self.parse_with_rust(lines)
        self.assertEqual([r[:2] for r in rows], [["ready", u] for u in urls])
        self.assertTrue(all(r[2] == "true" for r in rows), "each is an address the shell is willing to open")

    def test_unusual_text_survives_the_trip(self):
        weird = 'http://127.0.0.1:2/?q="quote"\\back/slash \u00e9\u4e2d\U0001F600'
        row = self.parse_with_rust([EN.ready_line(weird, port=2, version="v")])[0]
        self.assertEqual(row[1], weird)
        self.assertEqual(row[2], "false", "whitespace in an address is refused by the shell even when the engine sent it")

    def test_python_errors_are_read_as_errors(self):
        messages = ["SLAI checkout not found", "Cannot listen on 127.0.0.1:1: Address already in use. Try --port.",
                    "Documaster cannot start: 3 of 495 SLAI file(s) are missing: a (missing); Reinstall SLAI Documaster."]
        rows = self.parse_with_rust([EN.error_line(m) for m in messages] + [EN.error_line("")])
        self.assertEqual([r[1] for r in rows[:3]], messages)
        self.assertEqual(rows[3][0], "error")

    def test_banner_and_log_lines_are_not_protocol(self):
        banner = ["SLAI Documaster 2.4.0 (installed app)", "  SLAI root : C:\\x", "  Open      : (given to the host)",
                  "  agents    : language: ready (LanguageAgent, 12.3 s)"]
        self.assertEqual(self.parse_with_rust(banner), [["other"]] * len(banner))

    def test_python_and_rust_agree_on_what_is_protocol(self):
        samples = ["DOCUMASTER_READY", 'DOCUMASTER_READY {"port": 1}', "DOCUMASTER_READY not json", 'DOCUMASTER_READYX {"url":"u"}',
                   "DOCUMASTER_ERRORS x", "", "plain text"]
        rust = [r[0] for r in self.parse_with_rust(samples)]
        python = [(EN.parse_line(s) or ("other",))[0] for s in samples]
        self.assertEqual(rust, python)


if __name__ == "__main__":
    unittest.main()
