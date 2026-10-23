"""Gateway to the real SLAI agents (``src/agents``) through SLAI's own ``AgentFactory``.

Rules this module enforces for Documaster:

* agents are created lazily, one per role, through ``AgentFactory().create(name, shared_memory=...)``
* every call has a time budget; a call that overruns is abandoned, never waited on forever
* a role that fails repeatedly is bypassed for a cool-down (circuit breaker) and then probed again
* an unavailable agent raises :class:`~slai.errors.AgentUnavailable` with the *reason*; callers show that reason
  and use the documented deterministic path. Nothing here fabricates an agent answer.
* ``agents="none"`` in the settings disables agent use entirely (privacy / offline), ``"strict"`` makes the
  failure to load a requested role an error instead of a degradation.
"""
from __future__ import annotations

import os
import threading
import time
import traceback
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Any, Callable

from ..errors import AgentUnavailable, TimedOut
from . import slai_root

ROLES = ("language", "reader", "privacy", "safety", "quality", "observability", "handler", "planning")


class Slot:
    def __init__(self, name: str) -> None:
        self.name = name
        self.state = "pending"        # pending | ready | unavailable | disabled
        self.instance: Any = None
        self.cls = ""
        self.error: str | None = None
        self.load_ms = 0.0
        self.calls = self.failures = self.timeouts = self.skipped = self.consecutive = 0
        self.cooldown = 15.0
        self.open_until = 0.0
        self.last_error: str | None = None
        self.last_trace: str | None = None
        self.lat: deque = deque(maxlen=100)
        self.run_lock = threading.Lock()   # real agents are not promised to be re-entrant
        self.test: dict[str, Any] | None = None   # last self-test: {"ok": bool, "detail": str, "ms": int, "at": float}

    def breaker_open(self) -> bool:
        return time.monotonic() < self.open_until

    def view(self) -> dict[str, Any]:
        lat = sorted(self.lat)
        p95 = round(lat[min(len(lat) - 1, int(0.95 * len(lat)))], 1) if lat else 0.0
        return {"state": self.state, "class": self.cls, "loadMs": round(self.load_ms), "error": self.error,
                "calls": self.calls, "failures": self.failures, "timeouts": self.timeouts,
                "skipped": self.skipped, "breakerOpen": self.breaker_open(), "lastError": self.last_error,
                "latencyP95Ms": p95, "test": self.test}


