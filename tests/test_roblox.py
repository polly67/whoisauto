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


def test_ticket_forbidden_is_not_reported_as_expired_cookie(monkeypatch):
    import urllib.error
    from email.message import Message

    def open(*args, **kwargs):
        raise urllib.error.HTTPError("https://auth.roblox.com", 403, "Forbidden", Message(),
                                     io.BytesIO(b'{"errors":[{"code":1,"message":"Denied"}]}'))

    monkeypatch.setattr(roblox.urllib.request, "urlopen", open)
    with pytest.raises(roblox.RobloxError) as error:
        roblox.authentication_ticket("test-cookie")
    assert error.value.code == "ticket_forbidden"
    assert error.value.details["stage"] == "launch_ticket"
    assert error.value.details["upstream_codes"] == [1]
    assert "refresh the account cookie" not in str(error.value)


def test_ticket_security_challenge_is_reported_without_secret_headers(monkeypatch):
    import urllib.error
    from email.message import Message

    headers = Message()
    headers["rblx-challenge-id"] = "private-challenge"

    def open(*args, **kwargs):
        raise urllib.error.HTTPError("https://auth.roblox.com", 403, "Forbidden", headers,
                                     io.BytesIO(b'{"errors":[{"code":0,"message":"Challenge required"}]}'))

    monkeypatch.setattr(roblox.urllib.request, "urlopen", open)
    with pytest.raises(roblox.RobloxError) as error:
        roblox.authentication_ticket("test-cookie")
    assert error.value.code == "login_verification_required"
    assert "private-challenge" not in json.dumps(error.value.details)
    assert "test-cookie" not in json.dumps(error.value.details)


def test_ticket_csrf_failure_is_distinct_from_cookie_failure(monkeypatch):
    import urllib.error
    from email.message import Message

    headers = Message()
    headers["x-csrf-token"] = "private-csrf"
    calls = []

    def open(*args, **kwargs):
        calls.append(1)
        raise urllib.error.HTTPError("https://auth.roblox.com", 403, "Forbidden", headers,
                                     io.BytesIO(b'{"errors":[{"code":0,"message":"Token Validation Failed"}]}'))

    monkeypatch.setattr(roblox.urllib.request, "urlopen", open)
    with pytest.raises(roblox.RobloxError) as error:
        roblox.authentication_ticket("test-cookie")
    assert error.value.code == "csrf_rejected"
    assert len(calls) == 2
    assert "private-csrf" not in json.dumps(error.value.details)
