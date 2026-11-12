"""Run API Sentinel and the demo deployment in ONE process.

    python serve.py [--port 8000] [--demo-port 8100] [--no-demo]

start.bat uses this. Both servers live in the same Python process, so pressing Ctrl+C or closing the window stops
both and frees both ports. (The old start.bat launched the demo deployment as a separate background process; when
Windows asked "Terminate batch job (Y/N)?" and you answered Y, the batch file stopped before its clean-up step and that
process kept port 8100.)
"""
from __future__ import annotations

import argparse
import socket
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn  # noqa: E402


def port_busy(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def start_demo(port: int) -> uvicorn.Server | None:
    """The demo deployment for live tests, in a daemon thread of this process (it dies with the process)."""
    if port_busy(port):
        print(f"[info]  Port {port} is busy, so the demo deployment is not started.", flush=True)
        return None
    from demo_target.petstore_v2 import app as demo_app
    server = uvicorn.Server(uvicorn.Config(demo_app, host="127.0.0.1", port=port, log_level="warning"))
    # uvicorn installs signal handlers only in the main thread, so Ctrl+C is handled by the main server below
    threading.Thread(target=server.run, name="demo-deployment", daemon=True).start()
    print(f"[ok]    Demo deployment for live tests: http://127.0.0.1:{port}/v1", flush=True)
    return server


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--demo-port", type=int, default=8100)
    ap.add_argument("--no-demo", action="store_true", help="do not start the demo deployment")
    args = ap.parse_args(argv)

    demo = None if args.no_demo else start_demo(args.demo_port)
    server = uvicorn.Server(uvicorn.Config("app.main:app", host="127.0.0.1", port=args.port))
    try:
        server.run()  # graceful shutdown on Ctrl+C; uvicorn then re-raises KeyboardInterrupt
    except KeyboardInterrupt:
        pass
    finally:
        if demo is not None:
            demo.should_exit = True
    if server.started:
        ports = [str(args.port)] + ([str(args.demo_port)] if demo is not None else [])
        print(f"[ok]    API Sentinel stopped. Port{'s' if len(ports) > 1 else ''} {' and '.join(ports)} "
              f"{'are' if len(ports) > 1 else 'is'} free.", flush=True)
    return 0 if server.started else 1


if __name__ == "__main__":
    sys.exit(main())
