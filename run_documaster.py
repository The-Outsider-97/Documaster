"""``python run_documaster.py`` (or double-click) starts Documaster; the same as ``python -m slai``.

Options: ``--check`` reports whether this installation can run Documaster, ``--build`` builds the web client, ``--dev`` rebuilds it first
when it is missing or older than its sources. See ``--help`` for the rest.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))          # this folder, so ``slai`` imports whatever the working directory is

from slai.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
