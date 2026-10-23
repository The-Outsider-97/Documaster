"""The application object: settings, services, workspace and job manager, created once per run."""
from __future__ import annotations

import time

from .config import Settings
from .analysis import agent_calls
from .analysis.agent_calls import Consent
from .jobs import Job, JobManager
from .services import Services
from .workspace import Workspace


class App:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.started = time.time()
        self.svc = Services.create(settings)
        self.ws = Workspace(self.svc)
        self.consent = Consent(default=settings.agent_read_default and settings.agents != "none")
        self.jobs = JobManager(settings.job_timeout_s, hook=self._job_finished)

    def _job_finished(self, job: Job, exc: BaseException | None) -> None:
        """Telemetry to the Observability agent; a failure is also described by the Handler agent (both only if available)."""
        if not self.settings.agent_telemetry:
            return
        ms = ((job.finished or time.time()) - (job.started or time.time())) * 1000
        agent_calls.observe(self.svc, job.kind, ms, job.state == "done", job.message)
        if exc is not None and job.error is not None:
            diagnosis = agent_calls.normalise_failure(self.svc, exc, job.kind)
            if diagnosis:
                job.error["diagnosis"] = diagnosis

    def close(self) -> None:
        self.jobs.shutdown()
        self.ws.close()
        self.svc.agents.close()