class AgentGateway:
    TRIP_AFTER = 3
    MAX_COOLDOWN = 240.0

    def __init__(self, mode: str = "auto", timeout_s: float = 60.0, log: Callable[[str], None] | None = None) -> None:
        self.mode = mode
        self.timeout_s = timeout_s
        self.log = log or (lambda m: None)
        self.slots: dict[str, Slot] = {r: Slot(r) for r in ROLES}
        self.factory: Any = None
        self.shared_memory: Any = None
        self.fatal: str | None = None
        self._init_lock = threading.Lock()
        self._create_lock = threading.Lock()   # warm-up and a first request must not both build the same agent
        self._initialised = False
        self.executor = ThreadPoolExecutor(max_workers=6, thread_name_prefix="dm-agent")
        if mode == "none":
            for slot in self.slots.values():
                slot.state, slot.error = "disabled", "agents are disabled (--agents none)"

    # ------------------------------------------------------------------ factory
    def _init_factory(self) -> None:
        with self._init_lock:
            if self._initialised:
                return
            self._initialised = True
            if slai_root() is None:
                self.fatal = "SLAI checkout not found (set SLAI_ROOT or --slai-root); agents are unavailable"
                return
            try:
                os.chdir(str(slai_root()))  # real agents write logs/checkpoints relative to the working directory
            except OSError:
                pass
            try:
                from src.agents.agent_factory import AgentFactory  # type: ignore
                self.factory = AgentFactory()
            except Exception as exc:  # noqa: BLE001
                self.fatal = f"AgentFactory could not be created: {type(exc).__name__}: {str(exc)[:300]}"
                return
            try:
                from src.agents.collaborative.shared_memory import SharedMemory  # type: ignore
                self.shared_memory = SharedMemory()
            except Exception as exc:  # noqa: BLE001 - optional
                self.log(f"SharedMemory unavailable ({type(exc).__name__}); agents use their own")

    def ensure(self, role: str) -> Slot:
        """Create the agent for ``role`` on first use. Returns its slot (state ``ready`` or ``unavailable``)."""
        slot = self.slots[role]
        if slot.state in ("ready", "disabled", "unavailable"):
            return slot
        with self._create_lock:
            if slot.state in ("ready", "disabled", "unavailable"):
                return slot
            return self._create(slot, role)

    def _create(self, slot: Slot, role: str) -> Slot:
        self._init_factory()
        if self.fatal or self.factory is None:
            slot.state, slot.error = "unavailable", self.fatal or "AgentFactory not available"
            return slot
        t0 = time.perf_counter()
        try:
            kwargs: dict[str, Any] = {}
            if self.shared_memory is not None:
                kwargs["shared_memory"] = self.shared_memory
            slot.instance = self.factory.create(role, **kwargs)
            if slot.instance is None:
                raise RuntimeError("AgentFactory returned None")
            slot.cls = type(slot.instance).__name__
            slot.state = "ready"
        except Exception as exc:  # noqa: BLE001
            slot.state = "unavailable"
            slot.error = f"{type(exc).__name__}: {str(exc)[:400]}"
        finally:
            slot.load_ms = (time.perf_counter() - t0) * 1000.0
        return slot

    def warm_up(self, roles: list[str] | tuple[str, ...], log: Callable[[str], None] | None = None) -> threading.Thread:
        """Create the given agents one after another in a background thread (the factory is not promised to be re-entrant)."""
        def go() -> None:
            for role in roles:
                slot = self.ensure(role)
                if log:
                    log(f"{role}: ready ({slot.cls}, {slot.load_ms / 1000:.1f} s)" if slot.state == "ready"
                        else f"{role}: {slot.state} - {slot.error}")
        thread = threading.Thread(target=go, daemon=True, name="dm-agent-warmup")
        thread.start()
        return thread

    def inject(self, role: str, instance: Any) -> None:
        """Install an already-built agent (tests, or a host that owns its own factory)."""
        slot = self.slots[role]
        slot.instance, slot.cls, slot.state, slot.error = instance, type(instance).__name__, "ready", None

    # ------------------------------------------------------------------ calls
    def usable(self, role: str) -> bool:
        slot = self.ensure(role)
        return slot.state == "ready" and not slot.breaker_open()

    def reason_unavailable(self, role: str) -> str | None:
        slot = self.ensure(role)
        if slot.state == "ready":
            return "circuit open after repeated failures" if slot.breaker_open() else None
        return slot.error or slot.state

    def _trip(self, slot: Slot) -> None:
        slot.consecutive += 1
        if slot.consecutive >= self.TRIP_AFTER:
            slot.open_until = time.monotonic() + slot.cooldown
            slot.cooldown = min(self.MAX_COOLDOWN, slot.cooldown * 2)
            slot.consecutive = 0

    def call(self, role: str, fn: Callable[[Any], Any], budget_s: float | None = None) -> Any:
        """Run ``fn(agent)`` with a time budget. Raises AgentUnavailable / TimedOut or what the agent raised."""
        slot = self.ensure(role)
        if slot.state != "ready":
            raise AgentUnavailable(f"The {role} agent is not available: {slot.error or slot.state}",
                                   details={"role": role, "reason": slot.error or slot.state})
        if slot.breaker_open():
            raise AgentUnavailable(f"The {role} agent is paused after repeated failures",
                                   details={"role": role, "reason": f"paused after repeated failures (last error: {slot.last_error})"})
        if not slot.run_lock.acquire(blocking=False):
            slot.skipped += 1
            raise AgentUnavailable(f"The {role} agent is still busy with a previous call", details={"role": role})
        budget = budget_s if budget_s is not None else self.timeout_s
        t0 = time.perf_counter()
        slot.calls += 1

        def run() -> Any:
            try:
                return fn(slot.instance)
            finally:
                slot.run_lock.release()

        try:
            future: Future = self.executor.submit(run)
        except Exception:
            slot.run_lock.release()
            raise
        try:
            out = future.result(timeout=budget)
        except FutureTimeout:
            slot.timeouts += 1
            slot.last_error = f"timeout after {budget:.0f}s"
            self._trip(slot)
            raise TimedOut(f"The {role} agent did not answer within {budget:.0f}s", details={"role": role}) from None
        except Exception as exc:  # noqa: BLE001
            slot.failures += 1
            slot.last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
            slot.last_trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-2000:]
            self._trip(slot)
            raise
        slot.consecutive = 0
        slot.lat.append((time.perf_counter() - t0) * 1000.0)
        return out

    # ------------------------------------------------------------------ reporting
    def status(self, probe: bool = False) -> dict[str, Any]:
        if probe:
            for role in ROLES:
                self.ensure(role)
        return {"mode": self.mode, "root": str(slai_root()) if slai_root() else None, "fatal": self.fatal,
                "agents": {name: slot.view() for name, slot in self.slots.items()}}

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)
