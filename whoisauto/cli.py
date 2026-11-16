"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import webbrowser

from . import compiler, installer
from .config import ConfigError, DEFAULT_OPOM_PORT, DEFAULT_WEB_PORT, Paths, write_json
from .service import OpomService, ServiceError
from .web import serve


def _paths(args) -> Paths:
    return Paths.load(getattr(args, "runtime", None))


def cmd_serve(args) -> int:
    paths = _paths(args)
    server = serve(paths, args.port, args.opom_port)
    host, port = server.server_address
    url = f"http://{host}:{port}"
    # Callers (CI, service managers) poll this instead of scraping stdout.
    record = paths.state / "panel.json"
    write_json(record, {"port": port, "url": url, "pid": os.getpid()})
    print(f"whoisauto: {url}", flush=True)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        record.unlink(missing_ok=True)
    return 0


def cmd_install(args) -> int:
    paths = _paths(args)
    result = installer.install_all(paths, args.source, args.roblox_source, args.launch)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


def cmd_start(args) -> int:
    service = OpomService(_paths(args), args.opom_port)
    print(json.dumps(service.start(), indent=2))
    return 0


def cmd_stop(args) -> int:
    print(json.dumps(OpomService(_paths(args), args.opom_port).stop(), indent=2))
    return 0


def cmd_build_compiler(args) -> int:
    paths = _paths(args)
    try:
        result = compiler.build(paths)
    except compiler.CompilerError as error:
        if error.log:
            print(error.log, file=sys.stderr)
        raise
    print(json.dumps(result, indent=2))
    return 0


def cmd_status(args) -> int:
    paths = _paths(args)
    print(json.dumps({"service": OpomService(paths, args.opom_port).status(),
                      "install": installer.probe(paths)}, indent=2))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="whoisauto",
                                     description="Local panel for the opom Opiumware API")
    parser.add_argument("command", nargs="?", default="serve",
                        choices=["serve", "install", "start", "stop", "status",
                                 "build-compiler"])
    parser.add_argument("--port", type=int, default=DEFAULT_WEB_PORT, help="panel port")
    parser.add_argument("--opom-port", type=int, default=DEFAULT_OPOM_PORT, help="preferred opom port")
    parser.add_argument("--runtime", help="runtime directory (default: ./runtime)")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    parser.add_argument("--source", help="Opiumware .app/.dmg/.zip or URL")
    parser.add_argument("--roblox-source", help="patched Roblox .app/.dmg/.zip or URL")
    parser.add_argument("--launch", action="store_true", help="open Opiumware after install")
    args = parser.parse_args(argv)
    try:
        return {"serve": cmd_serve, "install": cmd_install, "start": cmd_start,
                "stop": cmd_stop, "status": cmd_status,
                "build-compiler": cmd_build_compiler}[args.command](args)
    except (ConfigError, ServiceError, installer.InstallError, compiler.CompilerError, OSError) as error:
        print(f"whoisauto: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
