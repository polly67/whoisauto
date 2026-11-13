import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whoisauto.config import Paths


@pytest.fixture
def paths(tmp_path):
    value = Paths(
        repo=tmp_path,
        runtime=tmp_path / "runtime",
        state=tmp_path / "runtime" / "state",
        root=tmp_path / "runtime" / "opom-root",
        workspace=tmp_path / "runtime" / "opom-root" / "Data" / "workspace",
        installers=tmp_path / "installers",
    )
    value.ensure()
    return value


class _FakeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def reply(self, payload, status=200):
        encoded = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)
        self.close_connection = True

    def do_GET(self):
        self.server.calls.append(("GET", self.path, None))
        if self.path.startswith("/health"):
            if self.server.stopped:
                return self.reply({"ok": False, "error": "stopped"}, 503)
            return self.reply({"ok": True, "service": "opom", "pid": 4242,
                               "instances": [{"port": 8390, "pid": 99, "executable": "/tmp/RobloxPlayer"}],
                               "apps": {}, "ready": True})
        if self.path.startswith("/instances"):
            return self.reply({"ok": True, "instances": [{"port": 8390, "pid": 99,
                                                          "executable": "/tmp/RobloxPlayer"}]})
        if self.path.startswith("/runtime"):
            return self.reply({"ok": True, "runtime": {"place_id": 42, "game_job_id": "job-1",
                                                       "loaded": True, "connected": True,
                                                       "player": {"name": "tester", "user_id": 7,
                                                                  "display_name": "Tester"}}})
        return self.reply({"ok": False, "error": "not_found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.calls.append(("POST", self.path, body))
        if self.path == "/join":
            return self.reply({"ok": True, "joined": True, "place_id": body.get("place_id"),
                               "instance": {"port": 8390, "pid": 99}, "runtime": {"place_id": 42}})
        if self.path == "/execute":
            return self.reply({"ok": True, "job": {"id": "a" * 32, "status": "completed",
                                                   "result": {"returns": [{"type": "string", "value": "hi"}]}}})
        if self.path == "/check":
            return self.reply({"ok": True, "valid": True})
        if self.path == "/shutdown":
            self.server.stopped = True
            return self.reply({"ok": True})
        return self.reply({"ok": False, "error": "not_found"}, 404)


@pytest.fixture
def fake_opom():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHandler)
    server.calls = []
    server.stopped = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
