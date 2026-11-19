"""``python -m slai`` - start Documaster."""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import webbrowser
from pathlib import Path

from . import __version__
from . import bridge as BR
from . import engine as EN
from .bridge import activate, find_slai_root
from .config import Settings
from .errors import DocumasterError
from . import startup as ST


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m slai", description="SLAI Documaster - documents on your own machine")
    p.add_argument("--host", default="127.0.0.1", help="address to listen on (loopback only by default)")
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--slai-root", help="path to the SLAI checkout (default: found automatically)")
    p.add_argument("--data-dir", help="working folder for sessions")
    p.add_argument("--output-dir", help="where converted/merged files are written")
    p.add_argument("--agents", choices=("auto", "none", "strict"), default=None, help="use SLAI agents when available (default auto)")
    p.add_argument("--agent-telemetry", action="store_true", help="send job telemetry to the Observability agent and failures to the Handler agent")
    p.add_argument("--ask-before-agents", action="store_true", help="agents may read a document only after you allow it for that document (default: allowed; the Privacy agent still gates)")
    p.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    p.add_argument("--token", help="fixed access token (for tests; random by default)")
    p.add_argument("--client-dir", help="folder with the built web client")
    p.add_argument("--version", action="version", version=f"Documaster {__version__}")
    host = p.add_argument_group("for a host program (the desktop app)")
    host.add_argument("--ready-line", action="store_true",
                      help="print one 'DOCUMASTER_READY {json}' line when listening (or 'DOCUMASTER_ERROR text' when it cannot start); no other line carries the access token")
    host.add_argument("--exit-on-stdin-close", action="store_true", help="quit when standard input is closed (the host held it open)")
    host.add_argument("--port-fallback", action="store_true", help="if --port is taken, listen on any free port instead of failing")
    p.add_argument("--deep", action="store_true", help="with --check: also hash every file of the installed bundle (slower)")
    modes = p.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="report whether this installation can run Documaster (Python, packages, programs, SLAI, web client) and exit; 0 when it can")
    modes.add_argument("--build", action="store_true", help="build the web client (npm run build) and exit")
    modes.add_argument("--dev", action="store_true", help="development start: rebuild the web client first when it is missing or older than its sources")
    args = p.parse_args(argv)
    logging.getLogger("pypdf").setLevel(logging.ERROR)       # damaged-but-readable PDFs otherwise print a warning per object
    if args.ready_line:                                      # the host reads our stdout as UTF-8, whatever the console code page is
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
            except (AttributeError, ValueError):
                pass

    def fail(message: str, code: int) -> int:
        """Report why Documaster cannot start: for a person on stderr, and for a host as the protocol's error line on stdout."""
        print(message, file=sys.stderr, flush=True)
        if args.ready_line:
            print(EN.error_line(message), flush=True)
        return code

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        return fail("Refusing to listen on a non-loopback address: Documaster handles private documents and is a local app.", 2)
    s = Settings(host=args.host, port=args.port)
    if args.data_dir:
        s.data_dir = Path(args.data_dir).expanduser().resolve()
    bundle = EN.find_bundle(ST.HERE)                         # the installed desktop app: SLAI, models, client and helpers sit beside the code
    if bundle is not None:
        ST.BUNDLED = True
        EN.apply_environment(EN.bundled_environment(bundle, s.data_dir))
        BR.HELPER_DIRS[:] = bundle.helper_dirs()
    if args.output_dir:
        s.output_dir = Path(args.output_dir).expanduser().resolve()
    if args.token:
        s.token = args.token
    if args.agents:
        s.agents = args.agents
    s.agent_telemetry = args.agent_telemetry
    s.agent_read_default = not args.ask_before_agents
    s.open_browser = not args.no_browser
    try:
        # a bundle's own SLAI comes first: an SLAI_ROOT left in the user's environment must not swap the models of an installed app
        root = find_slai_root(args.slai_root or (str(bundle.slai_root) if bundle else os.environ.get("SLAI_ROOT")))
        activate(root)
        s.slai_root = root
    except DocumasterError as exc:
        return fail(f"Documaster needs the SLAI checkout: {exc.message}", 2)
    client_dir = Path(args.client_dir).expanduser().resolve() if args.client_dir else (bundle.client_dir if bundle else ST.HERE / "dist")
    try:
        if args.check:
            findings = ST.check_environment(root, client_dir, data_dir=s.data_dir, bundle=bundle)
            if bundle is not None and args.deep:
                findings.append(ST.check_bundle(bundle, deep=True))
            print(f"SLAI Documaster {__version__} on this installation:\n{ST.render(findings)}")
            return 1 if ST.blocking(findings) else 0
        if args.build or (args.dev and ST.check_client(ST.HERE, client_dir).status != "ok"):
            print(f"Building the web client in {ST.HERE} ...", flush=True)
            ST.build_client(ST.HERE, root=root)
            print("Built.", flush=True)
            if args.build:
                return 0
        problems = ST.blocking(ST.check_environment(root, client_dir, data_dir=s.data_dir, bundle=bundle))
        if problems:
            raise ST.StartupError("; ".join(f"{f.name}: {f.detail}" for f in problems),
                                  hint=" ".join(f.fix for f in problems if f.fix) or None,
                                  details={"check": "python -m slai --check"})
    except ST.StartupError as exc:
        if exc.details.get("output"):
            print(exc.details["output"], file=sys.stderr)
        elif exc.details.get("check"):
            print(f"Run `{exc.details['check']}` for the full report.", file=sys.stderr)
        return fail(f"Documaster cannot start: {exc.message} {exc.hint or ''}".strip(), 1)
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.sessions_dir.mkdir(parents=True, exist_ok=True)

    from .app import App
    from .server import DocumasterServer
    app = App(s)
    try:
        try:
            server = DocumasterServer(app, client_dir)
        except OSError as exc:
            if not args.port_fallback or s.port == 0:
                raise
            print(f"Port {s.port} is taken ({exc.strerror or exc}); using any free port.", flush=True)
            s.port = 0
            server = DocumasterServer(app, client_dir)
    except OSError as exc:
        app.close()
        return fail(f"Cannot listen on {s.host}:{s.port}: {exc.strerror or exc}. Is another copy of Documaster already running? Try --port.", 2)
    url = f"{server.base_url}/?token={s.token}"
    stale = ST.check_client(ST.HERE, client_dir)
    shown_url = "(given to the host)" if args.ready_line else url          # the tokenised address goes to the host only, never to a log
    print(f"SLAI Documaster {__version__}{' (installed app)' if bundle else ''}\n  SLAI root : {s.slai_root}\n  Output    : {s.output_dir}\n  Open      : {shown_url}", flush=True)
    if stale.status == "warn":
        print(f"  Note      : {stale.detail}. {stale.fix}", flush=True)
    if s.agents != "none":      # load the agents in the background so the System page shows the truth and the first use is not slow
        roles = ["language", "privacy", "safety", "quality"] + (["observability", "handler"] if s.agent_telemetry else [])
        app.svc.agents.warm_up(roles, log=lambda m: print(f"  agents    : {m}", flush=True))
    if s.open_browser and not args.ready_line:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    if args.exit_on_stdin_close:
        EN.watch_stdin(lambda: threading.Thread(target=server.shutdown, daemon=True, name="dm-shutdown").start())
    if args.ready_line:
        print(EN.ready_line(url, port=server.server_address[1], version=__version__, bundled=bundle is not None), flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()
    if args.exit_on_stdin_close:
        # A host asked us to quit. Agent worker threads (torch, executors) must not be able to keep the process alive after that.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
