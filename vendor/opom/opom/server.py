from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
from pathlib import Path
import socketserver
import urllib.parse

from .api import API
from .core import APIError, REPO

BODY_LIMIT = 24 * 1024 * 1024 + 4096


class Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, address, config, api=None):
        self.api = api or API(config)
        self.config = config
        super().__init__(address, Handler)
        self.api.shutdown = self.shutdown

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = self.server_address[1]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "opom/0.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def reply(self, status, payload, content_type="application/json"):
        encoded = json.dumps(payload, ensure_ascii=False).encode() if content_type == "application/json" else payload
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)
        self.close_connection = True

    def dispatch(self):
        try:
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path.rstrip("/") or "/"
            if self.command == "GET" and path in ("/", "/docs"):
                return self.reply(200, (REPO / "docs/index.html").read_bytes(), "text/html")
            token = self.server.config.token
            if token and not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                raise APIError(401, "unauthorized", "Use the configured API bearer token")
            query = {key: values[0] for key, values in urllib.parse.parse_qs(parsed.query).items()}
            if self.command == "GET" and path == "/openapi.json":
                return self.reply(200, json.loads((REPO / "openapi.json").read_text()))
            body = None
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get("Transfer-Encoding"):
                raise APIError(400, "invalid_body", "Send Content-Length with JSON")
            if len(lengths) > 1:
                raise APIError(400, "invalid_length", "Use one Content-Length header")
            length = 0
            if lengths:
                try:
                    length = int(lengths[0])
                except ValueError:
                    raise APIError(400, "invalid_length", "Content-Length must be an integer") from None
            if length < 0 or length > BODY_LIMIT:
                raise APIError(413, "body_too_large", "Body exceeds the supported limit")
            if self.command in ("POST", "PUT") and not lengths:
                raise APIError(411, "length_required", "Send a JSON object with Content-Length")
            if length:
                if self.headers.get_content_type() != "application/json":
                    raise APIError(415, "content_type", "Use application/json")
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise APIError(400, "incomplete_body", "Request body is incomplete")
                try:
                    body = json.loads(raw.decode())
                except (ValueError, UnicodeDecodeError):
                    raise APIError(400, "invalid_json", "Use valid UTF-8 JSON") from None
                if not isinstance(body, dict):
                    raise APIError(400, "invalid_body", "Request body must be a JSON object")
            status, payload = self.server.api.handle(self.command, path, body, query)
            self.reply(status, payload)
        except APIError as error:
            self.reply(error.status, error.payload)
        except TimeoutError:
            self.reply(408, {"ok": False, "error": "request_timeout"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            self.reply(500, {"ok": False, "error": "internal_error", "message": str(error)})

    do_GET = dispatch
    do_POST = dispatch
    do_PUT = dispatch
    do_DELETE = dispatch
