"""Local HTTP panel: static UI plus a small JSON API in front of opom."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import mimetypes
import os
from pathlib import Path
import socketserver
import sys
import time
import urllib.parse

from . import compiler, installer, roblox
from .autoexec import AutoExec
from .config import (ConfigError, DEFAULT_OPOM_PORT, DEFAULT_WEB_PORT, Paths, find_free_port, read_json, write_json)
from .service import OpomService, ServiceError
from .player import PlayerControls, PlayerError

STATIC = Path(__file__).resolve().parent / "static"
BODY_LIMIT = 8 * 1024 * 1024


class PanelError(RuntimeError):
    def __init__(self, status: int, code: str, message: str, **details):
        self.status = status
        self.code = code
        self.message = message
        self.details = details
        super().__init__(message)


def _integer(value, name: str, low: int = 1, high: int | None = None) -> int:
    if isinstance(value, str):
        try:
            value = int(value)
        except ValueError:
            pass
    if type(value) is not int or value < low or (high is not None and value > high):
        raise PanelError(400, f"invalid_{name}", f"{name} must be an integer from {low}"
                         + (f" to {high}" if high is not None else ""))
    return value


def _number(value, name: str, low: float, high: float) -> float:
    if type(value) not in (int, float) or not low <= value <= high:
        raise PanelError(400, f"invalid_{name}", f"{name} must be a number from {low:g} to {high:g}")
    return value


def _boolean(body: dict, name: str, default: bool) -> bool:
    value = body.get(name, default)
    if type(value) is not bool:
        raise PanelError(400, f"invalid_{name}", f"{name} must be a boolean")
    return value


def _script(body: dict, empty_message: str) -> str:
    script = body.get("script", "")
    if not isinstance(script, str):
        raise PanelError(400, "invalid_script", "script must be text")
    try:
        size = len(script.encode("utf-8"))
    except UnicodeEncodeError:
        raise PanelError(400, "invalid_script", "script must contain valid Unicode") from None
    if size > 4 * 1024 * 1024:
        raise PanelError(413, "script_too_large", "script must be at most 4 MiB")
    if not script.strip():
        raise PanelError(400, "empty_script", empty_message)
    return script


def _matches(value: str, token: str) -> bool:
    try:
        return hmac.compare_digest(value.encode("utf-8"), token.encode("utf-8"))
    except UnicodeEncodeError:
        return False


# --------------------------------------------------------------------- account

def load_account(paths: Paths) -> dict | None:
    record = read_json(paths.account_file)
    if not isinstance(record, dict) or not isinstance(record.get("token"), str) or not record["token"]:
        return None
    return record


def save_account(paths: Paths, token: str, user: dict) -> dict:
    record = {"token": token, "user": user, "saved_at": int(time.time())}
    write_json(paths.account_file, record, mode=0o600)
    return record


def clear_account(paths: Paths) -> None:
    paths.account_file.unlink(missing_ok=True)


def public_account(record: dict | None) -> dict:
    if not record:
        return {"signed_in": False}
    user = record.get("user")
    if not isinstance(user, dict):
        user = {}
    return {"signed_in": True, "user": user, "masked": roblox.mask(record["token"]),
            "saved_at": record.get("saved_at")}


# ----------------------------------------------------------------------- panel

class Panel:
    def __init__(self, paths: Paths, service: OpomService | None = None):
        self.paths = paths
        self.service = service or OpomService(paths)
        self.autoexec = AutoExec(paths, self.service)
        self.player = PlayerControls(paths, self.service, before_stop=self.autoexec.cancel)
        self._last_place: int | None = None
        self._last_port: int | None = None

    def _call(self, method: str, path: str, payload=None, timeout: float = 40,
              error: str = "opom_failed", message: str = "opom request failed") -> dict:
        status, value = self.service.call(method, path, payload, timeout=timeout)
        if status >= 400:
            raise PanelError(status, value.get("error", error), value.get("message", message))
        return value

    # --------------------------------------------------------------- read routes

    def state(self) -> dict:
        account = load_account(self.paths)
        return {
            "ok": True,
            "platform": sys.platform,
            "darwin": sys.platform == "darwin",
            "service": self.service.status(),
            "install": installer.probe(self.paths),
            "compiler": {"available": compiler.available(self.paths),
                         "path": str(compiler.binary(self.paths))},
            "account": public_account(account),
            "autoexec": self.autoexec.snapshot(include_script=False),
            "paths": {"root": str(self.paths.root), "state": str(self.paths.state),
                      "installers": str(self.paths.installers), "log": str(self.paths.log_file)},
        }

    def players(self, place_id: int | None = None) -> dict:
        instances = []
        payload = self._call("GET", "/instances")
        raw = payload.get("instances") or []
        if not isinstance(raw, list) or any(not isinstance(item, dict) or "port" not in item for item in raw):
            raise PanelError(502, "api_bad_response", "opom returned invalid instances")

        def runtime(instance):
            try:
                value = self._call("GET", f"/runtime?port={instance['port']}", timeout=8)
                return value.get("runtime")
            except (ServiceError, PanelError):
                return None

        with ThreadPoolExecutor(max_workers=min(8, max(1, len(raw)))) as pool:
            runtimes = list(pool.map(runtime, raw))
        for instance, info in zip(raw, runtimes):
            instances.append({**instance, "runtime": info})

        result = {"ok": True, "instances": instances, "count": len(instances)}
        if place_id:
            account = load_account(self.paths)
            try:
                result["place"] = roblox.place_details(place_id)
                result["servers"] = roblox.public_servers(
                    place_id, (account or {}).get("token"))
            except roblox.RobloxError as error:
                result["place_error"] = {"error": error.code, "message": error.message}
        return result

    # -------------------------------------------------------------- write routes

    def install(self, body: dict) -> dict:
        for name in ("editor_source", "roblox_source"):
            if body.get(name) is not None and not isinstance(body[name], str):
                raise PanelError(400, "invalid_source", f"{name} must be text")
        result = installer.install_all(self.paths,
                                       editor_source=body.get("editor_source") or None,
                                       roblox_source=body.get("roblox_source") or None,
                                       launch=_boolean(body, "launch", False))
        return result

    def save_autoexec(self, body: dict) -> dict:
        try:
            return self.autoexec.save(body)
        except ValueError as error:
            raise PanelError(400, "invalid_autoexec", str(error)) from None

    def run_autoexec(self) -> dict:
        job = self.autoexec.trigger("manual", self._last_place, self._last_port)
        if job is None:
            raise PanelError(400, "autoexec_disabled",
                             "Enable auto-execute and save a script first")
        return {"ok": True, "job": job}

    def build_compiler(self) -> dict:
        try:
            return compiler.build(self.paths)
        except compiler.CompilerError as error:
            message = error.message + ("\n" + error.log if error.log else "")
            raise PanelError(502, "compiler_build_failed", message) from None

    def start_service(self, body: dict) -> dict:
        if "port" in body:
            self.service.preferred = _integer(body["port"], "port", high=65535)
        return self.service.start()

    def stop_service(self) -> dict:
        self.autoexec.cancel()
        return self.service.stop()

    def set_account(self, body: dict) -> dict:
        launch = _boolean(body, "launch", True)
        try:
            token = roblox.normalize_token(body.get("token", ""))
            user = roblox.validate_token(token)
        except roblox.RobloxError as error:
            raise PanelError(400 if error.code == "invalid_token" else 401, error.code, error.message) from None
        record = save_account(self.paths, token, user)
        account = public_account(record)
        result = {"ok": True, "account": account}
        if launch:
            self.autoexec.cancel()
            try:
                result["player"] = self.player.restart(token)
            except PlayerError as error:
                raise PanelError(error.status, error.code, "Account saved, but " + str(error),
                                 account=account) from None
            self._last_place = self._last_port = None
        return result

    def player_action(self, action: str) -> dict:
        if action == "stop":
            self.autoexec.cancel()
            result = self.player.stop()
        else:
            account = load_account(self.paths)
            if not account:
                raise PanelError(400, "account_required", "Save a Roblox account cookie first")
            if action == "restart":
                self.autoexec.cancel()
            operation = self.player.restart if action == "restart" else self.player.open
            result = operation(account["token"])
        if action in ("stop", "restart"):
            self._last_place = self._last_port = None
        return result

    def join(self, body: dict) -> dict:
        place_id = _integer(body.get("place_id"), "place_id")
        payload = {"place_id": place_id, "wait": _number(body.get("wait", 20), "wait", 0, 30)}
        if body.get("game_instance_id"):
            payload["game_instance_id"] = str(body["game_instance_id"])
        if "port" in body:
            payload["port"] = _integer(body["port"], "port", high=65535)
        value = self._call("POST", "/join", payload, timeout=45,
                           error="join_failed", message="Join failed")
        trigger = "rejoin" if self._last_place == place_id else "join"
        self._last_place = place_id
        port = (value.get("instance") or {}).get("port") or payload.get("port")
        self._last_port = port
        job = self.autoexec.trigger(trigger, place_id, port)
        return {**value, "autoexec": job}

    def execute(self, body: dict) -> dict:
        script = _script(body, "Write or paste a script first")
        payload = {"script": script, "wait": _number(body.get("wait", 10), "wait", 0, 30),
                   "timeout": _number(body.get("timeout", 30), "timeout", 0.1, 300),
                   "capture": _boolean(body, "capture", True)}
        if "port" in body:
            payload["port"] = _integer(body["port"], "port", high=65535)
        if _boolean(body, "check", True):
            checked = self._call("POST", "/check", {"script": script},
                                 error="check_failed", message="Syntax check failed")
            if not checked.get("ok"):
                return {"ok": False, "error": "syntax_error", "check": checked}
        return self._call("POST", "/execute", payload, timeout=payload["wait"] + 30,
                          error="execute_failed", message="Execution failed")

    def check(self, body: dict) -> dict:
        script = _script(body, "Nothing to check")
        return self._call("POST", "/check", {"script": script},
                          error="check_failed", message="Syntax check failed")


# ---------------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    server_version = "whoisauto/0.1"
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    @property
    def panel(self) -> Panel:
        return self.server.panel  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):  # keep the console readable
        message = fmt % args
        try:
            parsed = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            if any(key == "token" for key, _ in query):
                safe_query = urllib.parse.urlencode([(key, "REDACTED" if key == "token" else value)
                                                     for key, value in query])
                safe_path = urllib.parse.urlunsplit(parsed._replace(query=safe_query))
                message = message.replace(self.path, safe_path)
        except ValueError:
            pass
        sys.stderr.write("%s - %s\n" % (self.address_string(), message))

    # ------------------------------------------------------------------ plumbing

    def send_json(self, status: int, payload: dict) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)
        self.close_connection = True

    def send_file(self, path: Path, status: int = 200) -> None:
        data = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
            content_type += "; charset=utf-8"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def read_body(self) -> dict:
        if self.headers.get("Transfer-Encoding"):
            raise PanelError(400, "invalid_encoding", "Send a Content-Length header instead of Transfer-Encoding")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) > 1:
            raise PanelError(400, "invalid_length", "Send one Content-Length header")
        if not lengths:
            return {}
        try:
            length = int(lengths[0])
        except ValueError:
            raise PanelError(400, "invalid_length", "Content-Length must be an integer") from None
        if length < 0 or length > BODY_LIMIT:
            raise PanelError(413, "body_too_large", "Request body is too large")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise PanelError(400, "invalid_body", "Request body is incomplete")
        if not raw:
            return {}
        try:
            def invalid_constant(value):
                raise ValueError(f"Invalid JSON number: {value}")
            body = json.loads(raw.decode(), parse_constant=invalid_constant)
        except (ValueError, UnicodeDecodeError):
            raise PanelError(400, "invalid_json", "Send valid UTF-8 JSON") from None
        if not isinstance(body, dict):
            raise PanelError(400, "invalid_body", "Body must be a JSON object")
        return body

    def authorized(self, path: str) -> bool:
        token = self.server.access_token
        if not token or path.startswith("/static/"):
            return True
        if _matches(self.headers.get("Authorization", ""), "Bearer " + token):
            return True
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == "whoisauto_token" and _matches(value, token):
                return True
        return False

    def grant(self, token: str) -> None:
        if not _matches(token, self.server.access_token):
            raise PanelError(401, "unauthorized", "Bad access token")
        self.send_response(302)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie",
                         f"whoisauto_token={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=43200")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def dispatch(self, method: str) -> None:
        try:
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = {key: values[0] for key, values in urllib.parse.parse_qs(parsed.query).items()}
            if self.server.access_token and path == "/" and query.get("token"):
                return self.grant(query["token"])
            if not self.authorized(path):
                raise PanelError(401, "unauthorized",
                                 "Append ?token=<access token> to the URL, or send a bearer token")
            if method == "GET" and not path.startswith("/api/"):
                return self.serve_static(path)
            body = self.read_body() if method in ("POST", "PUT", "DELETE") else {}
            status, payload = self.route(method, path, body, query)
            self.send_json(status, payload)
        except PanelError as error:
            self.send_json(error.status, {"ok": False, "error": error.code, "message": error.message,
                                          **error.details})
        except PlayerError as error:
            self.send_json(error.status, {"ok": False, "error": error.code, "message": str(error)})
        except (ServiceError, installer.InstallError) as error:
            code = getattr(error, "code", "install_failed")
            status = 502 if code == "api_bad_response" else 503 if code in ("api_down", "api_unreachable", "api_timeout") else 400
            self.send_json(status,
                           {"ok": False, "error": code, "message": str(error)})
        except ConfigError as error:
            self.send_json(400, {"ok": False, "error": "invalid_port", "message": str(error)})
        except TimeoutError:
            self.send_json(408, {"ok": False, "error": "request_timeout", "message": "Request timed out"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:  # keep the panel alive on unexpected input
            self.send_json(500, {"ok": False, "error": "internal_error", "message": str(error)})

    def serve_static(self, path: str) -> None:
        relative = "index.html" if path == "/" else path.lstrip("/")
        if relative.startswith("static/"):
            relative = relative[len("static/"):]
        target = (STATIC / relative).resolve()
        if not target.is_relative_to(STATIC.resolve()) or not target.is_file():
            raise PanelError(404, "not_found", "No such file")
        self.send_file(target)

    def route(self, method: str, path: str, body: dict, query: dict) -> tuple[int, dict]:
        panel = self.panel
        if method == "GET" and path == "/api/state":
            return 200, panel.state()
        if method == "GET" and path == "/api/players":
            place = query.get("place_id")
            return 200, panel.players(_integer(place, "place_id") if place else None)
        if method == "POST" and path == "/api/install":
            return 200, panel.install(body)
        if method == "GET" and path == "/api/autoexec":
            return 200, panel.autoexec.snapshot()
        if method == "POST" and path == "/api/autoexec":
            return 200, panel.save_autoexec(body)
        if method == "POST" and path == "/api/autoexec/cancel":
            return 200, panel.autoexec.cancel()
        if method == "POST" and path == "/api/autoexec/run":
            return 200, panel.run_autoexec()
        if method == "POST" and path == "/api/compiler/build":
            return 200, panel.build_compiler()
        if method == "POST" and path == "/api/service/start":
            return 200, panel.start_service(body)
        if method == "POST" and path == "/api/service/stop":
            return 200, panel.stop_service()
        if method == "POST" and path in ("/api/roblox/open", "/api/roblox/restart", "/api/roblox/stop"):
            return 200, panel.player_action(path.rsplit("/", 1)[1])
        if method == "GET" and path == "/api/account":
            return 200, {"ok": True, "account": public_account(load_account(panel.paths))}
        if method == "POST" and path == "/api/account":
            return 200, panel.set_account(body)
        if method == "DELETE" and path == "/api/account":
            clear_account(panel.paths)
            return 200, {"ok": True, "account": {"signed_in": False}}
        if method == "POST" and path == "/api/join":
            return 200, panel.join(body)
        if method == "POST" and path == "/api/execute":
            return 200, panel.execute(body)
        if method == "POST" and path == "/api/check":
            return 200, panel.check(body)
        raise PanelError(404, "not_found", f"No route for {method} {path}")

    do_GET = lambda self: self.dispatch("GET")  # noqa: E731
    do_POST = lambda self: self.dispatch("POST")  # noqa: E731
    do_DELETE = lambda self: self.dispatch("DELETE")  # noqa: E731


class Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, address, panel: Panel, access_token: str = ""):
        self.panel = panel
        self.access_token = access_token
        super().__init__(address, Handler)

    def server_bind(self):
        # HTTPServer.server_bind calls socket.getfqdn(), which can block for
        # tens of seconds on a runner with slow reverse DNS. opom skips it for
        # the same reason; the panel never uses server_name.
        socketserver.TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = self.server_address[1]

    def server_close(self):
        self.panel.autoexec.close()
        super().server_close()


def serve(paths: Paths, port: int = DEFAULT_WEB_PORT, opom_port: int | None = None) -> Server:
    paths.ensure()
    service = OpomService(paths, DEFAULT_OPOM_PORT if opom_port is None else opom_port)
    panel = Panel(paths, service)
    bound = find_free_port(port)
    return Server(("127.0.0.1", bound), panel, os.environ.get("WHOISAUTO_ACCESS_TOKEN", ""))
