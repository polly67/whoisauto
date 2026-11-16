import json

import pytest

from whoisauto.desktop import DesktopError, WebRTCDesktop, input_event, tailscale_ip


def test_tailnet_config_needs_no_turn_credentials(paths, monkeypatch):
    monkeypatch.setenv("WHOISAUTO_TURN_URL", "broken-unused-value")
    config = WebRTCDesktop(paths).config()
    assert config["iceServers"] == []
    assert config["transport"] == "tailscale"
    assert "credential" not in json.dumps(config)


def test_tailnet_address_is_validated(monkeypatch):
    monkeypatch.setenv("WHOISAUTO_TAILSCALE_IP", "100.85.55.26")
    assert tailscale_ip() == "100.85.55.26"
    for value in ("127.0.0.1", "not-an-address", "192.168.1.1"):
        monkeypatch.setenv("WHOISAUTO_TAILSCALE_IP", value)
        with pytest.raises(DesktopError): tailscale_ip()


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
