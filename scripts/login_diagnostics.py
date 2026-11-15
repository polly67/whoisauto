"""Inspect Roblox errors on the runner without exporting its account cookie."""
import io
import json
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request

from whoisauto import roblox

record = json.loads((Path(sys.argv[1]) / "account.json").read_text())
cookie = roblox.normalize_token(record["token"])
original = urllib.request.urlopen

def inspect(request, *args, **kwargs):
    stage = "launch_ticket" if "authentication-ticket" in request.full_url else "account_validation"
    try:
        response = original(request, *args, **kwargs)
    except urllib.error.HTTPError as error:
        raw = error.read(8192)
        error.fp = io.BytesIO(raw)
        details = []
        try:
            payload = json.loads(raw)
            for item in payload.get("errors", [])[:4]:
                message = str(item.get("message", ""))[:500].replace(cookie, "[REDACTED]")
                message = re.sub(r"eyJ[A-Za-z0-9._=-]{20,}", "[REDACTED]", message)
                if cookie[:32] in message or cookie[-32:] in message:
                    message = "Provider message withheld"
                details.append({"code": item.get("code"), "message": message})
        except (ValueError, AttributeError, TypeError):
            pass
        print(json.dumps({"stage": stage, "status": error.code, "errors": details,
                          "csrf_received": bool(error.headers.get("x-csrf-token")),
                          "csrf_sent": bool(request.get_header("X-csrf-token")),
                          "challenge_received": bool(error.headers.get("rblx-challenge-id"))}), flush=True)
        raise
    print(json.dumps({"stage": stage, "status": response.status,
                      "ticket_received": bool(response.headers.get("rbx-authentication-ticket"))}), flush=True)
    return response

urllib.request.urlopen = inspect
try:
    roblox.validate_token(cookie)
    roblox.authentication_ticket(cookie)
    print("Launch ticket obtained; credentials were not exported.")
except roblox.RobloxError as error:
    print(json.dumps({"result": error.code, "status": error.status}))
