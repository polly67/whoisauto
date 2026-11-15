import time
import threading

import pytest

from whoisauto.autoexec import AutoExec
from whoisauto.config import read_json, write_json
from whoisauto.service import OpomService


@pytest.fixture
def autoexec(paths, fake_opom):
    write_json(paths.port_file, {"port": fake_opom.server_address[1], "pid": 4242})
    return AutoExec(paths, OpomService(paths))


@pytest.fixture
def workers(monkeypatch):
    original = threading.Thread
    threads = []

    def thread(*args, **kwargs):
        value = original(*args, **kwargs)
        threads.append(value)
        return value

    monkeypatch.setattr("whoisauto.autoexec.threading.Thread", thread)
    yield threads
    for value in threads:
        value.join(timeout=2)


def _wait(job_id, autoexec, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = autoexec.job()
        if job and job["id"] == job_id and job["status"] not in ("waiting", "running"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not settle: {autoexec.job()}")


def test_config_round_trips_and_clamps(autoexec):
    saved = autoexec.save({"enabled": True, "script": "print('hi')", "name": "main.luau",
                           "delay": -4, "wait_for": 9999, "timeout": 0})
    assert saved["config"]["enabled"] is True
    assert saved["config"]["delay"] == 0
    assert saved["config"]["wait_for"] == 600
    assert saved["config"]["timeout"] == 1
    assert saved["config"]["bytes"] == len("print('hi')")


def test_snapshot_can_hide_the_script(autoexec):
    autoexec.save({"enabled": True, "script": "print('secret')"})
    assert "script" not in autoexec.snapshot(include_script=False)["config"]


def test_disabled_does_not_queue(autoexec):
    autoexec.save({"enabled": False, "script": "print('hi')"})
    assert autoexec.trigger("join", 42, 8390) is None


def test_empty_script_does_not_queue(autoexec):
    autoexec.save({"enabled": True, "script": "   "})
    assert autoexec.trigger("join", 42, 8390) is None


def test_runs_the_stored_script_once_the_place_is_loaded(autoexec, fake_opom):
    autoexec.save({"enabled": True, "script": "print('auto')", "name": "auto.luau", "delay": 0})
    job = autoexec.trigger("join", 42, 8390)

    settled = _wait(job["id"], autoexec)
    assert settled["status"] == "done"
    assert settled["script"] == "auto.luau"

    executes = [entry for entry in fake_opom.calls if entry[1] == "/execute"]
    assert len(executes) == 1
    assert executes[0][2]["script"] == "print('auto')"
    assert executes[0][2]["port"] == 8390


def test_a_rejoin_replaces_the_job_still_waiting(autoexec):
    autoexec.save({"enabled": True, "script": "print('auto')", "delay": 30})
    first = autoexec.trigger("join", 42, 8390)
    second = autoexec.trigger("rejoin", 42, 8390)
    assert second["id"] != first["id"]
    assert second["trigger"] == "rejoin"
    assert autoexec.job()["id"] == second["id"]


def test_a_running_job_is_not_replaced(autoexec, monkeypatch):
    import threading

    autoexec.save({"enabled": True, "script": "print('auto')", "delay": 0})
    started = threading.Event()
    release = threading.Event()
    original = autoexec.service.call

    def blocking(method, path, payload=None, timeout=40):
        if path == "/execute":
            started.set()
            release.wait(5)
            return 200, {"ok": True}
        return original(method, path, payload, timeout)

    monkeypatch.setattr(autoexec.service, "call", blocking)
    try:
        first = autoexec.trigger("join", 42, 8390)
        assert started.wait(5)
        second = autoexec.trigger("rejoin", 42, 8390)
        assert second["id"] == first["id"]
    finally:
        release.set()
        _wait(first["id"], autoexec)


def test_cancel_stops_a_job_that_has_not_fired(autoexec):
    autoexec.save({"enabled": True, "script": "print('auto')", "delay": 30})
    job = autoexec.trigger("join", 42, 8390)

    result = autoexec.cancel()
    assert result["cancelled"] is True
    assert result["job"]["status"] == "cancelled"
    assert autoexec.job()["status"] == "cancelled"


def test_cancel_reports_nothing_when_idle(autoexec):
    assert autoexec.cancel()["cancelled"] is False


def test_cancel_refuses_once_the_script_is_dispatched(autoexec, monkeypatch):
    import threading

    autoexec.save({"enabled": True, "script": "print('auto')", "delay": 0})
    started = threading.Event()
    release = threading.Event()
    original = autoexec.service.call

    def blocking(method, path, payload=None, timeout=40):
        if path == "/execute":
            started.set()
            release.wait(5)
            return 200, {"ok": True, "job_id": "b" * 32}
        return original(method, path, payload, timeout)

    monkeypatch.setattr(autoexec.service, "call", blocking)
    try:
        job = autoexec.trigger("join", 42, 8390)
        assert started.wait(5), "execute was never dispatched"
        result = autoexec.cancel()
        assert result["cancelled"] is False
        assert "already" in result["message"]
    finally:
        release.set()
        _wait(job["id"], autoexec)


def test_manual_run_skips_the_place_wait(autoexec, fake_opom):
    autoexec.save({"enabled": True, "script": "print('manual')", "delay": 0})
    job = autoexec.trigger("manual", None, 8390)
    settled = _wait(job["id"], autoexec)
    assert settled["status"] == "done"
    assert any(entry[2]["script"] == "print('manual')"
               for entry in fake_opom.calls if entry[1] == "/execute")


def test_state_file_is_json(autoexec, paths):
    autoexec.save({"enabled": True, "script": "print('hi')"})
    assert read_json(paths.state / "autoexec.json")["script"] == "print('hi')"


def test_cancel_while_waiting_for_place_never_executes(autoexec, monkeypatch, workers):
    reached = threading.Event()
    release = threading.Event()
    executed = []

    def call(method, path, payload=None, timeout=40):
        if path.startswith("/runtime"):
            reached.set()
            release.wait(2)
            return 200, {"runtime": {"place_id": 42, "loaded": True, "connected": True}}
        executed.append(path)
        return 200, {"ok": True}

    monkeypatch.setattr(autoexec.service, "call", call)
    autoexec.save({"enabled": True, "script": "print('auto')", "delay": 0})
    job = autoexec.trigger("join", 42, 8390)
    try:
        assert reached.wait(2)
        assert autoexec.cancel()["cancelled"] is True
    finally:
        release.set()
    assert _wait(job["id"], autoexec)["status"] == "cancelled"
    for worker in workers:
        worker.join(timeout=2)
        assert not worker.is_alive()
    assert not executed


def test_rejoin_retires_worker_waiting_for_runtime(autoexec, monkeypatch, workers):
    reached = threading.Event()
    release = threading.Event()
    executed = []

    def call(method, path, payload=None, timeout=40):
        if path == "/runtime?port=8390":
            reached.set()
            release.wait(2)
            return 200, {"runtime": {"place_id": 42, "loaded": True, "connected": True}}
        if path.startswith("/runtime"):
            return 200, {"runtime": {"place_id": 43, "loaded": True, "connected": True}}
        executed.append(payload["port"])
        return 200, {"ok": True}

    monkeypatch.setattr(autoexec.service, "call", call)
    autoexec.save({"enabled": True, "script": "print('auto')", "delay": 0})
    first = autoexec.trigger("join", 42, 8390)
    try:
        assert reached.wait(2)
        second = autoexec.trigger("rejoin", 43, 8391)
        assert second["id"] != first["id"]
    finally:
        release.set()
    assert _wait(second["id"], autoexec)["status"] == "done"
    for worker in workers:
        worker.join(timeout=2)
        assert not worker.is_alive()
    assert executed == [8391]


def test_autoexec_reports_remote_execution_failure(autoexec, monkeypatch):
    monkeypatch.setattr(autoexec.service, "call", lambda *args, **kwargs: (
        200, {"ok": False, "job": {"id": "a" * 32, "status": "failed",
                                    "result": {"error": "script crashed"}}}))
    autoexec.save({"enabled": True, "script": "error('oops')", "delay": 0})
    job = autoexec.trigger("manual", None)
    settled = _wait(job["id"], autoexec)
    assert settled["status"] == "failed"
    assert settled["message"] == "script crashed"
    assert settled["job_id_remote"] == "a" * 32


def test_autoexec_waits_for_submitted_remote_job(autoexec, monkeypatch):
    calls = []

    def call(method, path, payload=None, timeout=40):
        calls.append(path)
        status = "submitted" if path == "/execute" else "completed"
        return 202 if status == "submitted" else 200, {
            "ok": True, "job": {"id": "a" * 32, "status": status}}

    monkeypatch.setattr(autoexec.service, "call", call)
    autoexec.save({"enabled": True, "script": "print('auto')", "delay": 0})
    job = autoexec.trigger("manual", None)
    assert _wait(job["id"], autoexec)["status"] == "done"
    assert "/jobs/" + "a" * 32 in calls


@pytest.mark.parametrize("value", [[], {"script": 12, "name": []}])
def test_corrupt_autoexec_state_is_safe(autoexec, paths, value):
    write_json(paths.state / "autoexec.json", value)
    assert autoexec.snapshot()["config"]["script"] == ""


def test_autoexec_rejects_invalid_name(autoexec):
    with pytest.raises(ValueError, match="name must be text"):
        autoexec.save({"name": 123})


def test_nonfinite_config_uses_defaults(autoexec):
    result = autoexec.save({"delay": float("nan"), "timeout": float("inf")})
    assert result["config"]["delay"] == 3
    assert result["config"]["timeout"] == 30


def test_disable_cancels_a_queued_script(autoexec):
    autoexec.save({"enabled": True, "script": "print('auto')", "delay": 30})
    autoexec.trigger("manual", None)
    result = autoexec.save({"enabled": False})
    assert result["job"]["status"] == "cancelled"
    assert result["last"]["status"] == "cancelled"


def test_autoexec_reports_remote_timeout(autoexec, monkeypatch):
    monkeypatch.setattr(autoexec.service, "call", lambda *args, **kwargs: (
        504, {"ok": False, "job": {"id": "a" * 32, "status": "timed_out"}}))
    autoexec.save({"enabled": True, "script": "print('auto')", "delay": 0})
    job = autoexec.trigger("manual", None)
    settled = _wait(job["id"], autoexec)
    assert settled["status"] == "timeout"
    assert "may still be running" in settled["message"]


def test_leaving_place_during_grace_period_does_not_execute(autoexec, monkeypatch):
    calls = []

    def call(method, path, payload=None, timeout=40):
        calls.append(path)
        return 200, {"runtime": {"place_id": 42 if len(calls) == 1 else 43,
                                  "loaded": True, "connected": True}}

    monkeypatch.setattr(autoexec.service, "call", call)
    autoexec.save({"enabled": True, "script": "print('auto')", "delay": 0.01})
    job = autoexec.trigger("join", 42, 8390)
    settled = _wait(job["id"], autoexec)
    assert settled["error"] == "place_changed"
    assert "/execute" not in calls


def test_closed_worker_cannot_queue_another_script(autoexec):
    autoexec.save({"enabled": True, "script": "print('auto')", "delay": 30})
    autoexec.trigger("manual", None)
    autoexec.close()
    assert autoexec.job()["status"] == "cancelled"
    assert autoexec.trigger("join", 42) is None
