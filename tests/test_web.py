import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from whoisauto import roblox
from whoisauto.config import write_json
from whoisauto.service import OpomService
from whoisauto.web import Panel, Server, load_account


@pytest.fixture
def panel(paths, fake_opom):
    write_json(paths.port_file, {"port": fake_opom.server_address[1], "pid": 4242})
    server = Server(("127.0.0.1", 0), Panel(paths, OpomService(paths)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def call(base, method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        with error:
            return error.code, json.load(error)


def fetch_text(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as response:
        return response.status, response.read().decode()


def test_state_reports_install_and_service(panel, fake_opom):
    status, payload = call(panel, "GET", "/api/state")
    assert status == 200
    assert payload["service"]["running"] is True
    assert payload["service"]["port"] == fake_opom.server_address[1]
    assert payload["install"]["editor"]["available"] is False
    assert payload["account"]["signed_in"] is False


def test_index_is_served(panel):
    status, body = fetch_text(panel, "/")
    assert status == 200
    assert "whoisauto" in body
    assert "/static/app.js" in body


def test_unknown_route_returns_json_404(panel):
    status, payload = call(panel, "GET", "/api/nope")
    assert status == 404
    assert payload["error"] == "not_found"


def test_account_roundtrip(panel, paths, monkeypatch):
    monkeypatch.setattr("whoisauto.web.roblox.validate_token",
                        lambda token: {"id": 7, "name": "tester", "display_name": "Tester",
                                       "masked": roblox.mask(token)})
    status, payload = call(panel, "POST", "/api/account", {"token": ".ROBLOSECURITY=abc123", "launch": False})
    assert status == 200
    assert payload["account"]["user"]["name"] == "tester"

    record = load_account(paths)
    assert record["token"] == "abc123"
    assert oct(paths.account_file.stat().st_mode & 0o777) == oct(0o600)

    status, listed = call(panel, "GET", "/api/account")
    assert listed["account"]["signed_in"] is True
    assert "abc123" not in json.dumps(listed)

    status, cleared = call(panel, "DELETE", "/api/account")
    assert cleared["account"]["signed_in"] is False
    assert load_account(paths) is None


def test_account_rejects_bad_token(panel, monkeypatch):
    def reject(token):
        raise roblox.RobloxError("unauthorized", "Roblox rejected the token", 401)

    monkeypatch.setattr("whoisauto.web.roblox.validate_token", reject)
    status, payload = call(panel, "POST", "/api/account", {"token": "bad"})
    assert status == 401
    assert payload["error"] == "unauthorized"


def test_join_proxies_place_id(panel, fake_opom):
    status, payload = call(panel, "POST", "/api/join", {"place_id": 2753915549, "wait": 5})
    assert status == 200
    assert payload["joined"] is True
    assert any(entry[0] == "POST" and entry[1] == "/join" and entry[2]["place_id"] == 2753915549
               for entry in fake_opom.calls)


def test_join_validates_place_id(panel):
    status, payload = call(panel, "POST", "/api/join", {"place_id": "not-a-number"})
    assert status == 400
    assert payload["error"] == "invalid_place_id"


def test_execute_returns_job(panel):
    status, payload = call(panel, "POST", "/api/execute", {"script": "print('hi')", "check": True})
    assert status == 200
    assert payload["job"]["status"] == "completed"


def test_execute_rejects_empty_script(panel):
    status, payload = call(panel, "POST", "/api/execute", {"script": "   "})
    assert status == 400
    assert payload["error"] == "empty_script"


def test_players_combines_instances_and_place(panel, monkeypatch):
    monkeypatch.setattr("whoisauto.web.roblox.place_details",
                        lambda place_id: {"place_id": place_id, "name": "Test Place"})
    monkeypatch.setattr("whoisauto.web.roblox.public_servers",
                        lambda place_id, token=None: {"servers": [{"id": "s1", "playing": 3,
                                                                   "max_players": 10}],
                                                      "total_players": 3})
    status, payload = call(panel, "GET", "/api/players?place_id=42")
    assert status == 200
    assert payload["count"] == 1
    assert payload["instances"][0]["runtime"]["player"]["name"] == "tester"
    assert payload["place"]["name"] == "Test Place"
    assert payload["servers"]["total_players"] == 3


def test_service_stop_route(panel, fake_opom):
    status, payload = call(panel, "POST", "/api/service/stop")
    assert status == 200
    assert payload["stopped"] is True
    assert any(entry[1] == "/shutdown" for entry in fake_opom.calls)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@pytest.fixture
def secure_panel(paths, fake_opom):
    write_json(paths.port_file, {"port": fake_opom.server_address[1], "pid": 4242})
    server = Server(("127.0.0.1", 0), Panel(paths, OpomService(paths)), access_token="s3cret")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_panel_rejects_unauthenticated_api(secure_panel):
    status, payload = call(secure_panel, "GET", "/api/state")
    assert status == 401
    assert payload["error"] == "unauthorized"


def test_panel_accepts_bearer(secure_panel):
    request = urllib.request.Request(secure_panel + "/api/state",
                                     headers={"Authorization": "Bearer s3cret"})
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status == 200


def test_panel_accepts_wrong_token_as_rejection(secure_panel):
    request = urllib.request.Request(secure_panel + "/api/state",
                                     headers={"Authorization": "Bearer nope"})
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=10)
    assert error.value.code == 401


def test_token_link_sets_cookie(secure_panel):
    opener = urllib.request.build_opener(_NoRedirect)
    with pytest.raises(urllib.error.HTTPError) as error:
        opener.open(secure_panel + "/?token=s3cret", timeout=10)
    response = error.value
    assert response.code == 302
    assert response.headers["Location"] == "/"
    assert "whoisauto_token=s3cret" in response.headers["Set-Cookie"]


def test_cookie_authorises_api(secure_panel):
    request = urllib.request.Request(secure_panel + "/api/state",
                                     headers={"Cookie": "whoisauto_token=s3cret"})
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status == 200


def test_static_assets_stay_public(secure_panel):
    status, body = fetch_text(secure_panel, "/static/style.css")
    assert status == 200
    assert "--bg" in body


def test_state_reports_the_compiler(panel):
    status, payload = call(panel, "GET", "/api/state")
    assert status == 200
    assert payload["compiler"]["available"] is False
    assert payload["compiler"]["path"].endswith("opom-compiler")


def test_compiler_build_route_reports_failure(panel, monkeypatch, tmp_path):
    from whoisauto import config

    # opom_dir is a property, so patching the module constant reaches the
    # already-running panel
    monkeypatch.setattr(config, "OPOM_DIR", tmp_path / "absent")
    status, payload = call(panel, "POST", "/api/compiler/build")
    assert status == 502
    assert payload["error"] == "compiler_build_failed"


def test_autoexec_route_saves_and_returns_the_script(panel):
    status, payload = call(panel, "POST", "/api/autoexec",
                           {"enabled": True, "script": "print('auto')", "name": "auto.luau"})
    assert status == 200
    assert payload["config"]["script"] == "print('auto')"
    assert payload["config"]["enabled"] is True

    status, payload = call(panel, "GET", "/api/autoexec")
    assert payload["config"]["name"] == "auto.luau"


def test_state_hides_the_autoexec_script(panel):
    call(panel, "POST", "/api/autoexec", {"enabled": True, "script": "print('secret')"})
    status, payload = call(panel, "GET", "/api/state")
    assert status == 200
    assert "script" not in payload["autoexec"]["config"]
    assert payload["autoexec"]["config"]["bytes"] == len("print('secret')")


def test_join_queues_the_autoexec_script(panel, fake_opom):
    call(panel, "POST", "/api/autoexec",
         {"enabled": True, "script": "print('auto')", "delay": 0})
    status, payload = call(panel, "POST", "/api/join", {"place_id": 42})
    assert status == 200
    assert payload["autoexec"]["trigger"] == "join"

    deadline = time.monotonic() + 5
    last = None
    while time.monotonic() < deadline:
        _, snapshot = call(panel, "GET", "/api/autoexec")
        last = snapshot.get("last")
        if last and last["status"] in ("done", "failed", "timeout"):
            break
        time.sleep(0.05)
    assert last["status"] == "done"
    assert any(entry[2]["script"] == "print('auto')"
               for entry in fake_opom.calls if entry[1] == "/execute")


def test_second_join_is_labelled_rejoin(panel):
    call(panel, "POST", "/api/autoexec",
         {"enabled": True, "script": "print('auto')", "delay": 30})
    call(panel, "POST", "/api/join", {"place_id": 42})
    status, payload = call(panel, "POST", "/api/join", {"place_id": 42})
    assert payload["autoexec"]["trigger"] == "rejoin"
    call(panel, "POST", "/api/autoexec/cancel")


def test_autoexec_run_needs_a_saved_script(panel):
    status, payload = call(panel, "POST", "/api/autoexec/run")
    assert status == 400
    assert payload["error"] == "autoexec_disabled"


def test_autoexec_cancel_is_a_noop_when_idle(panel):
    status, payload = call(panel, "POST", "/api/autoexec/cancel")
    assert status == 200
    assert payload["cancelled"] is False


@pytest.mark.parametrize("method,path,body,code", [
    ("GET", "/api/players?place_id=oops", None, "invalid_place_id"),
    ("POST", "/api/join", {"place_id": 0}, "invalid_place_id"),
    ("POST", "/api/join", {"place_id": 1.5}, "invalid_place_id"),
    ("POST", "/api/join", {"place_id": True}, "invalid_place_id"),
    ("POST", "/api/join", {"place_id": 42, "wait": "slow"}, "invalid_wait"),
    ("POST", "/api/execute", {"script": 123}, "invalid_script"),
    ("POST", "/api/execute", {"script": "print('hi')", "timeout": "slow"}, "invalid_timeout"),
    ("POST", "/api/execute", {"script": "print('hi')", "port": "oops"}, "invalid_port"),
    ("POST", "/api/account", {"token": 123}, "invalid_token"),
    ("POST", "/api/autoexec", {"name": 123}, "invalid_autoexec"),
])
def test_invalid_input_returns_client_error(panel, method, path, body, code):
    status, payload = call(panel, method, path, body)
    assert status == 400
    assert payload["error"] == code


def test_players_propagates_upstream_error(paths, monkeypatch):
    service = OpomService(paths)
    monkeypatch.setattr(service, "call", lambda *args, **kwargs: (
        403, {"error": "unauthorized", "message": "Access denied"}))
    from whoisauto.web import PanelError
    with pytest.raises(PanelError, match="Access denied"):
        Panel(paths, service).players()


def test_unicode_access_token_is_rejected_cleanly(secure_panel):
    status, payload = call(secure_panel, "GET", "/?token=%C3%A9")
    assert status == 401
    assert payload["error"] == "unauthorized"


def test_panel_log_redacts_access_token(secure_panel, capsys):
    opener = urllib.request.build_opener(_NoRedirect)
    with pytest.raises(urllib.error.HTTPError):
        opener.open(secure_panel + "/?token=s3cret", timeout=10)
    assert "s3cret" not in capsys.readouterr().err


@pytest.mark.parametrize("body", [{"script": "\ud800"}, {"script": "print('hi')", "name": "\ud800"}])
def test_autoexec_rejects_invalid_unicode(panel, body):
    status, payload = call(panel, "POST", "/api/autoexec", body)
    assert status == 400
    assert payload["error"] == "invalid_autoexec"


def test_run_now_targets_the_last_joined_instance(paths, monkeypatch):
    service = OpomService(paths)
    monkeypatch.setattr(service, "call", lambda *args, **kwargs: (
        200, {"ok": True, "instance": {"port": 8391}}))
    panel = Panel(paths, service)
    calls = []

    def trigger(reason, place_id, port=None):
        calls.append((reason, place_id, port))
        return {"id": "test"}

    monkeypatch.setattr(panel.autoexec, "trigger", trigger)
    panel.join({"place_id": 42, "port": 8391})
    panel.run_autoexec()
    assert calls == [("join", 42, 8391), ("manual", 42, 8391)]


def test_saving_cookie_restarts_player_after_verification(paths, monkeypatch):
    panel = Panel(paths)
    events = []

    def validate(token):
        events.append("verify")
        return {"id": 7, "name": "tester"}

    def restart(token):
        events.append("restart")
        assert load_account(paths)["token"] == token
        return {"ok": True, "instance": {"port": 8390, "pid": 20}}

    monkeypatch.setattr(roblox, "validate_token", validate)
    monkeypatch.setattr(panel.player, "sign_in", restart)
    result = panel.set_account({"token": "test-cookie"})
    assert events == ["verify", "restart"]
    assert result["player"]["instance"]["pid"] == 20
    assert "test-cookie" not in json.dumps(result)


def test_failed_player_login_keeps_verified_account(paths, monkeypatch):
    from whoisauto.player import PlayerError
    from whoisauto.web import PanelError

    panel = Panel(paths)
    monkeypatch.setattr(roblox, "validate_token", lambda token: {"id": 7, "name": "tester"})

    def fail(token):
        raise PlayerError(502, "player_launch_failed", "Could not open Roblox")

    monkeypatch.setattr(panel.player, "sign_in", fail)
    with pytest.raises(PanelError) as error:
        panel.set_account({"token": "test-cookie"})
    assert load_account(paths)["token"] == "test-cookie"
    assert error.value.details["account"]["signed_in"] is True
    assert "test-cookie" not in json.dumps(error.value.details)
    assert "Account saved" in str(error.value)


@pytest.mark.parametrize("route,method", [("open", "open"), ("restart", "restart"), ("stop", "stop")])
def test_player_routes_use_saved_cookie(panel, paths, monkeypatch, route, method):
    from whoisauto.player import PlayerControls
    from whoisauto.web import save_account

    save_account(paths, "test-cookie", {"id": 7, "name": "tester"})
    seen = []

    def control(self, *args):
        seen.append(args)
        return {"ok": True, "stopped": []}

    monkeypatch.setattr(PlayerControls, method, control)
    status, payload = call(panel, "POST", f"/api/roblox/{route}", {})
    assert status == 200
    expected = [()] if route == "stop" else [("test-cookie",)]
    assert seen == expected


def test_player_open_requires_saved_account(panel):
    status, payload = call(panel, "POST", "/api/roblox/open", {})
    assert status == 400
    assert payload["error"] == "account_required"


def test_ticket_error_details_reach_account_api(panel, monkeypatch):
    from whoisauto.player import PlayerControls, PlayerError

    monkeypatch.setattr(roblox, "validate_token", lambda token: {"id": 7, "name": "tester"})

    def denied(self, token):
        raise PlayerError(403, "ticket_forbidden", "Roblox blocked Player login",
                          stage="launch_ticket", upstream_status=403, upstream_codes=[1])

    monkeypatch.setattr(PlayerControls, "sign_in", denied)
    status, payload = call(panel, "POST", "/api/account", {"token": "test-cookie"})
    assert status == 403
    assert payload["error"] == "ticket_forbidden"
    assert payload["stage"] == "launch_ticket"
    assert payload["upstream_codes"] == [1]
    assert payload["account"]["signed_in"] is True
    assert "test-cookie" not in json.dumps(payload)


def test_manual_login_route_does_not_require_saved_cookie(panel, monkeypatch):
    from whoisauto.player import PlayerControls
    monkeypatch.setattr(PlayerControls, "open_login", lambda self: {
        "ok": True, "manual_sign_in": True, "instance": {"pid": 20, "port": None}})
    status, payload = call(panel, "POST", "/api/roblox/login", {})
    assert status == 200
    assert payload["manual_sign_in"] is True
