import ctypes
import json
import os
from pathlib import Path
import plistlib
import re
import signal
import socket
import subprocess
import threading
import time
import urllib.parse
import uuid
import zlib

from .core import APIError, REPO, SCRIPT_LIMIT, text, write_json

PORT_MIN, PORT_MAX = 8390, 8420


def process_path(pid):
    library = ctypes.CDLL("/usr/lib/libproc.dylib")
    library.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    library.proc_pidpath.restype = ctypes.c_int
    value = ctypes.create_string_buffer(4096)
    if library.proc_pidpath(pid, value, len(value)) <= 0:
        return None
    return Path(os.fsdecode(value.value)).resolve()


class Backend:
    def __init__(self, config):
        self.config = config
        self.lock = threading.Lock()
        self.discovery_lock = threading.Lock()
        self.cache = []
        self.cache_time = 0

    def apps(self):
        result = {}
        for name, app in [("roblox", self.config.roblox), ("opiumware", self.config.editor)]:
            item = {"available": False, "path": str(app)}
            try:
                with (app / "Contents/Info.plist").open("rb") as stream:
                    info = plistlib.load(stream)
                executable = app / "Contents/MacOS" / info["CFBundleExecutable"]
                item.update(available=executable.is_file(), version=info.get("CFBundleShortVersionString"))
                if name == "roblox":
                    item["available"] = item["available"] and (app / "Contents/Resources/libOpiumware.dylib").is_file()
            except (OSError, ValueError, KeyError):
                pass
            result[name] = item
        return result

    def instances(self, refresh=False):
        with self.discovery_lock:
            if not refresh and time.monotonic() - self.cache_time < .25:
                return list(self.cache)
            allowed = {(self.config.roblox / "Contents/MacOS/RobloxPlayer").resolve()}
            copies = self.config.state / "players"
            if copies.is_dir():
                allowed.update(path.resolve() for path in copies.glob("*/Roblox.app/Contents/MacOS/RobloxPlayer"))
            installed = Path("/Applications/Roblox.app")
            if (installed / "Contents/Resources/libOpiumware.dylib").is_file():
                allowed.add((installed / "Contents/MacOS/RobloxPlayer").resolve())
            try:
                listing = subprocess.run(["/usr/sbin/lsof", "-nP", f"-iTCP:{PORT_MIN}-{PORT_MAX}",
                                          "-sTCP:LISTEN", "-Fpcn"],
                                         capture_output=True, text=True, timeout=5)
            except (OSError, subprocess.TimeoutExpired) as error:
                raise APIError(503, "discovery_failed", str(error)) from None
            found = []
            pid, executable = None, None
            for line in listing.stdout.splitlines():
                if line.startswith("p"):
                    pid = int(line[1:])
                    executable = process_path(pid)
                elif line.startswith("n127.0.0.1:") and executable in allowed:
                    port = int(line.rsplit(":", 1)[1])
                    if PORT_MIN <= port <= PORT_MAX:
                        value = {"port": port, "pid": pid, "executable": str(executable)}
                        if value not in found:
                            found.append(value)
            self.cache = sorted(found, key=lambda value: value["port"])
            self.cache_time = time.monotonic()
            return list(self.cache)

    def select(self, port=None):
        if port is not None and (type(port) is not int or not PORT_MIN <= port <= PORT_MAX):
            raise APIError(400, "invalid_port", f"port must be from {PORT_MIN} to {PORT_MAX}")
        instances = self.instances(refresh=True)
        if not instances:
            raise APIError(503, "no_player", "Launch Opiumware's modified Roblox first")
        selected = next((value for value in instances if port is None or value["port"] == port), None)
        if selected is None:
            raise APIError(404, "instance_not_found", "No Opiumware Player on that port")
        return selected

    def send(self, source, port=None, prefix="OpiumwareScript "):
        source = text(source, "script")
        if len(source.encode()) > 2 * SCRIPT_LIMIT:
            raise APIError(413, "packet_too_large", "Expanded script exceeds the transport limit")
        selected = self.select(port)
        packet = zlib.compress((prefix + source).encode(), 6)
        connected = False
        with self.lock:
            try:
                with socket.create_connection(("127.0.0.1", selected["port"]), timeout=5) as connection:
                    connected = True
                    connection.sendall(packet)
                    connection.shutdown(socket.SHUT_WR)
            except TimeoutError:
                raise APIError(504, "delivery_timeout", "Sending timed out", submission_uncertain=connected) from None
            except OSError as error:
                raise APIError(502, "delivery_failed", str(error), submission_uncertain=connected) from None
        return {"status": "submitted", "instance": selected, "compressed_bytes": len(packet)}

    def setting(self, name, enabled, port=None):
        name = text(name, "name")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", name) or type(enabled) is not bool:
            raise APIError(400, "invalid_setting", "Use a setting name without spaces and a boolean enabled value")
        receipt = self.send(name + " " + str(enabled).lower(), port, "OpiumwareSetting ")
        value = {"ok": True, "name": name, "enabled": enabled, **receipt}
        write_json(self.config.state / "settings" / f"{receipt['instance']['port']}-{name}.json", value)
        return value

    def settings(self, port=None):
        directory = self.config.state / "settings"
        values = []
        if directory.is_dir():
            for path in sorted(directory.glob("*.json")):
                value = json.loads(path.read_text())
                if port is None or value["instance"]["port"] == port:
                    values.append(value)
        return {"ok": True, "requested": values, "confirmed_state": None}

    def launch(self, new_instance=False, timeout=45):
        existing = self.instances(refresh=True)
        if existing and not new_instance:
            return {"ok": True, "reused": True, "instance": existing[0]}
        if not self.apps()["roblox"]["available"]:
            raise APIError(503, "app_missing", "Modified Roblox app is unavailable")
        self.config.state.mkdir(parents=True, exist_ok=True)
        app = self.config.roblox
        if new_instance:
            if len(existing) >= 8:
                raise APIError(429, "instance_limit", "At most eight opom Player instances are supported")
            app = self.config.state / "players" / uuid.uuid4().hex / "Roblox.app"
            app.parent.mkdir(parents=True)
            copied = subprocess.run(["/usr/bin/ditto", str(self.config.roblox), str(app)],
                                    capture_output=True, text=True, timeout=60)
            if copied.returncode:
                raise APIError(502, "copy_failed", copied.stderr[-2000:])
        executable = app / "Contents/MacOS/RobloxPlayer"
        log_path = self.config.state / f"player-{time.time_ns()}.log"
        with log_path.open("ab") as log:
            child = subprocess.Popen([str(executable)], cwd=executable.parent,
                                     stdout=log, stderr=log, start_new_session=True)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise APIError(502, "launch_failed", "Player exited during startup", log=str(log_path))
            selected = next((value for value in self.instances(refresh=True) if value["pid"] == child.pid), None)
            if selected:
                write_json(self.config.state / f"player-{child.pid}.json", {**selected, "log": str(log_path)})
                return {"ok": True, "reused": False, "instance": selected}
            time.sleep(.25)
        raise APIError(504, "startup_timeout", "Player has not opened an execution port", pid=child.pid, log=str(log_path))

    def stop(self, port=None, force=False):
        selected = self.select(port)
        expected = Path(selected["executable"]).resolve()
        if process_path(selected["pid"]) != expected:
            raise APIError(409, "process_changed", "Player process changed before stopping")
        os.kill(selected["pid"], signal.SIGTERM)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and process_path(selected["pid"]) == expected:
            time.sleep(.1)
        forced = False
        if force and process_path(selected["pid"]) == expected:
            os.kill(selected["pid"], signal.SIGKILL)
            forced = True
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and process_path(selected["pid"]) == expected:
                time.sleep(.1)
        if process_path(selected["pid"]) == expected:
            raise APIError(504, "stop_timeout", "Player did not exit after SIGTERM")
        return {"ok": True, "stopped": selected, "forced": forced}

    def open_editor(self):
        if not self.apps()["opiumware"]["available"]:
            raise APIError(503, "app_missing", "Opiumware editor is unavailable")
        result = subprocess.run(["/usr/bin/open", "-a", str(self.config.editor)], capture_output=True, text=True, timeout=10)
        if result.returncode:
            raise APIError(502, "open_failed", result.stderr.strip())
        return {"ok": True, "opened": str(self.config.editor)}

    def join(self, place_id, port=None, job_id=None):
        if type(place_id) is not int or place_id <= 0:
            raise APIError(400, "invalid_place_id", "place_id must be a positive integer")
        selected = self.select(port)
        helper = self.config.state / "open-player"
        if not helper.is_file():
            self.config.state.mkdir(parents=True, exist_ok=True)
            built = subprocess.run(["xcrun", "swiftc", str(REPO / "native/open_player.swift"),
                                    "-O", "-o", str(helper)], capture_output=True, text=True, timeout=60)
            if built.returncode:
                raise APIError(503, "helper_build_failed", built.stderr[-2000:])
        query = {"placeId": str(place_id)}
        if job_id is not None:
            query["gameInstanceId"] = text(job_id, "game_instance_id")
        uri = "roblox://experiences/start?" + urllib.parse.urlencode(query)
        app = Path(selected["executable"]).parents[2]
        delivered = subprocess.run([str(helper), str(app), str(selected["pid"]), uri],
                                   capture_output=True, text=True, timeout=25)
        if delivered.returncode:
            raise APIError(502, "join_delivery_failed", delivered.stderr.strip())
        return {"ok": True, "url_delivered": True, "joined": False,
                "place_id": place_id, "instance": selected}
