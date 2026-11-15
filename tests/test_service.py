import sys

import pytest

from whoisauto.config import write_json
from whoisauto.service import OpomService, ServiceError


def test_running_uses_recorded_port(paths, fake_opom):
    write_json(paths.port_file, {"port": fake_opom.server_address[1], "pid": 4242})
    service = OpomService(paths)
    found = service.running()
    assert found is not None
    port, health = found
    assert port == fake_opom.server_address[1]
    assert health["pid"] == 4242


def test_resolve_port_reuses_running_instance(paths, fake_opom):
    write_json(paths.port_file, {"port": fake_opom.server_address[1], "pid": 4242})
    assert OpomService(paths).resolve_port() == fake_opom.server_address[1]


def test_stop_is_a_noop_when_down(paths):
    assert OpomService(paths).stop()["stopped"] is False


def test_call_requires_running_api(paths):
    with pytest.raises(ServiceError) as error:
        OpomService(paths).call("GET", "/instances")
    assert error.value.code == "api_down"


def test_call_proxies_json(paths, fake_opom):
    write_json(paths.port_file, {"port": fake_opom.server_address[1], "pid": 4242})
    status, payload = OpomService(paths).call("POST", "/join", {"place_id": 42})
    assert status == 200
    assert payload["joined"] is True
    assert ("POST", "/join", {"place_id": 42}) in fake_opom.calls


@pytest.mark.skipif(sys.platform == "darwin", reason="platform guard only fires off macOS")
def test_start_refuses_off_macos(paths):
    with pytest.raises(ServiceError) as error:
        OpomService(paths).start()
    assert error.value.code == "unsupported_platform"


def test_status_reports_preferred_port(paths):
    status = OpomService(paths).status()
    assert status["running"] is False
    assert status["preferred"] == 8765
    assert "preferred_free" in status


@pytest.mark.parametrize("record", [[], {"port": -1}, {"port": True}, {"port": 99999}])
def test_invalid_port_record_is_ignored(paths, monkeypatch, record):
    write_json(paths.port_file, record)
    probes = []
    service = OpomService(paths, 65535)
    monkeypatch.setattr(service, "health", lambda port: probes.append(port))
    assert service.running() is None
    assert probes == [65535]


def test_discovery_ignores_non_json_server(paths, monkeypatch):
    service = OpomService(paths)
    monkeypatch.setattr(service, "_request", lambda *args: (_ for _ in ()).throw(ValueError("bad JSON")))
    assert service.health(8765) is None


def test_shutdown_reports_upstream_failure(paths, fake_opom, monkeypatch):
    service = OpomService(paths)
    monkeypatch.setattr(service, "running", lambda: (8765, {"pid": 42}))
    monkeypatch.setattr(service, "_request", lambda *args: (403, {"message": "Access denied"}))
    with pytest.raises(ServiceError, match="Access denied") as error:
        service.stop()
    assert error.value.code == "shutdown_failed"


def test_start_waits_for_its_own_child_pid(paths, monkeypatch):
    from whoisauto import service as module
    from whoisauto.config import read_json
    from types import SimpleNamespace

    service = OpomService(paths)
    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(service, "running", lambda: None)
    monkeypatch.setattr(module, "find_free_port", lambda *args: 18765)
    child = SimpleNamespace(pid=4321, poll=lambda: None)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: child)
    monkeypatch.setattr(service, "health", lambda *args, **kwargs: {"service": "opom", "pid": 4321})
    result = service.start()
    assert result["pid"] == 4321
    assert read_json(paths.port_file)["pid"] == 4321


def test_start_reaps_child_when_another_pid_answers(paths, monkeypatch):
    from whoisauto import service as module
    from types import SimpleNamespace

    service = OpomService(paths)
    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(service, "running", lambda: None)
    monkeypatch.setattr(module, "find_free_port", lambda *args: 18765)
    calls = []
    child = SimpleNamespace(pid=4321, poll=lambda: None,
                            terminate=lambda: calls.append("terminate"),
                            wait=lambda **kwargs: calls.append("wait"))
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: child)
    monkeypatch.setattr(service, "health", lambda *args, **kwargs: {"service": "opom", "pid": 9999})
    clock = iter([0, 0, 21])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(module.time, "sleep", lambda *args: None)
    with pytest.raises(ServiceError) as error:
        service.start()
    assert error.value.code == "startup_timeout"
    assert calls == ["terminate", "wait"]
