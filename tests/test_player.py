import json
from types import SimpleNamespace

import pytest

from whoisauto import player


class Service:
    def __init__(self):
        self.calls = []
        self.instances = [{"pid": 10, "port": 8390}]

    def status(self):
        return {"running": True}

    def call(self, method, path, payload=None, timeout=40):
        self.calls.append((method, path, payload))
        if path == "/instances":
            return 200, {"instances": list(self.instances)}
        if path == "/instances/stop":
            stopped = self.instances.pop(0)
            return 200, {"ok": True, "stopped": stopped}
        raise AssertionError(path)


@pytest.fixture
def controls(paths, monkeypatch):
    monkeypatch.setattr(player.sys, "platform", "darwin")
    service = Service()
    value = player.PlayerControls(paths, service)
    monkeypatch.setattr(player.installer, "verify_app", lambda *args, **kwargs: {})
    monkeypatch.setattr(value, "_helper", lambda: paths.opom_state / "fake-helper")
    monkeypatch.setattr(value, "_stop_unregistered", lambda: [])
    monkeypatch.setattr(player.roblox, "authentication_ticket", lambda token: "test-ticket")
    monkeypatch.setattr(value, "_launch", lambda ticket: {"pid": 20, "port": 8391})
    return value


def test_new_account_stops_old_player_before_launch(controls, monkeypatch):
    def launch(ticket):
        assert not controls.service.instances
        assert ticket == "test-ticket"
        return {"pid": 20, "port": 8391}

    monkeypatch.setattr(controls, "_launch", launch)
    result = controls.restart("test-cookie")
    assert result["instance"]["pid"] == 20
    assert result["reused"] is False
    assert ("POST", "/instances/stop", {"port": 8390, "force": True}) in controls.service.calls
    assert "test-cookie" not in json.dumps(result)
    assert "test-ticket" not in json.dumps(result)


def test_failed_ticket_does_not_close_existing_player(controls, monkeypatch):
    def denied(token):
        raise player.roblox.RobloxError("unauthorized", "Session expired", 401)

    monkeypatch.setattr(player.roblox, "authentication_ticket", denied)
    with pytest.raises(player.PlayerError, match="Session expired"):
        controls.restart("test-cookie")
    assert controls.service.instances
    assert not any(path == "/instances/stop" for _, path, _ in controls.service.calls)


def test_open_reuses_player_started_with_saved_cookie(controls):
    controls.restart("test-cookie")
    controls.service.instances = [{"pid": 20, "port": 8391}]
    result = controls.open("test-cookie")
    assert result["reused"] is True
    assert result["instance"]["pid"] == 20


def test_open_does_not_reuse_unknown_account_session(controls):
    result = controls.open("test-cookie")
    assert result["reused"] is False
    assert result["instance"]["pid"] == 20


def test_changed_cookie_requires_fresh_login(controls):
    controls.restart("old-cookie")
    controls.service.instances = [{"pid": 20, "port": 8391}]
    assert controls.open("new-cookie")["reused"] is False


def test_stop_closes_controlled_players_and_is_idle_safe(controls):
    assert controls.stop()["stopped"][0]["pid"] == 10
    assert controls.stop()["stopped"] == []


def test_failed_stop_never_launches_replacement(controls, monkeypatch):
    original = controls.service.call

    def call(method, path, payload=None, timeout=40):
        if path == "/instances/stop":
            return 504, {"error": "stop_timeout", "message": "Player is still running"}
        return original(method, path, payload, timeout)

    monkeypatch.setattr(controls.service, "call", call)
    with pytest.raises(player.PlayerError, match="still running"):
        controls.restart("test-cookie")


def test_native_launcher_receives_ticket_over_stdin(controls, paths, monkeypatch):
    helper = paths.opom_state / "whoisauto-open-roblox"
    helper.write_bytes(b"fake helper")
    monkeypatch.setattr(controls, "_helper", lambda: helper)
    seen = []

    def run(command, **options):
        seen.append((command, options))
        controls.service.instances = [{"pid": 20, "port": 8391}]
        return SimpleNamespace(returncode=0, stdout='{"pid":20}', stderr="")

    monkeypatch.setattr(player.subprocess, "run", run)
    result = player.PlayerControls._launch(controls, "test-ticket")
    command, options = seen[0]
    assert result["pid"] == 20
    assert "test-ticket" not in repr(command)
    assert "launchmode:app" in options["input"]
    assert "gameinfo:test-ticket" in options["input"]


def test_launcher_does_not_return_ticket_in_error(controls, monkeypatch):
    monkeypatch.setattr(controls, "_helper", lambda: controls.paths.opom_state / "helper")
    monkeypatch.setattr(player.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=1, stdout="", stderr="native failure for test-ticket"))
    with pytest.raises(player.PlayerError) as error:
        player.PlayerControls._launch(controls, "test-ticket")
    assert "test-ticket" not in str(error.value)


def test_restart_stops_player_without_execution_port(controls, monkeypatch):
    controls.service.instances = []
    stopped = []
    monkeypatch.setattr(controls, "_stop_unregistered", lambda: stopped.append(10) or [10])
    result = controls.restart("test-cookie")
    assert stopped == [10]
    assert result["stopped"] == [{"pid": 10, "port": None}]


def test_opening_home_window_without_port_is_not_login_failure(controls, monkeypatch):
    monkeypatch.setattr(player.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout='{"pid":20}', stderr=""))
    controls.service.instances = []
    clock = iter([0, 0, 6])
    monkeypatch.setattr(player.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(player.time, "sleep", lambda delay: None)
    assert player.PlayerControls._launch(controls, "test-ticket") == {"pid": 20, "port": None}


def test_manual_sign_in_does_not_request_or_transfer_cookie(controls, monkeypatch):
    def ticket(token):
        raise AssertionError("Manual sign-in must not request a cookie ticket")

    monkeypatch.setattr(player.roblox, "authentication_ticket", ticket)
    requested = []
    monkeypatch.setattr(controls, "_launch", lambda ticket: requested.append(ticket) or {"pid": 20, "port": None})
    assert controls.open_login()["manual_sign_in"] is True
    assert requested == [None]
    assert controls.manual is True
    assert controls.restart("")["manual_sign_in"] is True
    assert requested == [None, None]


def test_cookie_sign_in_is_explicit_after_manual_login(controls, monkeypatch):
    controls.open_login()
    requested = []
    monkeypatch.setattr(player.roblox, "authentication_ticket", lambda token: requested.append(token) or "ticket")
    controls.sign_in("new-cookie")
    assert requested == ["new-cookie"]
    assert controls.manual is False


def test_native_manual_login_uses_no_ticket(controls, monkeypatch):
    commands = []

    def run(command, **options):
        commands.append((command, options))
        controls.service.instances = [{"pid": 20, "port": 8391}]
        return SimpleNamespace(returncode=0, stdout='{"pid":20}', stderr="")

    monkeypatch.setattr(player.subprocess, "run", run)
    player.PlayerControls._launch(controls, None)
    assert commands[0][0][1] == "login"
    assert commands[0][1]["input"] == ""
