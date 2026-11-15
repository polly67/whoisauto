"""Cookie-authenticated Roblox Player launch and explicit lifecycle controls."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.parse

from . import installer, roblox
from .config import Paths
from .service import OpomService, ServiceError


class PlayerError(RuntimeError):
    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        super().__init__(message)


class PlayerControls:
    def __init__(self, paths: Paths, service: OpomService, before_stop=None):
        self.paths = paths
        self.service = service
        self._lock = threading.Lock()
        self._session: tuple[bytes, int] | None = None
        self._before_stop = before_stop or (lambda: None)

    def _ensure_service(self) -> None:
        if sys.platform != "darwin":
            raise PlayerError(400, "unsupported_platform", "Roblox Player controls require macOS")
        try:
            if not self.service.status().get("running"):
                self.service.start()
        except ServiceError as error:
            raise PlayerError(503, error.code, str(error)) from None

    def _call(self, method: str, path: str, payload=None, timeout: float = 15) -> dict:
        try:
            status, value = self.service.call(method, path, payload, timeout=timeout)
        except ServiceError as error:
            raise PlayerError(503, error.code, str(error)) from None
        if status >= 400:
            raise PlayerError(status, value.get("error", "player_failed"),
                              value.get("message", "Roblox Player operation failed"))
        return value

    def _instances(self) -> list[dict]:
        instances = self._call("GET", "/instances").get("instances")
        if not isinstance(instances, list) or any(not isinstance(item, dict) or
                                                type(item.get("port")) is not int for item in instances):
            raise PlayerError(502, "api_bad_response", "opom returned invalid Player instances")
        return instances

    def open(self, token: str) -> dict:
        with self._lock:
            self._ensure_service()
            existing = self._instances()
            fingerprint = hashlib.sha256(token.encode("utf-8")).digest()
            if self._session and self._session[0] == fingerprint:
                owned = next((item for item in existing if item.get("pid") == self._session[1]), None)
                if owned:
                    return {"ok": True, "reused": True, "instance": owned}
            return self._start(token)

    def restart(self, token: str) -> dict:
        with self._lock:
            self._ensure_service()
            return self._start(token)

    def _start(self, token: str) -> dict:
        try:
            installer.verify_app(self.paths.roblox_app, require_loader=True)
        except installer.InstallError as error:
            raise PlayerError(400, "app_missing", str(error)) from None
        self._helper()
        try:
            ticket = roblox.authentication_ticket(token)
        except roblox.RobloxError as error:
            raise PlayerError(error.status or 502, error.code, str(error)) from None
        stopped = self._stop()
        instance = self._launch(ticket)
        self._session = (hashlib.sha256(token.encode("utf-8")).digest(), instance["pid"])
        return {"ok": True, "reused": False, "stopped": stopped,
                "instance": instance}

    def stop(self) -> dict:
        with self._lock:
            self._ensure_service()
            return {"ok": True, "stopped": self._stop()}

    def _stop(self) -> list[dict]:
        self._helper()
        self._before_stop()
        stopped = []
        for instance in self._instances():
            value = self._call("POST", "/instances/stop", {"port": instance["port"], "force": True})
            stopped.append(value.get("stopped", instance))
        stopped.extend({"pid": pid, "port": None} for pid in self._stop_unregistered())
        self._session = None
        return stopped

    def _stop_unregistered(self) -> list[int]:
        try:
            result = subprocess.run([str(self._helper()), "stop", str(self.paths.roblox_app),
                                     str(self.paths.opom_state / "players")],
                                    input="", capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            raise PlayerError(502, "player_stop_failed", "Could not stop Roblox Player") from None
        if result.returncode:
            raise PlayerError(502, "player_stop_failed", "Roblox Player did not stop")
        try:
            pids = json.loads(result.stdout)["stopped_pids"]
            if not isinstance(pids, list) or any(type(pid) is not int or pid <= 0 for pid in pids):
                raise ValueError("invalid pids")
            return pids
        except (ValueError, KeyError, TypeError):
            raise PlayerError(502, "player_stop_failed", "The launcher returned invalid stop information") from None

    def _helper(self) -> Path:
        source = Path(__file__).resolve().parent / "native" / "open_roblox.swift"
        binary = self.paths.opom_state / "whoisauto-open-roblox"
        if binary.is_file():
            return binary
        self.paths.opom_state.mkdir(parents=True, exist_ok=True)
        try:
            result = subprocess.run(["xcrun", "swiftc", str(source), "-O", "-o", str(binary)],
                                    capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            raise PlayerError(502, "helper_build_failed", "Could not build the macOS Roblox launcher") from None
        if result.returncode:
            raise PlayerError(502, "helper_build_failed", "Could not build the macOS Roblox launcher")
        return binary

    def _launch(self, ticket: str) -> dict:
        url = ("roblox-player:1+launchmode:app+gameinfo:" + urllib.parse.quote(ticket, safe="")
               + "+robloxLocale:en_us+gameLocale:en_us+channel:+LaunchExp:InApp")
        try:
            result = subprocess.run([str(self._helper()), "open", str(self.paths.roblox_app),
                                     str(self.paths.opom_state / "players")],
                                    input=url, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            raise PlayerError(502, "player_launch_failed", "Roblox did not open; try Restart Roblox") from None
        if result.returncode:
            # Native errors may echo a URL containing the one-use ticket.
            raise PlayerError(502, "player_launch_failed", "Roblox did not accept the login launch")
        try:
            pid = json.loads(result.stdout)["pid"]
            if type(pid) is not int or pid <= 0:
                raise ValueError("invalid pid")
        except (ValueError, KeyError, TypeError):
            raise PlayerError(502, "player_launch_failed", "The launcher returned invalid Player information") from None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            found = next((item for item in self._instances() if item.get("pid") == pid), None)
            if found:
                return found
            time.sleep(0.25)
        # The home/login window can exist before the injected transport starts.
        # Opening the app succeeded; absence of an execution port is not a login failure.
        return {"pid": pid, "port": None}
