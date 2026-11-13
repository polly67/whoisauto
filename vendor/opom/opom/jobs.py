from datetime import datetime, timezone
import hashlib
import json
import re
import threading
import time
import uuid

from .core import APIError, RESULT_LIMIT, SCRIPT_LIMIT, lua_string, text, write_json

WRAPPER = r'''
local _opom_json = game:GetService("HttpService")
local _opom_write = writefile
local _opom_tostring, _opom_type, _opom_pairs = tostring, typeof, pairs
local _opom_started = os.clock()
local _opom_output = {}
local _opom_truncated = false
local _opom_budget = 262144
local _opom_nodes = 0
local function _opom_pack(...) return {n=select("#",...),...} end
local function _opom_text(value)
    local ok, text = pcall(_opom_tostring,value)
    if not ok then return "<tostring failed>" end
    local limit=math.min(16384,_opom_budget)
    if #text > limit then text=string.sub(text,1,limit);_opom_truncated=true end
    _opom_budget=math.max(0,_opom_budget-#text)
    local valid=pcall(_opom_json.JSONEncode,_opom_json,text)
    if not valid then
        text="hex:"..string.gsub(text,".",function(c)return string.format("%02x",string.byte(c))end)
    end
    return text
end
local function _opom_capture(level,...)
    if #_opom_output>=128 then _opom_truncated=true;return end
    local args=_opom_pack(...);local parts={}
    if args.n>64 then _opom_truncated=true end
    for i=1,math.min(args.n,64) do parts[i]=_opom_text(args[i]) end
    table.insert(_opom_output,{level=level,text=table.concat(parts,"\t")})
end
local function _opom_value(value,depth,seen)
    local kind=_opom_type(value)
    local result={type=kind,text=_opom_text(value)}
    _opom_nodes+=1
    if _opom_nodes>512 then result.truncated=true;return result end
    if kind=="string" then
        local limit=math.min(65536,_opom_budget)
        if #value>limit then value=string.sub(value,1,limit);result.truncated=true end
        _opom_budget=math.max(0,_opom_budget-#value)
        local valid=pcall(_opom_json.JSONEncode,_opom_json,value)
        if valid then result.value=value else result.encoding="hex";result.value=string.gsub(value,".",function(c)return string.format("%02x",string.byte(c))end) end
    elseif kind=="boolean" then result.value=value
    elseif kind=="number" then if value==value and math.abs(value)~=math.huge then result.value=value end
    elseif kind=="table" and depth<4 and not seen[value] then
        seen[value]=true;local children={};local count=0
        for k,v in _opom_pairs(value) do
            count+=1;if count>128 then result.truncated=true;break end
            children[_opom_text(k)]=_opom_value(v,depth+1,seen)
        end
        result.value=children
    end
    return result
end
local function _opom_user(...)
    local print=function(...)_opom_capture("print",...)end
    local warn=function(...)_opom_capture("warn",...)end
__SOURCE__
end
local _opom_values=_opom_pack(pcall(_opom_user))
local _opom_result={job_id=__ID__,ok=_opom_values[1],output=_opom_output,returns={},
    place_id=game.PlaceId,duration=os.clock()-_opom_started,output_truncated=_opom_truncated}
if _opom_result.ok then
    for i=2,math.min(_opom_values.n,65) do
        table.insert(_opom_result.returns,_opom_value(_opom_values[i],0,{}))
    end
else
    _opom_result.kind="runtime";_opom_result.error=_opom_text(_opom_values[2])
end
_opom_result.output_truncated=_opom_truncated
local _opom_ok,_opom_encoded=pcall(_opom_json.JSONEncode,_opom_json,_opom_result)
if not _opom_ok then
    _opom_encoded=_opom_json:JSONEncode({job_id=__ID__,ok=false,kind="serialization",error=_opom_text(_opom_encoded)})
end
_opom_write(__FILE__,_opom_encoded)
'''


class Jobs:
    def __init__(self, config, backend, compiler):
        self.config, self.backend, self.compiler = config, backend, compiler
        self.lock = threading.RLock()
        self.directory = config.state / "jobs"
        self.results = config.workspace / ".opom/results"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.results.mkdir(parents=True, exist_ok=True)

    def submit(self, source, port=None, timeout=30, capture=True):
        source = text(source, "script")
        if type(timeout) not in (int, float) or not .1 <= timeout <= 300:
            raise APIError(400, "invalid_timeout", "timeout must be from 0.1 to 300 seconds")
        checked = self.compiler.check(source)
        if not checked["ok"]:
            raise APIError(400, "syntax", checked.get("error", "Invalid Luau source"))
        job_id = uuid.uuid4().hex
        relative = f".opom/results/{job_id}.json"
        expanded = WRAPPER.replace("__ID__", lua_string(job_id)).replace("__FILE__", lua_string(relative)).replace("__SOURCE__", source)
        if not capture:
            expanded = expanded.replace('    local print=function(...)_opom_capture("print",...)end\n', '')
            expanded = expanded.replace('    local warn=function(...)_opom_capture("warn",...)end\n', '')
        job = {"id": job_id, "status": "submitted", "submitted_at": datetime.now(timezone.utc).isoformat(),
               "submitted_timestamp": time.time(), "timeout": timeout, "script_bytes": len(source.encode()),
               "source_sha256": hashlib.sha256(source.encode()).hexdigest(), "result": None}
        job["capture"] = capture
        receipt = self.backend.send(expanded, port)
        job.update(instance=receipt["instance"], compressed_bytes=receipt["compressed_bytes"])
        with self.lock:
            write_json(self.directory / (job_id + ".json"), job)
        return self.get(job_id)

    def get(self, job_id):
        if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise APIError(400, "invalid_job_id", "Use a job ID returned by opom")
        path = self.directory / (job_id + ".json")
        with self.lock:
            if not path.is_file():
                raise APIError(404, "job_not_found", "Job does not exist")
            job = json.loads(path.read_text())
            result_path = self.results / (job_id + ".json")
            result = None
            if result_path.is_file() and job["result"] is None:
                if result_path.stat().st_size > RESULT_LIMIT:
                    result = {"job_id": job_id, "ok": False, "kind": "result_limit", "error": "Result exceeds 2 MiB"}
                else:
                    try:
                        result = json.loads(result_path.read_text())
                    except (OSError, ValueError):
                        pass  # Native writefile can be observed before its write completes.
            if result is not None:
                if not isinstance(result, dict) or result.get("job_id") != job_id or type(result.get("ok")) is not bool:
                    result = {"ok": False, "kind": "invalid_result", "error": "Native result has invalid metadata"}
                job["completed_after_timeout"] = job["status"] == "timed_out"
                job["result"] = result
                job["status"] = "completed" if result.get("ok") else "failed"
                job["completed_at"] = datetime.now(timezone.utc).isoformat()
                write_json(path, job)
            elif job["result"] is None and time.time() - job["submitted_timestamp"] > job["timeout"]:
                job["status"] = "timed_out"
                job["script_may_still_be_running"] = True
                write_json(path, job)
            return job

    def wait(self, job_id, seconds):
        if type(seconds) not in (int, float) or not 0 <= seconds <= 30:
            raise APIError(400, "invalid_wait", "wait must be from 0 to 30 seconds")
        deadline = time.monotonic() + seconds
        while True:
            job = self.get(job_id)
            if job["status"] in ("completed", "failed", "timed_out") or time.monotonic() >= deadline:
                return job
            time.sleep(.05)

    def list(self):
        paths = sorted(self.directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:100]
        return [self.get(path.stem) for path in paths]
