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
    status, payload = call(panel, "POST", "/api/account", {"token": ".ROBLOSECURITY=abc123"})
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
