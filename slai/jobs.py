"""Background jobs: progress, cancellation, timeouts. Long operations (conversion, merging, OCR, redaction) run here so the
interface stays responsive and the person can cancel."""
from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from .errors import Cancelled, DocumasterError, TimedOut
from .services import JobControl


class DeadlineControl(JobControl):
    def __init__(self, progress: Callable[[float, str], None], timeout_s: float) -> None:
        super().__init__(progress)
        self.deadline = time.monotonic() + timeout_s if timeout_s else None

    def check(self) -> None:
        super().check()
        if self.deadline is not None and time.monotonic() > self.deadline:
            raise TimedOut("The operation took too long and was stopped")


class Job:
    def __init__(self, kind: str, label: str) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.kind, self.label = kind, label
        self.state = "queued"
        self.progress = 0.0
        self.message = "Waiting"
        self.result: Any = None
        self.error: dict[str, Any] | None = None
        self.created = time.time()
        self.started: float | None = None
        self.finished: float | None = None
        self.control: DeadlineControl | None = None

    def public(self, with_result: bool = True) -> dict[str, Any]:
        out = {"id": self.id, "kind": self.kind, "label": self.label, "state": self.state,
               "progress": round(self.progress, 3), "message": self.message, "error": self.error,
               "elapsedMs": round(((self.finished or time.time()) - self.started) * 1000) if self.started else 0}
        if with_result and self.state == "done":
            out["result"] = self.result
        return out


class JobManager:
    def __init__(self, timeout_s: int, workers: int = 2, keep: int = 200, hook: Callable[[Job, BaseException | None], None] | None = None) -> None:
        self.hook = hook
        self.timeout_s = timeout_s
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="documaster-job")
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.keep = keep

    def submit(self, kind: str, label: str, fn: Callable[[JobControl], Any]) -> Job:
        job = Job(kind, label)

        def progress(fraction: float, message: str) -> None:
            job.progress, job.message = fraction, message or job.message

        job.control = DeadlineControl(progress, self.timeout_s)
        with self.lock:
            self.jobs[job.id] = job
            for old in [j for j in self.jobs.values() if j.finished and len(self.jobs) > self.keep][:20]:
                self.jobs.pop(old.id, None)

        def run() -> None:
            if job.state == "cancelled":
                return
            job.state, job.started = "running", time.time()
            failure: BaseException | None = None
            job.control.deadline = time.monotonic() + self.timeout_s if self.timeout_s else None  # type: ignore[union-attr]
            try:
                job.result = fn(job.control)  # type: ignore[arg-type]
                job.state, job.progress, job.message = "done", 1.0, "Done"
            except Cancelled:
                job.state, job.message = "cancelled", "Cancelled"
            except DocumasterError as exc:
                failure = exc
                job.state, job.error, job.message = "failed", exc.to_dict(), exc.message
            except Exception as exc:  # noqa: BLE001 - never let a worker die silently
                failure = exc
                job.state, job.message = "failed", "Something went wrong"
                job.error = {"code": "internal_error", "message": f"{type(exc).__name__}: {exc}", "details": {}}
            finally:
                job.finished = time.time()
                if self.hook and job.state in ("done", "failed"):
                    try:
                        self.hook(job, failure)
                    except Exception:  # noqa: BLE001 - telemetry must never affect the job
                        pass

        self.pool.submit(run)
        return job

    def get(self, job_id: str) -> Job:
        from .errors import NotFound
        with self.lock:
            job = self.jobs.get(job_id)
        if job is None:
            raise NotFound("Unknown job")
        return job

    def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.state in ("queued", "running"):
            if job.control:
                job.control.cancel()
            if job.state == "queued":
                job.state, job.finished, job.message = "cancelled", time.time(), "Cancelled"
        return job

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            return [j.public(with_result=False) for j in sorted(self.jobs.values(), key=lambda j: -j.created)[:50]]

    def shutdown(self) -> None:
        for j in list(self.jobs.values()):
            if j.state in ("queued", "running") and j.control:
                j.control.cancel()
        self.pool.shutdown(wait=False, cancel_futures=True)
