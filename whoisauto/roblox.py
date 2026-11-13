"""Roblox account token handling and public place lookups.

The token is the ``.ROBLOSECURITY`` value from an already signed-in browser
session. It is used for two things only: confirming which account is signed in,
and authenticated place/server queries. It never leaves this machine except to
roblox.com, and it is stored with 0600 permissions.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

USER_AGENT = "whoisauto/0.1 (+local control panel)"
TIMEOUT = 10


class RobloxError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 0):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)


def normalize_token(raw: str) -> str:
    token = (raw or "").strip().strip('"').strip("'")
    for prefix in (".ROBLOSECURITY=", "ROBLOSECURITY="):
        if token.startswith(prefix):
            token = token[len(prefix):]
    token = token.strip()
    if not token:
        raise RobloxError("invalid_token", "Paste the .ROBLOSECURITY value")
    if any(character.isspace() for character in token):
        raise RobloxError("invalid_token", "The token contains whitespace; paste only the cookie value")
    return token


def mask(token: str) -> str:
    if len(token) <= 12:
        return "*" * len(token)
    return f"{token[:6]}...{token[-4:]} ({len(token)} chars)"


def _get(url: str, token: str | None = None) -> dict:
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if token:
        headers["Cookie"] = f".ROBLOSECURITY={token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise RobloxError("unauthorized", "Roblox rejected the token; sign in again and re-copy it",
                              error.code) from None
        raise RobloxError("roblox_http_error", f"Roblox returned HTTP {error.code}", error.code) from None
    except (urllib.error.URLError, TimeoutError) as error:
        raise RobloxError("roblox_unreachable", f"Could not reach Roblox: {error}") from None
    except ValueError:
        raise RobloxError("roblox_bad_response", "Roblox returned a non-JSON response") from None


def validate_token(token: str) -> dict:
    token = normalize_token(token)
    user = _get("https://users.roblox.com/v1/users/authenticated", token)
    if not isinstance(user, dict) or "id" not in user:
        raise RobloxError("unauthorized", "Roblox did not return an account for this token", 401)
    return {"id": user["id"], "name": user.get("name"), "display_name": user.get("displayName"),
            "masked": mask(token)}


def universe_for_place(place_id: int) -> int:
    payload = _get(f"https://apis.roblox.com/universes/v1/places/{int(place_id)}/universe")
    universe = payload.get("universeId")
    if not universe:
        raise RobloxError("place_not_found", f"No universe for place {place_id}")
    return int(universe)


def place_details(place_id: int) -> dict:
    universe = universe_for_place(place_id)
    payload = _get(f"https://games.roblox.com/v1/games?universeIds={universe}")
    entries = payload.get("data") or []
    game = entries[0] if entries else {}
    return {
        "place_id": int(place_id),
        "universe_id": universe,
        "name": game.get("name"),
        "creator": (game.get("creator") or {}).get("name"),
        "playing": game.get("playing"),
        "visits": game.get("visits"),
        "max_players": game.get("maxPlayers"),
        "created": game.get("created"),
        "updated": game.get("updated"),
        "url": f"https://www.roblox.com/games/{place_id}",
    }


def public_servers(place_id: int, token: str | None = None, limit: int = 50) -> dict:
    payload = _get(f"https://games.roblox.com/v1/games/{int(place_id)}/servers/Public"
                   f"?limit={max(1, min(int(limit), 100))}&sortOrder=Desc", token)
    servers = []
    for entry in payload.get("data") or []:
        servers.append({
            "id": entry.get("id"),
            "playing": entry.get("playing"),
            "max_players": entry.get("maxPlayers"),
            "fps": entry.get("fps"),
            "ping": entry.get("ping"),
        })
    return {"place_id": int(place_id), "servers": servers,
            "total_players": sum(s["playing"] or 0 for s in servers)}
