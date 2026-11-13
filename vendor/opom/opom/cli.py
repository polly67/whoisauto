import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from .core import Config, REPO, write_json


def request(port, path="/health", body=None):
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("OPOM_API_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    url = f"http://127.0.0.1:{port}{path}"
    data = None if body is None else json.dumps(body).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=10) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description="opom: local Opiumware API")
    parser.add_argument("command", choices=["serve", "start", "stop", "status", "build-compiler"])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--state", type=Path)
    args = parser.parse_args()
    config = Config.load(args.root, args.state)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be from 1 to 65535")
    if args.command == "build-compiler":
        subprocess.run([sys.executable, str(REPO / "tools/build_compiler.py"), "--state", str(config.state)], check=True)
        return 0
    if args.command == "serve":
        if sys.platform != "darwin":
            parser.error("Player execution requires macOS")
        from .server import Server
        server = Server(("127.0.0.1", args.port), config)
        def stop(signum, frame):
            threading.Thread(target=server.shutdown, daemon=True).start()
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        print(f"opom: http://127.0.0.1:{args.port}/docs", flush=True)
        try:
            server.serve_forever(poll_interval=.1)
        finally:
            server.server_close()
        return 0
    current = None
    try:
        current = request(args.port)
    except urllib.error.HTTPError as error:
        error.close()
        if error.code in (401, 403):
            raise RuntimeError("The API is running but authentication failed; set OPOM_API_TOKEN correctly") from None
        raise RuntimeError(f"The listener returned HTTP {error.code}") from None
    except (OSError, ValueError):
        pass
    if args.command == "status":
        print(json.dumps(current or {"ok": False, "error": "not_running"}, indent=2))
        return 0 if current else 1
    if args.command == "stop":
        if not current:
            print("opom is already stopped")
            return 0
        if current.get("service") != "opom":
            raise RuntimeError("The listener on this port is not opom")
        print(json.dumps(request(args.port, "/shutdown", {})))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                request(args.port)
            except OSError:
                return 0
            time.sleep(.1)
        raise RuntimeError("opom has not stopped")
    if current:
        if current.get("service") != "opom":
            raise RuntimeError("Another API uses this port; use --port or stop that API")
        print(f"opom already running: http://127.0.0.1:{args.port}/docs")
        return 0
    config.state.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m", "opom", "serve", "--port", str(args.port),
               "--root", str(config.root), "--state", str(config.state)]
    with (config.state / "api.log").open("ab") as log:
        child = subprocess.Popen(command, cwd=REPO, stdout=log, stderr=log, start_new_session=True)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError("opom exited during startup; inspect " + str(config.state / "api.log"))
        try:
            current = request(args.port)
            if current.get("service") == "opom" and current.get("pid") == child.pid:
                write_json(config.state / f"api-{args.port}.json", {"pid": child.pid, "port": args.port})
                print(f"opom running: http://127.0.0.1:{args.port}/docs")
                return 0
        except (OSError, ValueError):
            pass
        time.sleep(.1)
    child.terminate()
    raise RuntimeError("opom did not become healthy")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
