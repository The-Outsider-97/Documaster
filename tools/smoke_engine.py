#!/usr/bin/env python3
"""Start Documaster the way the desktop shell does and check that it really works. Standard library only.

    python tools/smoke_engine.py --dev                       # the checkout's Python, from this folder (what ``tauri dev`` runs)
    python tools/smoke_engine.py --bundle src-tauri/engine   # a bundle made by tools/build_bundle.py, with ITS Python (what the installer ships)
    python tools/smoke_engine.py --bundle src-tauri/engine --agents-test    # also make one real call to each SLAI agent

What it checks, in order, and stops at the first failure:

1. the engine prints ``DOCUMASTER_READY`` within ``--timeout`` seconds, with an address on the loopback;
2. that address opens (token link answers with the session cookie) and ``/api/health`` answers with the token as a bearer;
3. without the token the API answers 401 (the access check is on);
4. ``--agents-test``: ``/api/agents?test=1`` makes one real call to the Privacy, Safety, Language and Quality agents; each must come
   back ``ok``. For the language agent that is the real model answering. This is the check that proves the packaged SLAI and its
   model load and run. It can take a minute or two the first time (torch, then the model);
5. closing the engine's standard input makes it exit with status 0, promptly.

Exit status 0 only if everything asked for passed. Nothing here is mocked: if a step cannot run, it fails and says why.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
READY = "DOCUMASTER_READY "
ERROR = "DOCUMASTER_ERROR"
AGENT_ROLES = ("privacy", "safety", "language", "quality")


class SmokeFailure(RuntimeError):
    pass


def engine_command(bundle: Path | None, port: int) -> tuple[list[str], Path, dict[str, str]]:
    """(command, working folder, extra environment) that start the engine with the shell's arguments."""
    common = ["--no-browser", "--ready-line", "--exit-on-stdin-close", "--port-fallback", "--port", str(port)]
    if bundle is None:
        return [sys.executable, "-m", "slai", *common], APP, {}
    python = bundle / "python" / ("python.exe" if os.name == "nt" else "bin/python3")
    script = bundle / "app" / "run_documaster.py"
    for what, path in (("bundle.json", bundle / "bundle.json"), ("the bundled Python", python), ("run_documaster.py", script)):
        if not path.is_file():
            raise SmokeFailure(f"the bundle is incomplete: {what} is missing ({path})")
    # the same environment the shell gives it, and nothing from the developer's own Python setup
    return [str(python), str(script), *common], bundle, {"PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"}


