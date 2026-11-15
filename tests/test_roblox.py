import io
import json

import pytest

from whoisauto import roblox


@pytest.mark.parametrize("value", [
    'abc123', '"abc123"', "'abc123'", '.ROBLOSECURITY=abc123',
    '.ROBLOSECURITY="abc123"', "ROBLOSECURITY='abc123'",
])
def test_normalize_pasted_cookie(value):
    assert roblox.normalize_token(value) == "abc123"


@pytest.mark.parametrize("value", [None, 123, "", "abc def", "abc\r\ndef", "abc\x00def"])
def test_invalid_cookie_is_rejected(value):
    with pytest.raises(roblox.RobloxError) as error:
        roblox.normalize_token(value)
    assert error.value.code == "invalid_token"


def test_non_object_roblox_response_has_clear_error(monkeypatch):
    monkeypatch.setattr(roblox.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(json.dumps([]).encode()))
    with pytest.raises(roblox.RobloxError) as error:
        roblox.place_details(42)
    assert error.value.code == "roblox_bad_response"


def test_launch_ticket_retries_csrf_challenge(monkeypatch):
    from email.message import Message
    import urllib.error

    headers = Message()
    headers["x-csrf-token"] = "test-csrf"
    challenge = urllib.error.HTTPError("https://auth.roblox.com", 403, "Forbidden", headers, io.BytesIO(b"{}"))
    requests = []

    class Response(io.BytesIO):
        status = 200
        headers = {"rbx-authentication-ticket": "test-ticket"}

    def open(request, **kwargs):
        requests.append(request)
        if len(requests) == 1:
            raise challenge
        return Response(b"{}")

    monkeypatch.setattr(roblox.urllib.request, "urlopen", open)
    assert roblox.authentication_ticket("test-cookie") == "test-ticket"
    assert requests[0].get_method() == "POST"
    assert requests[1].get_header("X-csrf-token") == "test-csrf"
    assert requests[1].get_header("Cookie") == ".ROBLOSECURITY=test-cookie"


def test_launch_ticket_failure_does_not_expose_cookie(monkeypatch):
    import urllib.error

    def open(*args, **kwargs):
        raise urllib.error.URLError("request rejected: test-cookie")

    monkeypatch.setattr(roblox.urllib.request, "urlopen", open)
    with pytest.raises(roblox.RobloxError) as error:
        roblox.authentication_ticket("test-cookie")
    assert error.value.code == "roblox_unreachable"
    assert "test-cookie" not in str(error.value)
