#!/usr/bin/env python3
"""Verify opom against a real, joined Opiumware Player; not run by fixture CI."""

import argparse
import hashlib
import json
from pathlib import Path
import time
import urllib.error
import urllib.request
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--port", type=int)
    parser.add_argument("--place-id", type=int, required=True)
    parser.add_argument("--report", type=Path, default=Path(".state/live-smoke.json"))
    args = parser.parse_args()
    report = {}

    def request(method, path, body=None):
        request = urllib.request.Request(args.url + path, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        try:
            response = urllib.request.urlopen(request, timeout=30)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            value = json.load(response)
            return response.status, value

    def run(name, path, body, successful=True):
        status, value = request("POST", path, {**body, "port": args.port, "wait": 5})
        report[name] = value
        assert status == 200, (status, value)
        job = value["job"]
        assert job["status"] == ("completed" if successful else "failed"), job
        result = job["result"]
        assert result["ok"] == successful, result
        assert result["place_id"] == args.place_id, result
        return result

    try:
        status, health = request("GET", "/health")
        report["health"] = health
        assert status == 200 and health["ready"], health
        assert all(value["available"] for value in health["apps"].values()), health
        result = run("capture", "/execute", {"script": 'print("héllo",nil);warn("warn");return 42,nil,true'})
        assert result["output"] == [{"level": "print", "text": "héllo\tnil"}, {"level": "warn", "text": "warn"}], result
        assert [value["type"] for value in result["returns"]] == ["number", "nil", "boolean"], result
        assert result["returns"][0]["value"] == 42 and result["returns"][2]["value"] is True, result
        run("eval", "/eval", {"expression": "game.PlaceId"})
        result = run("hash", "/call", {"function": "crypt.hash", "args": ["hello", "sha256"]})
        assert result["returns"][0]["value"].lower() == hashlib.sha256(b"hello").hexdigest(), result
        result = run("native_print", "/call", {"function": "iscclosure", "args": [{"$lua": "print"}]})
        assert result["returns"][0]["value"] is True, result
        result = run("lua_argument", "/call", {"function": "typeof", "args": [{"$lua": "workspace"}]})
        assert result["returns"][0]["value"] == "Instance", result
        result = run("method", "/call", {"function": "game.GetService", "method": True, "args": ["Players"]})
        assert result["returns"][0]["text"] == "Players", result
        result = run("runtime_error", "/execute", {"script": 'print("before");error("OPOM_EXPECTED_ERROR")'}, False)
        assert "OPOM_EXPECTED_ERROR" in result["error"] and result["output"][0]["text"] == "before", result
        status, checked = request("POST", "/check", {"script": "local ="})
        report["syntax"] = checked
        assert status == 200 and checked["ok"] is False, checked
        filename = "opom-smoke-" + uuid.uuid4().hex + ".luau"
        assert request("PUT", "/files", {"path": filename, "content": "return game.PlaceId"})[0] == 200
        try:
            status, content = request("GET", "/files?path=" + filename)
            assert content["content"] == "return game.PlaceId", content
            run("execute_file", "/execute-file", {"path": filename})
        finally:
            assert request("DELETE", "/files?path=" + filename)[0] == 200
        result = run("yield", "/execute", {"script": 'task.wait(.05);return "yielded"'})
        assert result["returns"][0]["value"] == "yielded", result
        report["ok"] = True
        print("Real Player execution, results, calls, files, errors and yielding verified")
    finally:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
