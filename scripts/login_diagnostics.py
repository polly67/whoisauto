"""Compare account verification with normal launch-ticket request headers on the runner."""
import json
from pathlib import Path
import sys
import urllib.request
import urllib.error
from whoisauto import roblox
cookie = roblox.normalize_token(json.loads((Path(sys.argv[1]) / "account.json").read_text())["token"])
for line in Path("/tmp/panel.log").read_text(errors="replace").splitlines():
    if line.startswith("WHOISAUTO_AUTH_DIAGNOSTIC "):
        print(line)
try:
    roblox.validate_token(cookie)
    print("CURRENT_ACCOUNT_VALID")
except roblox.RobloxError as error:
    print("CURRENT_ACCOUNT_INVALID", error.status)
for variant in ("normal-client", "game-referer", "negotiated-game-referer"):
    path = "/v1/authentication-ticket/"
    headers = {"Cookie": ".ROBLOSECURITY=" + cookie, "Content-Type": "application/json", "Accept": "application/json",
               "User-Agent": roblox.USER_AGENT, "Origin": "https://www.roblox.com", "Referer": "https://www.roblox.com/"}
    if variant != "normal-client":
        headers["Referer"] = "https://www.roblox.com/games"
    if variant == "negotiated-game-referer":
        headers["RBXAuthenticationNegotiation"] = "1"
    for attempt in range(2):
        request=urllib.request.Request("https://auth.roblox.com"+path,data=b"{}",headers=headers,method="POST")
        try: response=urllib.request.urlopen(request,timeout=15)
        except urllib.error.HTTPError as error: response=error
        with response:
            codes=[]
            try:
                errors=json.loads(response.read(8192)).get("errors",[])
                codes=[value.get("code") for value in errors if isinstance(value,dict)]
            except (ValueError,AttributeError,TypeError): pass
            csrf=response.headers.get("x-csrf-token")
            ticket=bool(response.headers.get("rbx-authentication-ticket"))
            print("TICKET_PROBE " + json.dumps({"variant":variant,"path":path,"attempt":attempt+1,"status":response.code,"codes":codes,
                "final_url":response.geturl().split("?")[0],"csrf":bool(csrf),"ticket":ticket}))
            if response.code == 429 or response.headers.get("rblx-challenge-id"): raise SystemExit(0)
            if ticket: raise SystemExit(0)
            if attempt == 0 and response.code == 403 and csrf:
                headers["X-CSRF-TOKEN"]=csrf
                continue
            break
