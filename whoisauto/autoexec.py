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
import math

from .config import Paths, read_json, write_json
from .service import OpomService, ServiceError

DEFAULT_NAME = "autoexec.luau"
SCRIPT_LIMIT = 4 * 1024 * 1024


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class AutoExec:
    def __init__(self, paths: Paths, service: OpomService):
        self.paths = paths
        self.service = service
        self._lock = threading.RLock()
        # one event per job: a rejoin retires the previous wait without
        # touching the job that replaces it
        self._event = threading.Event()
        self._job: dict | None = None
        self._last: dict | None = None
        self._closed = False

    # ------------------------------------------------------------------ config

    @property
    def file(self):
        return self.paths.state / "autoexec.json"

    def config(self) -> dict:
        data = read_json(self.file, {})
        if not isinstance(data, dict):
            data = {}
        script = data.get("script")
        if not isinstance(script, str):
            script = ""
        try:
            size = len(script.encode("utf-8"))
        except UnicodeEncodeError:
            script, size = "", 0
        if size > SCRIPT_LIMIT:
            script, size = "", 0
        name = data.get("name")
        if isinstance(name, str):
            name = name.encode("utf-8", errors="replace").decode("utf-8")
        return {
            "enabled": data.get("enabled") is True,
            "script": script,
            "name": name.strip() if isinstance(name, str) and name.strip() else DEFAULT_NAME,
            "bytes": size,
            "delay": _number(data.get("delay"), 3, 0, 120),
            "wait_for": _number(data.get("wait_for"), 90, 5, 600),
            "timeout": _number(data.get("timeout"), 30, 1, 120),
            "capture": bool(data.get("capture", True)),
        }

    def save(self, body: dict) -> dict:
        with self._lock:
            return self._save(body)

    def _save(self, body: dict) -> dict:
        current = self.config()
        script = body.get("script", current["script"])
        if not isinstance(script, str):
            raise ValueError("script must be text")
        if len(script.encode("utf-8")) > SCRIPT_LIMIT:
            raise ValueError("script must be at most 4 MiB")
        name = body.get("name", current["name"])
        if not isinstance(name, str):
            raise ValueError("name must be text")
        for key in ("enabled", "capture"):
            if key in body and type(body[key]) is not bool:
                raise ValueError(f"{key} must be a boolean")
        value = {
            "enabled": bool(body.get("enabled", current["enabled"])),
            "script": script,
            "name": name.strip() or current["name"] or DEFAULT_NAME,
            "delay": _number(body.get("delay"), current["delay"], 0, 120),
            "wait_for": _number(body.get("wait_for"), current["wait_for"], 5, 600),
            "timeout": _number(body.get("timeout"), current["timeout"], 1, 120),
            "capture": bool(body.get("capture", current["capture"])),
        }
        write_json(self.file, value)
        if not value["enabled"]:
            self.cancel()
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
        with self._lock:
            config = self.config()
            if self._closed or not config["enabled"] or not config["script"].strip():
                return None
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
                        "message": "Execution has already started and cannot be cancelled"}
            self._event.set()
            job["status"] = "cancelled"
            job["finished_at"] = _now()
            self._last = dict(job)
            return {"ok": True, "cancelled": True, "job": dict(job)}

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self.cancel()

    # ------------------------------------------------------------------- worker

    def _begin(self, job_id: str, event: threading.Event) -> bool:
        with self._lock:
            if (event.is_set() or not self._job or self._job["id"] != job_id
                    or self._job["status"] != "waiting"):
                return False
            self._job["status"] = "running"
            return True

    def _finish(self, job_id: str, status: str, **fields) -> None:
        with self._lock:
            if self._job and self._job["id"] == job_id:
                if self._job["status"] == "cancelled" and status != "cancelled":
                    return
                self._job.update({"status": status, "finished_at": _now(), **fields})
                self._last = dict(self._job)

    def _work(self, job_id: str, config: dict, place_id: int | None, port: int | None,
              event: threading.Event) -> None:
        try:
            self._run(job_id, config, place_id, port, event)
        except ServiceError as error:
            self._finish(job_id, "failed", error=error.code, message=error.message)
        except Exception as error:
            self._finish(job_id, "failed", error="autoexec_failed", message=str(error))

    def _run(self, job_id: str, config: dict, place_id: int | None, port: int | None,
             event: threading.Event) -> None:

        # a manual run has no place to wait for; execute as soon as it is asked
        deadline = time.monotonic() + config["wait_for"] if place_id else time.monotonic()
        while place_id and time.monotonic() < deadline:
            if event.is_set():
                return self._finish(job_id, "cancelled")
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

        if event.wait(config["delay"]):
            return self._finish(job_id, "cancelled")
        if place_id and config["delay"]:
            runtime = self._runtime(port)
            if (runtime.get("place_id") != place_id or not runtime.get("loaded")
                    or not runtime.get("connected")):
                return self._finish(job_id, "failed", error="place_changed",
                                    message="The client left the place before execution")
        # Claim dispatch under the same lock as cancel and rejoin. A retired
        # worker must never hand its script to opom after its replacement.
        if not self._begin(job_id, event):
            return

        payload = {"script": config["script"], "wait": 5,
                   "timeout": config["timeout"], "capture": config["capture"]}
        if port:
            payload["port"] = port
        deadline = time.monotonic() + config["timeout"] + 5
        status, value = self.service.call("POST", "/execute", payload,
                                          timeout=config["timeout"] + 30)
        remote = value.get("job") or {}
        while remote.get("status") == "submitted":
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._finish(job_id, "timeout", error="execution_timeout",
                                    job_id_remote=remote.get("id"),
                                    message="Execution is still pending; the script may still be running")
            time.sleep(min(0.25, remaining))
            status, value = self.service.call("GET", f"/jobs/{remote['id']}",
                                              timeout=min(10, remaining))
            remote = value.get("job") or {}
        fields = {"job_id_remote": remote.get("id") or value.get("job_id"),
                  "instance": remote.get("instance") or value.get("instance")}
        if remote.get("status") == "timed_out":
            return self._finish(job_id, "timeout", error="execution_timeout", **fields,
                                message="Execution timed out; the script may still be running")
        if status >= 400 or value.get("ok") is False or remote.get("status") == "failed":
            result = remote.get("result") or {}
            return self._finish(job_id, "failed", error=value.get("error", "execute_failed"),
                                message=result.get("error") or value.get("message", "Execution failed"),
                                **fields)
        self._finish(job_id, "done", **fields)

    def _runtime(self, port: int | None) -> dict:
        path = f"/runtime?port={port}" if port else "/runtime"
        try:
            _, value = self.service.call("GET", path, timeout=10)
        except ServiceError:
            return {}
        runtime = value.get("runtime")
        return runtime if isinstance(runtime, dict) else {}


def _number(value, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(number):
        return default
    return min(max(number, low), high)
