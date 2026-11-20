"""External programs must be stopped as a tree when they time out, and never run through a shell."""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from support import APP_ROOT  # noqa: F401
from slai.procs import run_bounded


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:                                              # a zombie still answers signal 0; its state says Z
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


class RunBoundedTests(unittest.TestCase):
    def test_output_and_exit_code(self):
        done = run_bounded([sys.executable, "-c", "import sys; print('out'); sys.stderr.write('err'); sys.exit(3)"], 20)
        self.assertEqual((done.returncode, done.stdout.strip(), done.stderr), (3, b"out", b"err"))

    def test_arguments_are_not_interpreted_by_a_shell(self):
        done = run_bounded([sys.executable, "-c", "import sys; print(sys.argv[1])", "a; echo injected $(id)"], 20)
        self.assertEqual(done.stdout.strip(), b"a; echo injected $(id)")

    def test_missing_program_is_an_oserror(self):
        with self.assertRaises(OSError):
            run_bounded(["/definitely/not/here"], 5)

    @unittest.skipIf(sys.platform == "win32", "uses a POSIX shell and /proc")
    def test_timeout_ends_the_grandchild_too(self):
        """The shape of soffice -> soffice.bin: the launcher starts a worker and waits for it."""
        with tempfile.TemporaryDirectory() as t:
            pidfile = Path(t) / "worker.pid"
            script = f"sleep 300 & echo $! > {pidfile}; wait"
            started = time.time()
            with self.assertRaises(subprocess.TimeoutExpired):
                run_bounded(["/bin/sh", "-c", script], 1.5)
            self.assertLess(time.time() - started, 10)
            worker = int(pidfile.read_text())
            time.sleep(0.3)
            self.assertFalse(_alive(worker), "the worker survived the timeout")

    @unittest.skipIf(sys.platform == "win32", "uses a POSIX shell and /proc")
    def test_plain_subprocess_run_does_leave_the_worker(self):
        """Why run_bounded exists: the standard call kills only the launcher (documents the behaviour, then cleans up)."""
        with tempfile.TemporaryDirectory() as t:
            pidfile = Path(t) / "worker.pid"
            with self.assertRaises(subprocess.TimeoutExpired):
                subprocess.run(["/bin/sh", "-c", f"sleep 300 & echo $! > {pidfile}; wait"], capture_output=True, timeout=1.5)
            worker = int(pidfile.read_text())
            time.sleep(0.3)
            survived = _alive(worker)
            if survived:
                os.kill(worker, 9)
            self.assertTrue(survived)


if __name__ == "__main__":
    unittest.main()