class Engine:
    def __init__(self, command: list[str], cwd: Path, extra_env: dict[str, str], clean: bool) -> None:
        # A bundle must work on its own: nothing from the developer's Python or SLAI setup may leak into the check.
        env = {k: v for k, v in os.environ.items() if not clean or k not in ("PYTHONPATH", "PYTHONHOME", "SLAI_ROOT", "SLAI_MODEL_SOURCE")}
        env.update(extra_env)
        self.proc = subprocess.Popen(command, cwd=str(cwd), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
        self.lines: list[str] = []
        self.stderr: list[str] = []
        self.ready: dict | None = None
        self.error: str | None = None
        threading.Thread(target=self._pump_out, daemon=True).start()
        threading.Thread(target=self._pump_err, daemon=True).start()

    def _pump_out(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith(READY):
                try:
                    self.ready = json.loads(line[len(READY):])
                except ValueError:
                    self.lines.append(line)
            elif line.startswith(ERROR):
                self.error = line[len(ERROR):].strip() or "(no text)"
            else:
                self.lines.append(line)

    def _pump_err(self) -> None:
        assert self.proc.stderr is not None
        for raw in self.proc.stderr:
            self.stderr.append(raw.decode("utf-8", "replace").rstrip("\r\n"))

    def wait_ready(self, timeout: float) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            if self.ready:
                return self.ready
            if self.error is not None:
                raise SmokeFailure(f"the engine reported: {self.error}")
            if self.proc.poll() is not None:
                time.sleep(0.3)
                raise SmokeFailure(f"the engine exited with status {self.proc.returncode} before it was ready" + self.tail())
            time.sleep(0.05)
        raise SmokeFailure(f"no DOCUMASTER_READY line within {timeout:.0f} s" + self.tail())

    def tail(self) -> str:
        text = "\n".join((self.lines + self.stderr)[-15:])
        return f"\n--- last output ---\n{text}" if text else ""

    def quit(self, timeout: float = 15.0) -> int:
        if self.proc.stdin:
            self.proc.stdin.close()
        try:
            return self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(10)
            raise SmokeFailure(f"the engine did not exit within {timeout:.0f} s of its standard input closing")

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(10)


def request(base: str, path: str, token: str | None = None, timeout: float = 60.0) -> tuple[int, dict[str, str], bytes]:
    parts = urllib.parse.urlsplit(base)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout)
    try:
        conn.request("GET", path, headers={"Authorization": f"Bearer {token}"} if token else {})
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    finally:
        conn.close()


def run(bundle: Path | None, timeout: float, agents_test: bool, port: int, say=print) -> list[str]:
    """Run the checks; returns the lines of the report. Raises :class:`SmokeFailure` at the first check that fails."""
    command, cwd, extra = engine_command(bundle, port)
    report: list[str] = []

    def ok(text: str) -> None:
        report.append(f"[ok] {text}")
        say(report[-1])

    engine = Engine(command, cwd, extra, clean=bundle is not None)
    try:
        t0 = time.time()
        ready = engine.wait_ready(timeout)
        url = urllib.parse.urlsplit(ready["url"])
        if url.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise SmokeFailure(f"the engine's address is not on the loopback: {url.hostname}")
        token = urllib.parse.parse_qs(url.query).get("token", [""])[0]
        if len(token) < 16:
            raise SmokeFailure("the access token is missing or too short")
        base = f"http://{url.hostname}:{url.port}"
        ok(f"READY after {time.time() - t0:.1f} s on port {url.port} (version {ready.get('version')}, "
           f"{'bundled' if ready.get('bundled') else 'from a checkout'})")
        if bool(bundle) != bool(ready.get("bundled")):
            raise SmokeFailure(f"expected bundled={bool(bundle)} but the engine says bundled={ready.get('bundled')}")

        status, headers, _ = request(base, "/?token=" + token)
        if status != 302 or "dm_session=" not in headers.get("set-cookie", ""):
            raise SmokeFailure(f"the token link did not answer with the session cookie (status {status})")
        status, _, body = request(base, "/api/health", token)
        health = (json.loads(body) if status == 200 else {}).get("data") or {}
        if status != 200 or health.get("name") != "SLAI Documaster":
            raise SmokeFailure(f"/api/health answered {status}: {body[:200]!r}")
        ok(f"/api/health: {health['name']} {health['version']}")
        status, _, _ = request(base, "/api/health")
        if status != 401:
            raise SmokeFailure(f"the API answered {status} without a token; expected 401")
        ok("the API refuses requests without the token (401)")

        if agents_test:
            say("asking each SLAI agent for one real answer (the first time this loads torch and the model; it can take minutes) ...")
            t1 = time.time()
            status, _, body = request(base, "/api/agents?test=1", token, timeout=600)
            if status != 200:
                raise SmokeFailure(f"/api/agents?test=1 answered {status}: {body[:300]!r}")
            agents = json.loads(body)["data"]["agents"]
            problems = []
            for role in AGENT_ROLES:
                slot = agents.get(role, {})
                test = slot.get("test") or {}
                line = f"{role}: state={slot.get('state')} class={slot.get('class')} test={'ok' if test.get('ok') else 'FAILED'} - {test.get('detail') or slot.get('error')}"
                report.append(("[ok] " if slot.get("state") == "ready" and test.get("ok") else "[FAIL] ") + line)
                say(report[-1])
                if not (slot.get("state") == "ready" and test.get("ok")):
                    problems.append(role)
            if problems:
                raise SmokeFailure(f"agent(s) not working: {', '.join(problems)} (see the lines above)")
            ok(f"all {len(AGENT_ROLES)} agents answered ({time.time() - t1:.0f} s)")

        code = engine.quit()
        if code != 0:
            raise SmokeFailure(f"the engine exited with status {code} after its standard input closed")
        ok("closing the engine's standard input made it exit with status 0")
        return report
    except SmokeFailure as exc:
        report.append(f"[FAIL] {exc}")
        raise
    finally:
        engine.kill()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    where = ap.add_mutually_exclusive_group(required=True)
    where.add_argument("--dev", action="store_true", help="run the checkout with this Python (python -m slai)")
    where.add_argument("--bundle", type=Path, help="a bundle folder made by tools/build_bundle.py")
    ap.add_argument("--timeout", type=float, default=150.0, help="seconds to wait for the READY line (default 150, the shell's limit)")
    ap.add_argument("--agents-test", action="store_true", help="also make one real call to each SLAI agent (needs the SLAI agents and torch)")
    ap.add_argument("--port", type=int, default=0, help="preferred port (default: any free one)")
    args = ap.parse_args(argv)
    try:
        run(args.bundle, args.timeout, args.agents_test, args.port)
    except SmokeFailure as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    print("smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
