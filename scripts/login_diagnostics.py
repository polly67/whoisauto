"""Read only redacted observation records and cookie format flags on the runner."""
import json
from pathlib import Path
import re
import sys
from whoisauto import roblox
cookie = roblox.normalize_token(json.loads((Path(sys.argv[1]) / "account.json").read_text())["token"])
print("COOKIE_FORMAT " + json.dumps({"length": len(cookie), "has_expected_warning": cookie.startswith("_|WARNING:"),
    "has_semicolon": ";" in cookie, "has_encoded_characters": bool(re.search(r"%[0-9A-Fa-f]{2}", cookie)),
    "has_quotes": "\"" in cookie or "'" in cookie, "has_cookie_prefix": "ROBLOSECURITY=" in cookie}))
for line in Path("/tmp/panel.log").read_text(errors="replace").splitlines():
    if line.startswith("WHOISAUTO_AUTH_DIAGNOSTIC "):
        print(line)
