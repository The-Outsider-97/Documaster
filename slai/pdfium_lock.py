"""PDFium is not thread-safe (the pypdfium2 documentation says so): two threads drawing pages at once crash the process with an
access violation. The server answers requests on many threads (a viewer asks for a dozen thumbnails at once), so every function
that touches pdfium holds this one re-entrant lock."""
from __future__ import annotations

import functools
import threading
from typing import Any, Callable, TypeVar

PDFIUM_LOCK = threading.RLock()
F = TypeVar("F", bound=Callable[..., Any])


def pdfium_locked(fn: F) -> F:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with PDFIUM_LOCK:
            return fn(*args, **kwargs)
    return wrapper  # type: ignore[return-value]
