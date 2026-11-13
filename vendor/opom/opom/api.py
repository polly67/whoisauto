import json
import math
import os
import re
import threading
import time

from . import __version__
from .backend import Backend
from .core import APIError, Compiler, Files, lua_string, text
from .jobs import Jobs

FUNCTIONS = [
    "identifyexecutor", "getgenv", "getrenv", "getreg", "loadstring", "loadfile", "dofile",
    "readfile", "writefile", "appendfile", "listfiles", "makefolder", "isfile", "isfolder", "delfile", "delfolder",
    "request", "http_request", "http.request", "crypt.base64encode", "crypt.base64decode",
    "crypt.encrypt", "crypt.decrypt", "crypt.generatebytes", "crypt.generatekey", "crypt.hash",
    "lz4compress", "lz4decompress", "checkcaller", "clonefunction", "hookfunction", "restorefunction",
    "iscclosure", "islclosure", "newcclosure", "isexecutorclosure", "getrawmetatable", "setrawmetatable",
    "isreadonly", "setreadonly", "hookmetamethod", "getnamecallmethod", "getgc", "getinstances",
    "getnilinstances", "getconnections", "getcallbackvalue", "gethiddenproperty", "sethiddenproperty",
    "isscriptable", "setscriptable", "cloneref", "compareinstances", "gethui", "getcustomasset",
    "getcallingscript", "getscriptclosure", "getscriptbytecode", "getscripthash", "getscripts",
    "getrunningscripts", "getloadedmodules", "getsenv", "getthreadidentity", "setthreadidentity",
    "debug.getinfo", "debug.getconstant", "debug.getconstants", "debug.setconstant", "debug.getproto",
    "debug.getprotos", "debug.getstack", "debug.setstack", "debug.getupvalue", "debug.getupvalues",
    "debug.setupvalue", "Drawing.new", "Drawing.Fonts", "isrenderobj", "getrenderproperty",
    "setrenderproperty", "cleardrawcache", "WebSocket.connect", "queue_on_teleport", "setclipboard",
    "setfpscap", "getfpscap", "rconsoleprint", "rconsoleclear", "rconsoleinput", "rconsolesettitle",
    "decompile", "saveinstance", "fireclickdetector", "firetouchinterest", "fireproximityprompt",
]

RUNTIME_SOURCE = '''local p=game:GetService("Players").LocalPlayer
local network=game:GetService("NetworkClient")
return game:GetService("HttpService"):JSONEncode({place_id=game.PlaceId,game_job_id=game.JobId,
loaded=game:IsLoaded(),connected=network:FindFirstChildOfClass("ClientReplicator")~=nil,
player=p and {name=p.Name,user_id=p.UserId,display_name=p.DisplayName} or nil})'''


def lua_value(value, depth=0):
    if depth > 16:
        raise APIError(400, "argument_depth", "Arguments exceed 16 nesting levels")
    if value is None:
        return "nil"
    if type(value) is bool:
        return str(value).lower()
    if type(value) in (int, float):
        if not math.isfinite(value):
            raise APIError(400, "invalid_number", "Numbers must be finite")
        return repr(value)
    if isinstance(value, str):
        return lua_string(text(value, "argument", nonempty=False))
    if isinstance(value, list):
        return "{" + ",".join(f"[{i+1}]={lua_value(v,depth+1)}" for i, v in enumerate(value)) + "}"
    if isinstance(value, dict):
        if set(value) == {"$lua"}:
            return "(" + text(value["$lua"], "$lua") + ")"
        return "{" + ",".join("["+lua_string(k)+"]="+lua_value(v,depth+1) for k, v in value.items()) + "}"
    raise APIError(400, "invalid_argument", "Arguments must be JSON values")


