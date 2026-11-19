"""Running external programs (LibreOffice, Tesseract) so that a timeout really stops them.

``subprocess.run(..., timeout=...)`` kills only the process it started. LibreOffice's ``soffice`` is a launcher that starts ``soffice.bin``,
so after a timeout the real worker would keep running (and keep the document open). Here the program starts in its own process group
(POSIX) or process tree (Windows) and the whole group is ended on timeout.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from typing import Sequence


def _kill_tree(proc: subprocess.Popen) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=15, check=False)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def run_bounded(cmd: Sequence[str], timeout: float, *, text: bool = False) -> subprocess.CompletedProcess:
    """Like ``subprocess.run(cmd, capture_output=True, timeout=timeout)`` but the whole process tree is ended on timeout.

    Raises ``subprocess.TimeoutExpired`` (after the tree is gone) or ``OSError`` when the program cannot be started. The argument list is
    never passed through a shell.
    """
    extra: dict = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else {"start_new_session": True}
    with subprocess.Popen(list(cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=text, **extra) as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            proc.communicate()                    # reap, and drain the pipes so nothing blocks
            raise
        except BaseException:
            _kill_tree(proc)
            raise
        return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)
