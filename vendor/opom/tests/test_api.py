import base64
import http.client
import json
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest

from opom.api import API, lua_value
from opom.core import APIError, Config, text, write_json
from opom.server import Server


class FakeCompiler:
    def __init__(self):
        self.fail = False

    def check(self, source):
        text(source, "script")
        return {"ok": False, "error": "invalid syntax"} if self.fail else {"ok": True, "bytecode_bytes": 1}


class FakeBackend:
    def __init__(self, config):
        self.config = config
        self.complete = True
        self.sources = []
        self.return_error = False

    def apps(self):
        return {"roblox": {"available": True}, "opiumware": {"available": True}}

    def instances(self, refresh=False):
        return [{"port": 8390, "pid": 123, "executable": "fixture"}]

    def join(self, place_id, port=None, job_id=None):
        return {"ok": True, "url_delivered": True, "joined": False, "place_id": place_id,
                "instance": self.instances()[0]}

    def stop(self, port=None, force=False):
        return {"ok": True, "port": port, "forced": force}

    def send(self, source, port=None, prefix="OpiumwareScript "):
        self.sources.append(source)
        match = re.search(r'job_id="([0-9a-f]{32})"', source)
        if self.complete and match:
            job_id = match[1]
            result = {"job_id": job_id, "ok": not self.return_error, "place_id": 123,
                      "output": [{"level": "print", "text": "hello"}],
                      "returns": [{"type": "number", "value": 42, "text": "42"}]}
            if self.return_error:
                result.update(kind="runtime", error="fixture runtime error")
            write_json(self.config.workspace / ".opom/results" / (job_id + ".json"), result)
        return {"status": "submitted", "instance": self.instances()[0], "compressed_bytes": len(source)}


class APITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.config = Config(root, root / "state", root / "workspace", root / "Roblox.app",
                             root / "Opiumware.app", root / "compiler")
        self.backend = FakeBackend(self.config)
        self.compiler = FakeCompiler()
        self.api = API(self.config, self.backend, self.compiler)
        self.server = Server(("127.0.0.1", 0), self.config, self.api)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temporary.cleanup()

    def request(self, method, path, body=None, token=None, raw=None, content_type="application/json"):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        data = raw if raw is not None else json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": content_type}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        status, mime = response.status, response.getheader("Content-Type")
        connection.close()
        return status, json.loads(payload) if mime.startswith("application/json") else payload.decode()

    def test_execute_result_status_and_history(self):
        status, data = self.request("POST", "/execute", {"script": "return 42", "wait": 1})
        self.assertEqual(status, 200)
        job = data["job"]
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["result"]["returns"][0]["value"], 42)
        self.assertEqual(self.request("GET", "/jobs/" + job["id"])[1]["job"]["id"], job["id"])
        self.assertEqual(self.request("GET", "/jobs/" + job["id"] + "/result")[0], 200)
        self.assertEqual(len(self.request("GET", "/jobs")[1]["jobs"]), 1)
        self.assertEqual(self.request("DELETE", "/jobs/" + job["id"])[0], 200)
        self.assertEqual(self.request("GET", "/jobs/" + job["id"])[0], 404)

    def test_pending_timeout_and_late_completion(self):
        self.backend.complete = False
        status, data = self.request("POST", "/execute", {"script": "return 42", "timeout": .1})
        self.assertEqual(status, 202)
        job_id = data["job"]["id"]
        time.sleep(.15)
        status, data = self.request("GET", "/jobs/" + job_id + "/result")
        self.assertEqual(status, 504)
        self.assertTrue(data["job"]["script_may_still_be_running"])
        write_json(self.api.jobs.results / (job_id + ".json"), {"job_id": job_id, "ok": True, "returns": []})
        status, data = self.request("GET", "/jobs/" + job_id)
        self.assertEqual(data["job"]["status"], "completed")
        self.assertTrue(data["job"]["completed_after_timeout"])

    def test_runtime_error_is_not_success(self):
        self.backend.return_error = True
        status, data = self.request("POST", "/execute", {"script": 'error("test")'})
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertEqual(data["job"]["status"], "failed")

    def test_join_requires_loaded_connected_requested_game(self):
        state = {"place_id": 123, "loaded": False, "connected": False, "game_job_id": ""}
        self.api.runtime = lambda *args: (200, {"runtime": state})
        status, value = self.request("POST", "/join", {"place_id": 123, "wait": .01})
        self.assertEqual(status, 202)
        self.assertFalse(value["joined"])
        state.update(loaded=True, connected=True, game_job_id="another-game")
        status, value = self.request("POST", "/join", {"place_id": 123, "game_instance_id": "requested-game", "wait": .01})
        self.assertFalse(value["joined"])
        state["game_job_id"] = "requested-game"
        status, value = self.request("POST", "/join", {"place_id": 123, "game_instance_id": "requested-game", "wait": .01})
        self.assertEqual(status, 200)
        self.assertTrue(value["joined"])

    def test_force_stop_requires_boolean_and_reaches_backend(self):
        status, value = self.request("POST", "/instances/stop", {"port": 8390, "force": True})
        self.assertEqual(status, 200)
        self.assertTrue(value["forced"])
        self.assertEqual(self.request("POST", "/instances/stop", {"force": "yes"})[0], 400)

    def test_syntax_failure_does_not_submit(self):
        self.compiler.fail = True
        status, data = self.request("POST", "/execute", {"script": "local ="})
        self.assertEqual(status, 400)
        self.assertEqual(data["error"], "syntax")
        self.assertEqual(self.backend.sources, [])
        self.assertFalse(self.request("POST", "/check", {"script": "local ="})[1]["ok"])

    def test_calls_methods_and_lua_arguments(self):
        status, _ = self.request("POST", "/call", {"function": "game.GetService", "method": True,
                                                     "args": ["Players"]})
        self.assertEqual(status, 200)
        self.assertIn('return game:GetService("Players")', self.backend.sources[-1])
        self.request("POST", "/call", {"function": "typeof", "args": [{"$lua": "workspace"}]})
        self.assertIn("return typeof((workspace))", self.backend.sources[-1])
        self.assertEqual(self.request("POST", "/call", {"function": "bad();error", "args": []})[0], 400)
        self.assertEqual(lua_value([1, None, False]), "{[1]=1,[2]=nil,[3]=false}")

    def test_reserved_template_text_is_preserved(self):
        self.request("POST", "/execute", {"script": 'return "__ID__", "__FILE__"'})
        self.assertIn('return "__ID__", "__FILE__"', self.backend.sources[-1])

    def test_files_roundtrip_and_boundaries(self):
        content = base64.b64encode(b"\xff\x00hello").decode()
        status, _ = self.request("PUT", "/files", {"path": "nested/test.bin", "content": content, "encoding": "base64"})
        self.assertEqual(status, 200)
        value = self.request("GET", "/files?path=nested/test.bin")[1]
        self.assertEqual(base64.b64decode(value["content"]), b"\xff\x00hello")
        self.assertEqual(self.request("PUT", "/files", {"path": "../outside", "content": "x"})[0], 400)
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        (self.config.workspace / "escape").symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.request("PUT", "/files", {"path": "escape/file", "content": "x"})[0], 400)
        self.assertEqual(self.request("DELETE", "/files?path=nested/test.bin")[0], 200)

    def test_authenticated_api_and_public_docs(self):
        self.config.token = "unit-test-token"
        self.assertEqual(self.request("GET", "/health")[0], 401)
        self.assertEqual(self.request("GET", "/health", token="unit-test-token")[0], 200)
        self.assertEqual(self.request("GET", "/docs")[0], 200)

    def test_cli_does_not_treat_auth_failure_as_stopped(self):
        from unittest.mock import patch
        from opom.cli import main
        self.config.token = "protected-test-token"
        with patch.dict("os.environ", {"OPOM_API_TOKEN": "incorrect-test-token"}), patch("sys.argv", [
                "opom", "start", "--port", str(self.server.server_address[1])]):
            with self.assertRaisesRegex(RuntimeError, "authentication failed"):
                main()

    def test_invalid_json_types_and_flags(self):
        self.assertEqual(self.request("POST", "/execute", raw=b"{broken")[0], 400)
        self.assertEqual(self.request("POST", "/execute", body=[])[0], 400)
        self.assertEqual(self.request("POST", "/execute", {"script": "return 1", "raw": "false"})[0], 400)
        self.assertEqual(self.request("POST", "/execute", {"script": "return 1"}, content_type="text/plain")[0], 415)
        self.assertEqual(self.request("POST", "/execute", {"script": "return 1", "wait": -1})[0], 400)

    def test_raw_and_documented_routes(self):
        status, data = self.request("POST", "/execute", {"script": "return 1", "raw": True})
        self.assertEqual(status, 202)
        self.assertTrue(data["raw"])
        self.assertEqual(self.backend.sources[-1], "return 1")
        self.assertEqual(self.request("GET", "/instances")[0], 200)
        self.assertEqual(self.request("GET", "/capabilities")[0], 200)
        spec = self.request("GET", "/openapi.json")[1]
        self.assertEqual(sum(len(ops) for ops in spec["paths"].values()), 25)
        self.assertEqual(self.request("GET", "/not-an-endpoint")[0], 404)


if __name__ == "__main__":
    unittest.main()
