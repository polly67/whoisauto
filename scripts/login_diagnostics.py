"""Temporary, redacted observation of the existing panel login requests."""
import json
from pathlib import Path
import sys
import urllib.request
OBSERVER = '\nimport json, io, re, sys, urllib.request, urllib.error\nfrom http.cookies import SimpleCookie\nfrom whoisauto import roblox\noriginal = urllib.request.urlopen\n\ndef observe(request, *args, **kwargs):\n    if not hasattr(request, "full_url") or not request.full_url.startswith(("https://users.roblox.com/", "https://auth.roblox.com/")):\n        return original(request, *args, **kwargs)\n    stage = "launch_ticket" if "authentication-ticket" in request.full_url else "account_validation"\n    cookie_header = request.get_header("Cookie", "")\n    cookie = cookie_header.removeprefix(".ROBLOSECURITY=")\n    error = None\n    try:\n        response = original(request, *args, **kwargs)\n    except urllib.error.HTTPError as value:\n        response, error = value, value\n    details = []\n    if error:\n        raw = error.read(8192)\n        error.fp = io.BytesIO(raw)\n        try:\n            for item in json.loads(raw).get("errors", [])[:4]:\n                message = str(item.get("message", ""))[:500].replace(cookie, "[REDACTED]")\n                message = re.sub(r"eyJ[A-Za-z0-9._=-]{20,}", "[REDACTED]", message)\n                if cookie[:32] in message or cookie[-32:] in message:\n                    message = "Provider message withheld"\n                details.append({"code": item.get("code"), "message": message})\n        except (ValueError, TypeError, AttributeError):\n            pass\n    refreshed = False\n    cleared = False\n    for header in response.headers.get_all("Set-Cookie", []):\n        values = SimpleCookie()\n        try: values.load(header)\n        except Exception: continue\n        if ".ROBLOSECURITY" in values:\n            new = values[".ROBLOSECURITY"].value\n            refreshed = refreshed or bool(new and new != cookie)\n            cleared = cleared or not bool(new)\n    print("WHOISAUTO_AUTH_DIAGNOSTIC " + json.dumps({\n        "stage": stage, "status": response.code, "errors": details,\n        "csrf_received": bool(response.headers.get("x-csrf-token")),\n        "csrf_sent": bool(request.get_header("X-csrf-token")),\n        "cookie_refreshed": refreshed, "cookie_cleared": cleared,\n        "challenge_received": bool(response.headers.get("rblx-challenge-id"))\n    }), flush=True)\n    if error: raise error\n    return response\nurllib.request.urlopen = observe\nfrom whoisauto.cli import main\nraise SystemExit(main())\n'

import os, shlex, signal, subprocess, time
repo = Path.cwd()
state = Path(sys.argv[1])
endpoint = json.loads((repo / "panel-endpoint.json").read_text())
record = json.loads((state / "panel.json").read_text())
port, pid = record["port"], record["pid"]
headers = {"Authorization": "Bearer " + endpoint["access_token"]}
with urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:" + str(port) + "/api/state", headers=headers), timeout=20) as response:
    info = json.load(response)
command = subprocess.check_output(["ps", "-p", str(pid), "-o", "command="], text=True).strip()
if "-m whoisauto serve" not in command:
    raise SystemExit("Unexpected panel process; observation was not enabled")
executable = shlex.split(command)[0]
if not Path(executable).is_file():
    raise SystemExit("Panel interpreter could not be identified")
observer = Path("/tmp/whoisauto-auth-observer.py")
observer.write_text(OBSERVER)
observer.chmod(0o600)
environment = {**os.environ, "PYTHONPATH": str(repo), "WHOISAUTO_ACCESS_TOKEN": endpoint["access_token"],
    "OPOM_ROOT": info["paths"]["root"], "OPOM_ROBLOX_APP": info["install"]["roblox"]["path"],
    "OPOM_EDITOR_APP": info["install"]["editor"]["path"], "OPOM_WORKSPACE": info["paths"]["root"] + "/workspace",
    "WHOISAUTO_RUNTIME": str(state.parent), "WHOISAUTO_INSTALLERS": info["paths"]["installers"]}
os.kill(pid, signal.SIGTERM)
for _ in range(50):
    try: os.kill(pid, 0)
    except ProcessLookupError: break
    time.sleep(0.1)
with Path("/tmp/panel.log").open("w") as log:
    child = subprocess.Popen([executable, "-u", str(observer), "serve", "--port", str(port), "--runtime", str(state.parent), "--no-browser"],
        cwd=repo, env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
for _ in range(60):
    try:
        current = json.loads((state / "panel.json").read_text())
        if current.get("pid") == child.pid and current.get("port") == port:
            print("AUTH_OBSERVATION_READY", flush=True)
            break
    except (OSError, ValueError): pass
    if child.poll() is not None:
        raise SystemExit("Observation panel failed to start")
    time.sleep(0.5)
else: raise SystemExit("Observation panel did not start in time")
for _ in range(24):
    lines = Path("/tmp/panel.log").read_text(errors="replace").splitlines()
    events = [line for line in lines if line.startswith("WHOISAUTO_AUTH_DIAGNOSTIC ")]
    if events:
        for line in events: print(line, flush=True)
        if any('"stage": "launch_ticket"' in line or '"status": 401' in line for line in events): break
    time.sleep(10)
