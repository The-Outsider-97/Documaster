"""Start-up checks: findings, the content-hash build record and the --check / --build / --dev modes. No SLAI checkout needed."""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from support import APP_ROOT  # noqa: F401  (puts the app on sys.path)
from slai import startup as ST


def _app(root: Path, *, built: bool = True) -> tuple[Path, Path]:
    """A miniature Documaster folder: one TypeScript source and (optionally) a build of it with a record."""
    app, dist = root / "app", root / "app" / "dist"
    (app / "src" / "client").mkdir(parents=True)
    (app / "src" / "client" / "main.ts").write_text("export const x = 1;\n")
    (app / "tsconfig.json").write_text("{}\n")
    if built:
        (dist / "js" / "client").mkdir(parents=True)
        (dist / "startup").mkdir()
        (dist / "index.html").write_text("<html></html>")
        (dist / "js" / "client" / "main.js").write_text("export const x = 1;\n")
        (dist / "startup" / "loader.js").write_text("")
        (dist / ST.BUILD_RECORD).write_text(json.dumps({"format": 1, "files": ST.client_inputs(app)}))
    return app, dist


class FindingTests(unittest.TestCase):
    def test_python_too_old_blocks_and_names_the_fix(self):
        f = ST.check_python(None, (3, 9, 1))
        self.assertEqual(f.status, "error")
        self.assertIn("3.10", f.fix)

    def test_python_range_declared_by_slai(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "pyproject.toml").write_text('[project]\nrequires-python = ">=3.10,<3.13"\n')
            self.assertEqual(ST.check_python(root, (3, 11, 4)).status, "ok")
            self.assertEqual(ST.check_python(root, (3, 13, 0)).status, "warn")      # newer than qualified: a warning, not a stop
            self.assertEqual(ST.check_python(root, (3, 9, 0)).status, "error")

    def test_dependency_states(self):
        have = lambda name: "5.9.0"          # noqa: E731
        self.assertEqual(ST.check_dependency("pypdf", "pypdf", "5.9", "x", required=True, installed=have, importable=lambda m: True).status, "ok")
        old = ST.check_dependency("pypdf", "pypdf", "6.0", "x", required=True, installed=have, importable=lambda m: True)
        self.assertEqual((old.status, "upgrade" in old.fix), ("error", True))
        gone = ST.check_dependency("regex", "regex", "1", "x", required=False, installed=lambda n: None)
        self.assertEqual(gone.status, "warn")                                        # optional: start-up goes on
        self.assertEqual(ST.check_dependency("pypdf", "pypdf", "1", "x", required=True, installed=lambda n: None).status, "error")
        # installed according to the metadata but not importable is still missing
        self.assertEqual(ST.check_dependency("pypdf", "pypdf", "1", "x", required=True, installed=have, importable=lambda m: False).status, "error")

    def test_program_lookup(self):
        self.assertEqual(ST.check_program("Tesseract OCR", ("tesseract",), "OCR", find=lambda *a, **k: "/bin/tesseract").status, "ok")
        missing = ST.check_program("Tesseract OCR", ("tesseract",), "OCR", find=lambda *a, **k: None)
        self.assertEqual(missing.status, "warn")
        self.assertIn("OCR", missing.detail)

    def test_working_folder_is_really_written(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(ST.check_data_dir(Path(t) / "new" / "dir").status, "ok")
            blocker = Path(t) / "file"
            blocker.write_text("x")
            self.assertEqual(ST.check_data_dir(blocker / "sub").status, "error")     # a path below a file cannot be created

    def test_render_summary(self):
        text = ST.render([ST.Finding("a", "ok", "fine"), ST.Finding("b", "warn", "meh", "do x"), ST.Finding("c", "error", "bad", "do y")])
        self.assertIn("Cannot start: 1 blocking problem(s), 1 warning(s).", text)
        self.assertIn("-> do y", text)
        self.assertNotIn("->  fine", text)
        self.assertTrue(ST.render([ST.Finding("a", "ok", "fine")]).endswith("Ready: 0 blocking problem(s), 0 warning(s)."))


class ClientBuildRecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_current_build_is_ok(self):
        app, dist = _app(self.root)
        self.assertEqual(ST.check_client(app, dist).status, "ok")

    def test_not_built_is_an_error(self):
        app, dist = _app(self.root, built=False)
        f = ST.check_client(app, dist)
        self.assertEqual(f.status, "error")
        self.assertIn("npm run build", f.fix)

    def test_changed_source_is_reported_by_content(self):
        app, dist = _app(self.root)
        (app / "src" / "client" / "main.ts").write_text("export const x = 2;\n")
        f = ST.check_client(app, dist)
        self.assertEqual(f.status, "warn")
        self.assertIn("src/client/main.ts", f.detail)

    def test_same_content_with_other_times_or_line_endings_is_current(self):
        app, dist = _app(self.root)
        source = app / "src" / "client" / "main.ts"
        source.write_bytes(b"export const x = 1;\r\n")          # a Windows checkout of the same text
        self.assertEqual(ST.check_client(app, dist).status, "ok")
        source.touch()                                          # file times never decide
        self.assertEqual(ST.check_client(app, dist).status, "ok")

    def test_added_and_removed_sources_are_changes(self):
        app, dist = _app(self.root)
        (app / "src" / "client" / "new.ts").write_text("export {};\n")
        self.assertEqual(ST.client_changes(app, dist), ["src/client/new.ts"])
        (app / "src" / "client" / "new.ts").unlink()
        (app / "src" / "client" / "main.ts").unlink()
        self.assertEqual(ST.client_changes(app, dist), ["src/client/main.ts"])

    def test_missing_compiled_module_is_an_incomplete_build(self):
        app, dist = _app(self.root)
        (dist / "js" / "client" / "main.js").unlink()
        f = ST.check_client(app, dist)
        self.assertEqual(f.status, "error")
        self.assertIn("js/client/main.js", f.detail)

    def test_build_without_record_cannot_be_compared(self):
        app, dist = _app(self.root)
        (dist / ST.BUILD_RECORD).unlink()
        self.assertIsNone(ST.client_changes(app, dist))
        self.assertEqual(ST.check_client(app, dist).status, "warn")
        (dist / ST.BUILD_RECORD).write_text("not json")
        self.assertIsNone(ST.client_changes(app, dist))

    def test_missing_loader_is_an_error(self):
        app, dist = _app(self.root)
        (dist / "startup" / "loader.js").unlink()
        self.assertEqual(ST.check_client(app, dist).status, "error")

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_node_and_python_write_the_same_record_on_the_real_tree(self):
        out = Path(self.tmp.name) / "dist"
        out.mkdir()
        script = (f"import {{ recordBuild }} from {json.dumps((APP_ROOT / 'tools' / 'record-build.mjs').as_uri())};"
                  f"recordBuild({json.dumps(str(APP_ROOT))}, {json.dumps(str(out))});")
        done = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        node_map = json.loads((out / "build.json").read_text())["files"]
        self.assertTrue(node_map)
        self.assertEqual(node_map, ST.client_inputs(APP_ROOT))

    def test_committed_build_matches_its_sources(self):
        """The dist/ that is committed was built from the sources that are committed (rebuild with `npm run build` when this fails)."""
        dist = APP_ROOT / "dist"
        if not (dist / ST.BUILD_RECORD).is_file():
            self.skipTest("dist/build.json is not present")
        self.assertEqual(ST.client_changes(APP_ROOT, dist), [])


class ModeTests(unittest.TestCase):
    def run_main(self, *argv: str) -> tuple[int, str, str]:
        from slai.__main__ import main
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_check_exits_nonzero_when_something_blocks(self):
        with tempfile.TemporaryDirectory() as t, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SLAI_ROOT", None)           # otherwise the environment's checkout is found as well
            code, out, _ = self.run_main("--check", "--data-dir", t, "--client-dir", str(Path(t) / "nothing"), "--slai-root", t)
        self.assertEqual(code, 1)
        self.assertIn("Cannot start", out)
        self.assertIn("[ERROR] SLAI checkout", out)
        self.assertIn("[ERROR] Web client", out)

    def test_remote_host_is_still_refused(self):
        code, _, err = self.run_main("--check", "--host", "0.0.0.0")
        self.assertEqual(code, 2)
        self.assertIn("non-loopback", err)

    def test_build_without_node_modules_explains_itself(self):
        with tempfile.TemporaryDirectory() as t:
            ST.HERE, saved = Path(t), ST.HERE
            try:
                code, _, err = self.run_main("--build", "--data-dir", t)
            finally:
                ST.HERE = saved
        self.assertEqual(code, 1)
        self.assertIn("cannot start", err.lower())
        self.assertIn("npm", err)

    def test_build_client_reports_a_failing_build_with_its_output(self):
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "node_modules").mkdir()
            fake = lambda *a, **k: subprocess.CompletedProcess(a, 2, "", "error TS2322: bad")      # noqa: E731
            if ST.npm_command() is None:
                self.skipTest("npm is not installed")
            with self.assertRaises(ST.StartupError) as caught:
                ST.build_client(Path(t), run=fake)
        self.assertIn("TS2322", caught.exception.details["output"])
        self.assertEqual(caught.exception.details["exit"], 2)


if __name__ == "__main__":
    unittest.main()
