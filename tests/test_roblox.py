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
