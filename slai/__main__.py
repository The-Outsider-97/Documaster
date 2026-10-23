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
from .bridge import activate, find_slai_root
from .config import Settings
from .errors import DocumasterError


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
    args = p.parse_args(argv)
    logging.getLogger("pypdf").setLevel(logging.ERROR)       # damaged-but-readable PDFs otherwise print a warning per object

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("Refusing to listen on a non-loopback address: Documaster handles private documents and is a local app.", file=sys.stderr)
        return 2
    s = Settings(host=args.host, port=args.port)
    if args.data_dir:
        s.data_dir = Path(args.data_dir).expanduser().resolve()
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
        root = find_slai_root(args.slai_root or os.environ.get("SLAI_ROOT"))
        activate(root)
        s.slai_root = root
    except DocumasterError as exc:
        print(f"Documaster needs the SLAI checkout: {exc.message}", file=sys.stderr)
        return 2
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.sessions_dir.mkdir(parents=True, exist_ok=True)

    from .app import App
    from .server import DocumasterServer
    client = Path(args.client_dir).expanduser().resolve() if args.client_dir else Path(__file__).resolve().parent.parent / "dist"
    app = App(s)
    try:
        server = DocumasterServer(app, client)
    except OSError as exc:
        print(f"Cannot listen on {s.host}:{s.port}: {exc}. Try --port.", file=sys.stderr)
        app.close()
        return 2
    url = f"{server.base_url}/?token={s.token}"
    print(f"SLAI Documaster {__version__}\n  SLAI root : {s.slai_root}\n  Output    : {s.output_dir}\n  Open      : {url}", flush=True)
    if s.agents != "none":      # load the agents in the background so the System page shows the truth and the first use is not slow
        roles = ["language", "privacy", "safety", "quality"] + (["observability", "handler"] if s.agent_telemetry else [])
        app.svc.agents.warm_up(roles, log=lambda m: print(f"  agents    : {m}", flush=True))
    if s.open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