class API:
    def __init__(self, config, backend=None, compiler=None):
        self.config = config
        self.backend = backend or Backend(config)
        self.compiler = compiler or Compiler(config)
        self.files = Files(config)
        self.jobs = Jobs(config, self.backend, self.compiler)
        self.shutdown = lambda: None

    def result(self, source, body=None, port=None):
        body = body or {}
        wait = body.get("wait", 0)
        if type(wait) not in (int, float) or not 0 <= wait <= 30:
            raise APIError(400, "invalid_wait", "wait must be from 0 to 30 seconds")
        job = self.jobs.submit(source, port if port is not None else body.get("port"), body.get("timeout", 30), body.get("capture", True))
        job = self.jobs.wait(job["id"], wait)
        status = 200 if job["status"] in ("completed", "failed") else 504 if job["status"] == "timed_out" else 202
        return status, {"ok": job["status"] not in ("failed", "timed_out"), "job": job}

    def runtime(self, port=None, wait=5):
        status, value = self.result(RUNTIME_SOURCE, {"wait": wait, "capture": False}, port)
        job = value["job"]
        if job["status"] == "completed":
            try:
                runtime = json.loads(job["result"]["returns"][0]["value"])
            except (KeyError, IndexError, ValueError):
                raise APIError(502, "invalid_runtime_result", "Player returned invalid runtime information") from None
            return 200, {"ok": True, "job_id": job["id"], "instance": job["instance"], "runtime": runtime}
        return status, value

    def join(self, body):
        wait = body.get("wait", 15)
        if type(wait) not in (int, float) or not 0 <= wait <= 30:
            raise APIError(400, "invalid_wait", "wait must be from 0 to 30 seconds")
        delivered = self.backend.join(body.get("place_id"), body.get("port"), body.get("game_instance_id"))
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            _, value = self.runtime(delivered["instance"]["port"], min(2, deadline-time.monotonic()))
            runtime = value.get("runtime", {})
            if runtime.get("place_id") == delivered["place_id"] and runtime.get("loaded") and runtime.get("connected") and runtime.get("game_job_id") and (
                    not body.get("game_instance_id") or runtime.get("game_job_id") == body["game_instance_id"]):
                return 200, {**delivered, "joined": True, "runtime": value["runtime"]}
            time.sleep(.2)
        return 202, delivered

    def handle(self, method, path, body, query):
        body = body or {}
        for name in ("raw", "method", "new_instance", "capture", "force"):
            if name in body and type(body[name]) is not bool:
                raise APIError(400, "invalid_argument", f"{name} must be a boolean")
        port = body.get("port")
        if "port" in query:
            try:
                port = int(query["port"])
            except ValueError:
                raise APIError(400, "invalid_port", "port must be an integer") from None
        if method == "GET" and path in ("/health", "/status"):
            instances = self.backend.instances()
            return 200, {"ok": True, "service": "opom", "version": __version__, "pid": os.getpid(),
                         "ready": bool(instances), "compiler_available": self.config.compiler.is_file(),
                         "apps": self.backend.apps(), "instances": instances}
        if method == "GET" and path == "/capabilities":
            return 200, {"ok": True, "version": __version__, "operations": [
                "execute", "eval", "call", "check", "execute-file", "jobs", "runtime", "functions",
                "instances", "launch", "join", "stop-player", "open-editor", "files", "settings", "shutdown"],
                "function_catalog": FUNCTIONS, "function_catalog_is_not_a_support_score": True}
        if method == "GET" and path == "/instances":
            return 200, {"ok": True, "instances": self.backend.instances(refresh=True)}
        if method == "GET" and path == "/runtime":
            return self.runtime(port)
        if method == "GET" and path == "/functions":
            lines = ['local result={}']
            for name in FUNCTIONS:
                lines.append(f'do local ok,value=pcall(function()return typeof({name})end);result[{lua_string(name)}]=ok and value or "missing" end')
            lines.append('return game:GetService("HttpService"):JSONEncode(result)')
            status, value = self.result("\n".join(lines), {"wait": 5, "capture": False}, port)
            job = value["job"]
            if job["status"] == "completed":
                return 200, {"ok": True, "job_id": job["id"], "functions": json.loads(job["result"]["returns"][0]["value"]),
                             "presence_is_not_a_support_score": True}
            return status, value
        if method == "POST" and path in ("/execute", "/eval", "/call", "/execute-file"):
            if path == "/execute-file":
                file = self.files.read(body.get("area", "workspace"), body.get("path", ""))
                if "entries" in file or file.get("encoding") != "utf-8":
                    raise APIError(400, "invalid_script_file", "Select a UTF-8 source file")
                source = file["content"]
            elif path == "/eval":
                source = "return " + text(body.get("expression"), "expression")
            elif path == "/call":
                name = text(body.get("function"), "function")
                if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", name, re.ASCII):
                    raise APIError(400, "invalid_function", "Use a dotted function path")
                args = body.get("args", [])
                if not isinstance(args, list) or len(args) > 128:
                    raise APIError(400, "invalid_args", "args must be an array of at most 128 values")
                if body.get("method", False):
                    if "." not in name:
                        raise APIError(400, "invalid_method", "Methods need an object path")
                    owner, member = name.rsplit(".", 1)
                    name = owner + ":" + member
                source = "return " + name + "(" + ",".join(lua_value(arg) for arg in args) + ")"
            else:
                source = text(body.get("script"), "script")
            if path in ("/call", "/eval") and "capture" not in body:
                body = {**body, "capture": False}
            if body.get("raw", False):
                if path != "/execute":
                    raise APIError(400, "invalid_raw", "raw is available only for /execute")
                return 202, {"ok": True, "raw": True, **self.backend.send(source, port)}
            return self.result(source, body, port)
        if method == "POST" and path == "/check":
            return 200, self.compiler.check(body.get("script"))
        if method == "GET" and path == "/jobs":
            return 200, {"ok": True, "jobs": self.jobs.list()}
        match = re.fullmatch(r"/jobs/([0-9a-f]{32})(/result)?", path)
        if match and method == "GET":
            job = self.jobs.get(match[1])
            if match[2]:
                status = 200 if job["result"] is not None else 504 if job["status"] == "timed_out" else 202
                return status, {"ok": job["status"] == "completed", "job": job}
            return 200, {"ok": True, "job": job}
        if match and method == "DELETE" and not match[2]:
            with self.jobs.lock:
                job = self.jobs.get(match[1])
                (self.jobs.directory / (job["id"] + ".json")).unlink()
                (self.jobs.results / (job["id"] + ".json")).unlink(missing_ok=True)
            return 200, {"ok": True, "deleted": job["id"],
                         "script_may_still_be_running": job["status"] in ("submitted", "timed_out")}
        if method == "POST" and path == "/instances/launch":
            value = self.backend.launch(body.get("new_instance", False))
            if "place_id" in body:
                return self.join({**body, "port": value["instance"]["port"]})
            return 200, value
        if method == "POST" and path == "/instances/stop":
            return 200, self.backend.stop(port, body.get("force", False))
        if method == "POST" and path == "/join":
            return self.join(body)
        if method == "POST" and path == "/editor/open":
            return 200, self.backend.open_editor()
        if method == "GET" and path == "/files":
            return 200, self.files.read(query.get("area", "workspace"), query.get("path", ""))
        if method == "PUT" and path == "/files":
            return 200, self.files.put(body)
        if method == "DELETE" and path == "/files":
            return 200, self.files.delete(query.get("area", "workspace"), query.get("path"))
        if method == "GET" and path == "/settings":
            return 200, self.backend.settings(port)
        if method == "POST" and path == "/settings":
            return 202, self.backend.setting(body.get("name"), body.get("enabled"), port)
        if method == "POST" and path == "/shutdown":
            timer = threading.Timer(.1, self.shutdown)
            timer.daemon = True
            timer.start()
            return 200, {"ok": True, "stopping": "opom", "players_stopped": False}
        raise APIError(404, "not_found", "See /docs or /openapi.json for available endpoints")
