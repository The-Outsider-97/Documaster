"""The launch protocol a host (the desktop shell) relies on, and the bundled layout. Real child processes, real sockets, real files."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

from support import APP_ROOT
from slai import engine as EN
from slai import startup as ST


# ------------------------------------------------------------------------------------------------------------------ pure pieces
class ProtocolLineTests(unittest.TestCase):
    def test_ready_line_round_trips(self):
        line = EN.ready_line("http://127.0.0.1:47870/?token=abc_-D", port=47870, version="2.4.0", bundled=True)
        self.assertTrue(line.startswith("DOCUMASTER_READY {"))
        kind, data = EN.parse_line(line + "\r\n")
        self.assertEqual(kind, "ready")
        self.assertEqual(data, {"url": "http://127.0.0.1:47870/?token=abc_-D", "port": 47870, "version": "2.4.0", "bundled": True})

    def test_ready_line_is_one_ascii_line_whatever_the_url_holds(self):
        line = EN.ready_line('http://x/?t="q"\\\n\u00e9\u4e2d', port=1, version="v")
        self.assertEqual(len(line.splitlines()), 1)
        line.encode("ascii")                                         # the host may read it with any code page
        self.assertEqual(EN.parse_line(line)[1]["url"], 'http://x/?t="q"\\\n\u00e9\u4e2d')

    def test_error_line_is_one_short_clean_line(self):
        line = EN.error_line("first\nsecond\tthird\x00\x1b[31m" + "x" * 5000)
        self.assertEqual(len(line.splitlines()), 1)
        self.assertNotIn("\x00", line)
        self.assertNotIn("\x1b", line)
        self.assertLessEqual(len(line), len(EN.ERROR) + 1 + EN.MAX_ERROR_CHARS)
        self.assertEqual(EN.parse_line(EN.error_line(""))[0], "error")
        self.assertIn("could not start", EN.error_line(""))

    def test_other_lines_are_not_protocol(self):
        for line in ("", "  Open      : http://127.0.0.1:1/", "DOCUMASTER_READY", "DOCUMASTER_READY not json",
                     'DOCUMASTER_READY {"port": 1}', 'DOCUMASTER_READY [1]', "DOCUMASTER_READYX {}"):
            self.assertIsNone(EN.parse_line(line), line)


class StdinWatchTests(unittest.TestCase):
    def test_close_is_noticed_and_data_is_ignored(self):
        r, w = os.pipe()
        done = threading.Event()
        with os.fdopen(r, "rb") as reader:
            EN.watch_stdin(done.set, reader)
            os.write(w, b"anything the host writes\n")
            self.assertFalse(done.wait(0.3), "data alone must not end the program")
            os.close(w)
            self.assertTrue(done.wait(5), "closing the pipe must")

    def test_an_unreadable_stream_counts_as_closed(self):
        class Broken:
            def read(self, n):
                raise OSError("handle is invalid")
        done = threading.Event()
        EN.watch_stdin(done.set, Broken())  # type: ignore[arg-type]
        self.assertTrue(done.wait(5))


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dm-bundle-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def make(self, helpers: tuple[str, ...] = ()) -> EN.Bundle:
        (self.tmp / "bundle.json").write_text(json.dumps({"documasterVersion": "2.4.0", "builtAt": "t", "python": "3.13.1",
                                                          "slai": {"revision": "abc", "models": ["m.pt"]}, "helpers": {h: {} for h in helpers}}))
        for h in helpers:
            (self.tmp / "helpers" / h / "bin").mkdir(parents=True)
        return EN.Bundle(self.tmp)

    def test_found_beside_the_app_folder_or_by_environment(self):
        self.assertIsNone(EN.find_bundle(self.tmp / "app", {}))
        bundle = self.make()
        self.assertEqual(EN.find_bundle(self.tmp / "app", {}), bundle)                         # <bundle>/app -> <bundle>
        elsewhere = Path(tempfile.mkdtemp(prefix="dm-else-"))
        self.addCleanup(shutil.rmtree, elsewhere, ignore_errors=True)
        self.assertEqual(EN.find_bundle(elsewhere / "app", {"DOCUMASTER_BUNDLE_DIR": str(self.tmp)}), bundle)
        self.assertIsNone(EN.find_bundle(elsewhere / "app", {"DOCUMASTER_BUNDLE_DIR": str(elsewhere)}))   # named, but has no bundle.json

    def test_layout_and_summary(self):
        bundle = self.make()
        self.assertEqual(bundle.slai_root, bundle.root / "slai-root")
        self.assertEqual(bundle.client_dir, bundle.root / "client")
        self.assertEqual(EN.bundle_summary(bundle), {"version": "2.4.0", "built": "t", "python": "3.13.1", "slaiRevision": "abc",
                                                     "models": ["m.pt"], "helpers": []})

    def test_damaged_manifest_is_an_empty_summary_not_a_crash(self):
        (self.tmp / "bundle.json").write_text("{ not json")
        self.assertEqual(EN.Bundle(self.tmp).manifest(), {})
        self.assertIsNone(EN.bundle_summary(EN.Bundle(self.tmp))["version"])

    def test_helper_dirs_list_each_helpers_program_folders(self):
        bundle = self.make(("tesseract", "libreoffice"))
        (self.tmp / "helpers" / "libreoffice" / "program").mkdir()
        dirs = [Path(d).relative_to(self.tmp / "helpers").as_posix() for d in bundle.helper_dirs()]
        self.assertEqual(dirs, ["libreoffice", "libreoffice/bin", "libreoffice/program", "tesseract", "tesseract/bin"])

    def test_offline_settings_override_the_users_environment_and_the_rest_only_fill_gaps(self):
        bundle = self.make()
        env = EN.bundled_environment(bundle, self.tmp / "data")
        self.assertEqual({k: env[k] for k in ("SLAI_MODEL_SOURCE", "SLAI_MODEL_OFFLINE", "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")},
                         {"SLAI_MODEL_SOURCE": "local", "SLAI_MODEL_OFFLINE": "1", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})
        self.assertEqual(env["SLAI_ROOT"], str(bundle.slai_root))
        target = {"SLAI_MODEL_SOURCE": "hub", "HF_HUB_OFFLINE": "0", "SLAI_ROOT": "C:/somebody/else/SLAI", "MPLCONFIGDIR": "/mine"}
        EN.apply_environment(env, target)
        self.assertEqual(target["SLAI_MODEL_SOURCE"], "local")
        self.assertEqual(target["HF_HUB_OFFLINE"], "1")
        self.assertEqual(target["SLAI_ROOT"], str(bundle.slai_root), "an SLAI_ROOT in the user's environment must not swap an installed app's SLAI")
        self.assertEqual(target["MPLCONFIGDIR"], "/mine", "other settings keep what the user already chose")
        self.assertEqual(target["NUMBA_CACHE_DIR"], str(self.tmp / "data" / "cache" / "numba"))

    def test_tesseract_data_is_used_when_the_bundle_has_it(self):
        bundle = self.make()
        self.assertNotIn("TESSDATA_PREFIX", EN.bundled_environment(bundle, self.tmp))
        (self.tmp / "helpers" / "tesseract" / "tessdata").mkdir(parents=True)
        self.assertEqual(EN.bundled_environment(bundle, self.tmp)["TESSDATA_PREFIX"], str(self.tmp / "helpers" / "tesseract" / "tessdata"))

    def test_helper_programs_are_found_in_the_bundle_before_the_path(self):
        from slai import bridge
        bundle = self.make(("qpdf",))
        exe = Path(bundle.helper_dirs()[-1]) / ("dm-fake-tool.exe" if os.name == "nt" else "dm-fake-tool")
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        self.addCleanup(lambda: bridge.HELPER_DIRS.clear())
        self.assertIsNone(bridge.find_binary("dm-fake-tool"))
        bridge.HELPER_DIRS[:] = bundle.helper_dirs()
        self.assertEqual(Path(bridge.find_binary("dm-fake-tool")).resolve(), exe.resolve())


class SlaiManifestCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dm-slai-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        root = self.tmp / "slai-root"
        (root / "src").mkdir(parents=True)
        (root / "src" / "a.py").write_text("print(1)\n")
        (root / "m.pt").write_bytes(b"0123456789")
        import hashlib
        entries = [{"path": "src/a.py", "bytes": 9, "kind": "code", "sha256": hashlib.sha256(b"print(1)\n").hexdigest()},
                   {"path": "m.pt", "bytes": 10, "kind": "model", "sha256": hashlib.sha256(b"0123456789").hexdigest()}]
        (root / "slai_bundle.json").write_text(json.dumps({"entries": entries, "models": [{"path": "m.pt", "bytes": 10}]}))
        (self.tmp / "bundle.json").write_text("{}")
        self.bundle = EN.Bundle(self.tmp)

    def test_complete_install_is_ok_and_names_the_model(self):
        finding = ST.check_bundle(self.bundle)
        self.assertEqual(finding.status, "ok")
        self.assertIn("m.pt", finding.detail)
        self.assertEqual(ST.check_bundle(self.bundle, deep=True).status, "ok")

    def test_missing_and_resized_files_are_blocking(self):
        (self.bundle.slai_root / "m.pt").unlink()
        finding = ST.check_bundle(self.bundle)
        self.assertEqual(finding.status, "error")
        self.assertIn("m.pt (missing)", finding.detail)
        self.assertIn("Reinstall", finding.fix)
        (self.bundle.slai_root / "m.pt").write_bytes(b"short")
        self.assertIn("m.pt (size differs)", ST.check_bundle(self.bundle).detail)

    def test_same_size_but_changed_content_is_found_only_by_the_deep_check(self):
        (self.bundle.slai_root / "m.pt").write_bytes(b"9876543210")
        self.assertEqual(ST.check_bundle(self.bundle).status, "ok")
        deep = ST.check_bundle(self.bundle, deep=True)
        self.assertEqual(deep.status, "error")
        self.assertIn("m.pt (content differs)", deep.detail)

    def test_unreadable_manifest_is_blocking(self):
        (self.bundle.slai_root / "slai_bundle.json").write_text("{")
        self.assertEqual(ST.check_bundle(self.bundle).status, "error")

    def test_read_only_slai_folder_is_a_warning_not_a_failure(self):
        self.assertEqual(ST.check_slai_writable(self.bundle.slai_root).status, "ok")
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root can write anywhere")
        os.chmod(self.bundle.slai_root, 0o555)
        self.addCleanup(os.chmod, self.bundle.slai_root, 0o755)
        self.assertEqual(ST.check_slai_writable(self.bundle.slai_root).status, "warn")


# ------------------------------------------------------------------------------------------------------------------ real processes
class Engine:
    """Documaster started the way the desktop shell starts it, with stdin held open."""

    def __init__(self, test: unittest.TestCase, *args: str, env: dict[str, str] | None = None):
        self.tmp = Path(tempfile.mkdtemp(prefix="dm-engine-"))
        test.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        slai = self.tmp / "slai"
        (slai / "src" / "utils").mkdir(parents=True)
        (slai / "src" / "utils" / "sldc.py").write_text("")          # looks like an SLAI checkout; --agents none never imports an agent
        full_env = {**os.environ, "SLAI_ROOT": str(slai), "PYTHONUNBUFFERED": "1"}
        full_env.pop("DOCUMASTER_BUNDLE_DIR", None)
        full_env.update(env or {})
        self.proc = subprocess.Popen(
            [sys.executable, str(APP_ROOT / "run_documaster.py"), "--no-browser", "--ready-line", "--exit-on-stdin-close", "--agents", "none",
             "--data-dir", str(self.tmp / "data"), "--output-dir", str(self.tmp / "out"), *args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=full_env)
        test.addCleanup(self.stop)
        self.lines: list[str] = []
        self.ready: dict | None = None
        self.error: str | None = None
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            self.lines.append(line)
            parsed = EN.parse_line(line)
            if parsed and parsed[0] == "ready":
                self.ready = parsed[1]
            elif parsed:
                self.error = parsed[1]

    def wait_for(self, what: str, timeout: float = 40.0) -> None:
        end = time.time() + timeout
        while time.time() < end and getattr(self, what) is None and self.proc.poll() is None:
            time.sleep(0.05)

    def close_stdin(self) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.close()

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(10)
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                stream and stream.close()
            except OSError:
                pass


class EngineProcessTests(unittest.TestCase):
    def test_ready_line_then_quit_when_stdin_closes(self):
        engine = Engine(self, "--port", "0")
        engine.wait_for("ready")
        self.assertIsNotNone(engine.ready, f"no READY line; output: {engine.lines}; stderr: {engine.proc.stderr.read() if engine.proc.poll() is not None else ''}")
        url = engine.ready["url"]
        self.assertEqual(engine.ready["port"], int(url.split(":")[2].split("/")[0]))
        self.assertFalse(engine.ready["bundled"])
        self.assertEqual(sum("token=" in line for line in engine.lines), 1, "the access token appears on the READY line only")
        # the URL really opens Documaster: the token link answers 302 with the session cookie, and the cookie opens the page
        import http.client
        parts = urllib.parse.urlsplit(url)
        conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
        conn.request("GET", parts.path + "?" + parts.query)
        first = conn.getresponse()
        first.read()
        self.assertEqual(first.status, 302)
        cookie = first.getheader("Set-Cookie", "").split(";")[0]
        self.assertTrue(cookie.startswith("dm_session="), cookie)
        conn.request("GET", "/", headers={"Cookie": cookie})
        page = conn.getresponse()
        self.assertEqual(page.status, 200)
        conn.close()
        engine.close_stdin()
        self.assertEqual(engine.proc.wait(15), 0, "closing stdin is how the shell asks Documaster to quit")
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", engine.ready["port"]), timeout=2)

    def test_trace_option_dumps_every_threads_stack_to_stderr(self):
        """DOCUMASTER_TRACE_AFTER is how a hang in a real bundle is located (smoke_engine.py --trace-after)."""
        engine = Engine(self, "--port", "0", env={"DOCUMASTER_TRACE_AFTER": "1"})
        engine.wait_for("ready")
        self.assertIsNotNone(engine.ready, engine.lines)
        time.sleep(2.5)
        engine.close_stdin()
        self.assertEqual(engine.proc.wait(15), 0)
        err = engine.proc.stderr.read().decode("utf-8", "replace")
        self.assertIn("most recent call first", err)
        self.assertIn("serve_forever", err, "the server thread's stack is in the dump")

    def test_taken_port_falls_back_only_when_asked(self):
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen(1)
            port = busy.getsockname()[1]
            engine = Engine(self, "--port", str(port), "--port-fallback")
            engine.wait_for("ready")
            self.assertIsNotNone(engine.ready, engine.lines)
            self.assertNotEqual(engine.ready["port"], port)
            self.assertTrue(engine.ready["url"].startswith(f"http://127.0.0.1:{engine.ready['port']}/"))
            engine.close_stdin()
            self.assertEqual(engine.proc.wait(15), 0)
            strict = Engine(self, "--port", str(port))
            strict.wait_for("error")
            self.assertEqual(strict.proc.wait(15), 2)
            self.assertIsNone(strict.ready)
            self.assertIn("Cannot listen", strict.error or "")

    def test_a_start_that_cannot_happen_ends_with_one_error_line(self):
        engine = Engine(self, "--port", "0", env={"SLAI_ROOT": str(Path(tempfile.gettempdir()) / "definitely-not-slai")})
        engine.wait_for("error")
        self.assertEqual(engine.proc.wait(30), 1)
        self.assertIsNone(engine.ready)
        self.assertIn("SLAI", engine.error or "")
        self.assertEqual([l for l in engine.lines if l.startswith("DOCUMASTER_")], [f"DOCUMASTER_ERROR {engine.error}"])

    def test_without_the_flags_nothing_changes(self):
        """A person at a terminal: human banner with the link, no protocol lines, no quitting on stdin."""
        tmp = Path(tempfile.mkdtemp(prefix="dm-plain-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "slai" / "src" / "utils").mkdir(parents=True)
        (tmp / "slai" / "src" / "utils" / "sldc.py").write_text("")
        proc = subprocess.Popen([sys.executable, str(APP_ROOT / "run_documaster.py"), "--no-browser", "--agents", "none", "--port", "0",
                                 "--data-dir", str(tmp / "d"), "--output-dir", str(tmp / "o")],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env={**os.environ, "SLAI_ROOT": str(tmp / "slai"), "PYTHONUNBUFFERED": "1"})
        self.addCleanup(lambda: (proc.kill(), proc.wait(10), proc.stdout.close(), proc.stderr.close()))
        out = []
        end = time.time() + 40
        while time.time() < end:
            line = proc.stdout.readline().decode()
            out.append(line)
            if "Open" in line:
                break
        text = "".join(out)
        self.assertIn("?token=", text)
        self.assertNotIn("DOCUMASTER_", text)
        time.sleep(1.0)
        self.assertIsNone(proc.poll(), "stdin being closed must not stop a Documaster started without --exit-on-stdin-close")


@unittest.skipUnless((APP_ROOT / "dist" / "index.html").is_file(), "the web client is not built")
class BundledModeProcessTests(unittest.TestCase):
    def test_bundle_is_detected_and_supplies_slai_and_client(self):
        tmp = Path(tempfile.mkdtemp(prefix="dm-bundled-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        root = tmp / "slai-root"
        (root / "src" / "utils").mkdir(parents=True)
        (root / "src" / "utils" / "sldc.py").write_text("")
        listing = [{"path": "src/utils/sldc.py", "bytes": 0, "kind": "code"}]
        (root / "slai_bundle.json").write_text(json.dumps({"entries": listing, "models": []}))
        (tmp / "bundle.json").write_text(json.dumps({"documasterVersion": "x"}))
        shutil.copytree(APP_ROOT / "dist", tmp / "client")
        engine = Engine(self, "--port", "0", env={"DOCUMASTER_BUNDLE_DIR": str(tmp), "SLAI_ROOT": "C:/not/the/bundles/slai",
                                                  "SLAI_MODEL_SOURCE": "hub"})
        engine.wait_for("ready")
        self.assertIsNotNone(engine.ready, engine.lines)
        self.assertTrue(engine.ready["bundled"])
        self.assertTrue(any(str(root) in line for line in engine.lines), "the bundle's SLAI, not the one from the environment: " + str(engine.lines))
        with urllib.request.urlopen(engine.ready["url"], timeout=10) as resp:
            self.assertIn(b"<html", resp.read(2000).lower(), "the client is served from the bundle")
        token = engine.ready["url"].split("token=")[1]
        health = urllib.request.Request(engine.ready["url"].split("/?")[0] + "/api/health", headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(health, timeout=10) as resp:
            data = json.loads(resp.read())["data"]
        self.assertTrue(data["bundled"])
        self.assertEqual(data["bundle"]["version"], "x", "the System page can show what the installed app was built from")
        engine.close_stdin()
        self.assertEqual(engine.proc.wait(15), 0)

    def test_a_damaged_bundle_does_not_start_and_says_why(self):
        tmp = Path(tempfile.mkdtemp(prefix="dm-damaged-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        root = tmp / "slai-root"
        (root / "src" / "utils").mkdir(parents=True)
        (root / "src" / "utils" / "sldc.py").write_text("")
        (root / "slai_bundle.json").write_text(json.dumps({"entries": [{"path": "models/m.pt", "bytes": 5}], "models": []}))
        (tmp / "bundle.json").write_text("{}")
        shutil.copytree(APP_ROOT / "dist", tmp / "client")
        engine = Engine(self, "--port", "0", env={"DOCUMASTER_BUNDLE_DIR": str(tmp)})
        engine.wait_for("error")
        self.assertEqual(engine.proc.wait(30), 1)
        self.assertIn("models/m.pt (missing)", engine.error or "")
        self.assertIn("Reinstall", engine.error or "")


if __name__ == "__main__":
    unittest.main()
