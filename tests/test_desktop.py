import json

import pytest

from whoisauto.desktop import DesktopError, WebRTCDesktop, ice_servers, input_event, turn_uri


def test_turn_credentials_are_configured_as_a_set(monkeypatch):
    for name in ("WHOISAUTO_TURN_URL", "WHOISAUTO_TURN_USERNAME", "WHOISAUTO_TURN_CREDENTIAL"):
        monkeypatch.delenv(name, raising=False)
    assert len(ice_servers()) == 1
    monkeypatch.setenv("WHOISAUTO_TURN_URL", "turn:relay.example:3478?transport=udp")
    with pytest.raises(DesktopError):
        ice_servers()
    monkeypatch.setenv("WHOISAUTO_TURN_USERNAME", "viewer")
    monkeypatch.setenv("WHOISAUTO_TURN_CREDENTIAL", "test/password")
    server = ice_servers()[1]
    assert turn_uri(server) == "turn://viewer:test%2Fpassword@relay.example:3478?transport=udp"


@pytest.mark.parametrize("event", [
    {"type": "move", "x": -1, "y": 0},
    {"type": "move", "x": float("nan"), "y": 0},
    {"type": "move", "x": True, "y": 0},
    {"type": "key", "code": "KeyW", "down": "yes"},
    {"type": "scroll", "dy": 10000},
    {"type": "exec", "command": "anything"},
])
def test_input_rejects_invalid_or_unbounded_events(event):
    assert input_event(json.dumps(event)) is None


def test_input_accepts_normalized_pointer_and_key():
    assert input_event('{"type":"button","x":0.5,"y":0.4,"button":0,"down":true}')
    assert input_event('{"type":"key","code":"KeyW","down":true}')


def test_invalid_offer_does_not_load_native_runtime(paths):
    desktop = WebRTCDesktop(paths)
    with pytest.raises(DesktopError):
        desktop.offer({"type": "offer", "sdp": "not an offer"})
    assert desktop._glib is None
