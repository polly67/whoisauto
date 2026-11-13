"""Start, stop and talk to the vendored opom API.

Port handling is the fiddly part: 8765 is a common port for other local
servers, and opom's CLI just dies if the bind fails. Here we first look for an
already-running opom (reuse it), then fall back to the next free port in the
range and remember it in ``runtime/state/opom-port.json``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

from .config import (DEFAULT_OPOM_PORT, PORT_SPAN, Paths, find_free_port, port_free,
                     read_json, write_json)


class ServiceError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class OpomService:
    def __init__(self, paths: Paths, preferred: int = DEFAULT_OPOM_PORT):
        self.paths = paths
        self.preferred = preferred

    # ---------------------------------------------------------------- discovery

    def health(self, port: int, timeout: float = 2.0) -> dict | None:
        try:
            status, payload = self._request("GET", "/health", None, port, timeout)
        except OSError:
            return None
        if status == 200 and payload.get("service") == "opom":
            return payload
        return None

    def running(self) -> tuple[int, dict] | None:
        recorded = read_json(self.paths.port_file, {}) or {}
        candidates = []
        if isinstance(recorded.get("port"), int):
            candidates.append(recorded["port"])
        candidates.extend(p for p in range(self.preferred, self.preferred + PORT_SPAN + 1)
                          if p not in candidates)
        for port in candidates:
            payload = self.health(port)
            if payload is not None:
                return port, payload
        return None

    def resolve_port(self) -> int:
        found = self.running()
        if found is not None:
            return found[0]
        port = find_free_port(self.preferred)
        write_json(self.paths.port_file, {"port": port, "pid": None, "reused": False})
        return port

    # ------------------------------------------------------------------ control

    def start(self) -> dict:
        if sys.platform != "darwin":
            raise ServiceError("unsupported_platform",
                               "opom's execution server requires macOS; the panel itself runs anywhere")
        found = self.running()
        if found is not None:
            port, payload = found
            write_json(self.paths.port_file, {"port": port, "pid": payload.get("pid"), "reused": True})
            return {"ok": True, "port": port, "reused": True, "health": payload}

        self.paths.ensure()
        port = self.resolve_port()
        command = [sys.executable, "-m", "opom", "serve", "--port", str(port),
                   "--root", str(self.paths.root), "--state", str(self.paths.opom_state)]
        environment = {**os.environ, **self.paths.env(port)}
        with self.paths.log_file.open("ab") as log:
            child = subprocess.Popen(command, cwd=str(self.paths.opom_dir), env=environment,
                                     stdout=log, stderr=log, start_new_session=True)

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise ServiceError("api_exited", self._log_tail())
            payload = self.health(port, timeout=1)
            if payload is not None and payload.get("pid") == child.pid:
                write_json(self.paths.port_file, {"port": port, "pid": child.pid, "reused": False})
                return {"ok": True, "port": port, "reused": False, "pid": child.pid, "health": payload}
            time.sleep(0.2)
        child.terminate()
        raise ServiceError("startup_timeout", f"opom did not answer on port {port}; see {self.paths.log_file}")

    def stop(self) -> dict:
        found = self.running()
        if found is None:
            return {"ok": True, "stopped": False, "message": "opom is not running"}
        port, payload = found
        try:
            self._request("POST", "/shutdown", {}, port, 5)
        except OSError as error:
            raise ServiceError("shutdown_failed", str(error)) from None
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.health(port, timeout=0.5) is None:
                self.paths.port_file.unlink(missing_ok=True)
                return {"ok": True, "stopped": True, "port": port, "pid": payload.get("pid")}
            time.sleep(0.15)
        raise ServiceError("shutdown_timeout", "opom is still answering")

    # -------------------------------------------------------------------- calls

    def call(self, method: str, path: str, payload=None, timeout: float = 40) -> tuple[int, dict]:
        found = self.running()
        if found is None:
            raise ServiceError("api_down", "opom is not running; start the API first")
        try:
            return self._request(method, path, payload, found[0], timeout)
        except TimeoutError:
            raise ServiceError("api_timeout", f"opom did not answer within {timeout:g}s") from None
        except OSError as error:
            raise ServiceError("api_unreachable", str(error)) from None

    def _request(self, method: str, path: str, payload, port: int,
                 timeout: float = 5) -> tuple[int, dict]:
        data = None if payload is None else json.dumps(payload).encode()
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        token = os.environ.get("OPOM_API_TOKEN")
        if token:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                         headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            with error:
                try:
                    return error.code, json.load(error)
                except ValueError:
                    return error.code, {"ok": False, "error": "http_error", "message": str(error)}

    def _log_tail(self, lines: int = 12) -> str:
        try:
            text = self.paths.log_file.read_text(errors="replace").strip().splitlines()
        except OSError:
            return "opom exited during startup (no log)"
        return "opom exited during startup:\n" + "\n".join(text[-lines:])

    # ------------------------------------------------------------------- status

    def status(self) -> dict:
        found = self.running()
        if found is None:
            return {"running": False, "port": None,
                    "preferred": self.preferred, "preferred_free": port_free(self.preferred),
                    "log": str(self.paths.log_file)}
        port, payload = found
        return {"running": True, "port": port, "health": payload, "preferred": self.preferred,
                "log": str(self.paths.log_file)}
