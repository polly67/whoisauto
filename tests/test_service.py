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
