"""Run a stored script every time the client lands in a place.

Joining delivers a ``roblox://`` URL and returns immediately, so the script can
only run once opom reports the client as loaded and connected. The worker waits
for that, which also covers rejoining the same place.

Only one job runs at a time. A job can be cancelled while it is still waiting
for the place; once the script has been handed to opom it is already in the
game and cannot be pulled back.
"""

from __future__ import annotations

import threading
import time
import uuid

from .config import Paths, read_json, write_json
from .service import OpomService, ServiceError

DEFAULT_NAME = "autoexec.luau"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class AutoExec:
    def __init__(self, paths: Paths, service: OpomService):
        self.paths = paths
        self.service = service
        self._lock = threading.Lock()
        # one event per job: a rejoin retires the previous wait without
        # touching the job that replaces it
        self._event = threading.Event()
        self._job: dict | None = None
        self._last: dict | None = None

    # ------------------------------------------------------------------ config

    @property
    def file(self):
        return self.paths.state / "autoexec.json"

    def config(self) -> dict:
        data = read_json(self.file, {}) or {}
        script = data.get("script") or ""
        return {
            "enabled": bool(data.get("enabled")),
            "script": script,
            "name": data.get("name") or DEFAULT_NAME,
            "bytes": len(script.encode("utf-8")),
            "delay": _number(data.get("delay"), 3, 0, 120),
            "wait_for": _number(data.get("wait_for"), 90, 5, 600),
            "timeout": _number(data.get("timeout"), 30, 1, 120),
            "capture": bool(data.get("capture", True)),
        }

    def save(self, body: dict) -> dict:
        current = self.config()
        script = body.get("script", current["script"])
        if not isinstance(script, str):
            raise ValueError("script must be text")
        value = {
            "enabled": bool(body.get("enabled", current["enabled"])),
            "script": script,
            "name": (body.get("name") or current["name"] or DEFAULT_NAME).strip() or DEFAULT_NAME,
            "delay": _number(body.get("delay"), current["delay"], 0, 120),
            "wait_for": _number(body.get("wait_for"), current["wait_for"], 5, 600),
            "timeout": _number(body.get("timeout"), current["timeout"], 1, 120),
            "capture": bool(body.get("capture", current["capture"])),
        }
        write_json(self.file, value)
        return self.snapshot()

    def snapshot(self, include_script: bool = True) -> dict:
        config = self.config()
        if not include_script:
            config.pop("script", None)
        return {"ok": True, "config": config, "job": self.job(), "last": self.last()}

    def job(self) -> dict | None:
        with self._lock:
            return dict(self._job) if self._job else None

    def last(self) -> dict | None:
        with self._lock:
            return dict(self._last) if self._last else None

    # ------------------------------------------------------------------ control

    def trigger(self, trigger: str, place_id: int | None, port: int | None = None) -> dict | None:
        """Queue the stored script for a place. Returns the job, or None."""
        config = self.config()
        if not config["enabled"] or not config["script"].strip():
            return None
        with self._lock:
            if self._job and self._job["status"] == "running":
                return dict(self._job)
            if self._job and self._job["status"] == "waiting":
                self._event.set()
            self._event = threading.Event()
            event = self._event
            job = {
                "id": uuid.uuid4().hex[:12],
                "trigger": trigger,
                "place_id": place_id,
                "port": port,
                "script": config["name"],
                "bytes": config["bytes"],
                "status": "waiting",
                "started_at": _now(),
            }
            self._job = job
        threading.Thread(target=self._work,
                         args=(job["id"], config, place_id, port, event),
                         daemon=True).start()
        return dict(job)

    def cancel(self) -> dict:
        with self._lock:
            job = self._job
            if not job or job["status"] not in ("waiting", "running"):
                return {"ok": True, "cancelled": False, "message": "nothing is queued"}
            if job["status"] == "running":
                return {"ok": True, "cancelled": False,
                        "message": "script is already in the game and cannot be pulled back"}
            self._event.set()
            job["status"] = "cancelled"
            job["finished_at"] = _now()
            return {"ok": True, "cancelled": True, "job": dict(job)}

    # ------------------------------------------------------------------- worker

    def _set(self, job_id: str, **fields) -> None:
        with self._lock:
            if self._job and self._job["id"] == job_id:
                self._job.update(fields)

    def _finish(self, job_id: str, status: str, **fields) -> None:
        with self._lock:
            if self._job and self._job["id"] == job_id:
                self._job.update({"status": status, "finished_at": _now(), **fields})
                self._last = dict(self._job)

    def _work(self, job_id: str, config: dict, place_id: int | None, port: int | None,
              event: threading.Event) -> None:
        if event.wait(config["delay"]):
            return self._finish(job_id, "cancelled")
        self._set(job_id, status="running")

        # a manual run has no place to wait for; execute as soon as it is asked
        deadline = time.monotonic() + config["wait_for"] if place_id else time.monotonic()
        while place_id and time.monotonic() < deadline:
            runtime = self._runtime(port)
            if (runtime.get("place_id") == place_id and runtime.get("loaded")
                    and runtime.get("connected")):
                break
            if event.wait(1.0):
                return self._finish(job_id, "cancelled")
        else:
            if place_id:
                return self._finish(job_id, "timeout", error="place_not_loaded",
                                    message=f"client was not in {place_id} within {config['wait_for']:g}s")

        payload = {"script": config["script"], "wait": 5,
                   "timeout": config["timeout"], "capture": config["capture"]}
        if port:
            payload["port"] = port
        try:
            status, value = self.service.call("POST", "/execute", payload,
                                              timeout=config["timeout"] + 30)
        except ServiceError as error:
            return self._finish(job_id, "failed", error=error.code, message=error.message)
        if status >= 400:
            return self._finish(job_id, "failed", error=value.get("error", "execute_failed"),
                                message=value.get("message", ""))
        self._finish(job_id, "done", job_id_remote=value.get("job_id"),
                     instance=value.get("instance"))

    def _runtime(self, port: int | None) -> dict:
        path = f"/runtime?port={port}" if port else "/runtime"
        try:
            _, value = self.service.call("GET", path, timeout=10)
        except ServiceError:
            return {}
        return value.get("runtime") or {}


def _number(value, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(number, low), high)
