# whoisauto

A local control panel that drives the [opom](../vendor/opom) Opiumware API from a browser:
install the executor into an opom root, start the API on a port that is actually free,
paste an account token, join a place, run a script, and watch the Player status.

```
whoisauto/
├── vendor/opom/          vendored opom checkout (commit in VENDORED_COMMIT)
├── whoisauto/            this package: config, installer, service, roblox, web
│   └── static/           single-page UI (no build step)
├── installers/           drop Opiumware/Roblox artifacts here (gitignored)
├── runtime/              generated: opom root, state, logs, token (gitignored)
└── tests/                pytest suite, runs on Linux and macOS
```

## Requirements

- macOS for anything that talks to the Player (opom's server refuses to start elsewhere;
  the panel itself runs anywhere, which is how the test suite works on Linux).
- Python 3.11+.
- An Opiumware build and an Opiumware-patched `Roblox.app`. whoisauto does not download
  either of them.

## Quick start

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# 1. put artifacts in place
cp -R /path/to/Opiumware.app installers/
cp -R /path/to/Roblox.app    installers/     # must contain Contents/Resources/libOpiumware.dylib

# 2. install into the opom root and open Opiumware once so it self-installs
python -m whoisauto install --launch

# 3. start the panel
python -m whoisauto serve            # http://127.0.0.1:8080
```

`install` accepts `--source` / `--roblox-source` (a `.app`, `.dmg`, `.zip`, or an
`https://` URL) instead of dropping files into `installers/`.

## How the pieces fit

**Installer** (`whoisauto/installer.py`) assembles the layout opom expects:

```
runtime/opom-root/Apps/Opiumware.app
runtime/opom-root/Apps/Roblox.app        # verified to carry libOpiumware.dylib
runtime/opom-root/Data/workspace
```

A bundle is verified *before* it is copied, so a stock Roblox app is rejected without
leaving a broken half-install. `--launch` opens Opiumware once, which is what lets it
finish its own first-run setup.

**Service** (`whoisauto/service.py`) owns the opom process. Port 8765 is a common
local-dev port, so instead of letting `opom serve` die on a bind error the panel:

1. reuses an opom already answering `/health` (recorded port first, then 8765–8790),
2. otherwise picks the next free port in that range and records it in
   `runtime/state/opom-port.json`,
3. waits for `/health` to report the *child's* pid before declaring success.

**Account** (`whoisauto/roblox.py`) normalises the pasted `.ROBLOSECURITY` value
(prefixes and quotes are stripped), validates it against `users.roblox.com`, and stores
it in `runtime/state/account.json` with `0600` permissions. It is used for account
verification and authenticated place/server queries. It is never logged, never returned
by the API, and never written into git.

**Join / Execute / Players** proxy opom's `/join`, `/check`, `/execute`, `/instances`
and `/runtime`. Join waits for the place id to match and the client to report
`connected`; execute can syntax-check first via the compiled Luau checker.

## Panel API

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/api/state` | platform, service status, install state, account |
| POST | `/api/install` | `{editor_source, roblox_source, launch}` |
| POST | `/api/service/start` \| `/stop` | opom lifecycle |
| GET/POST/DELETE | `/api/account` | read, verify+save, clear the token |
| POST | `/api/join` | `{place_id, game_instance_id?, wait?}` |
| POST | `/api/check` \| `/api/execute` | `{script, wait?, timeout?, check?}` |
| GET | `/api/players` | instances + runtime, optional `?place_id=` |

The panel binds `127.0.0.1` only.

## CLI

```sh
python -m whoisauto serve [--port 8080] [--opom-port 8765] [--no-browser]
python -m whoisauto install [--source PATH|URL] [--roblox-source PATH|URL] [--launch]
python -m whoisauto start | stop | status
```

## Tests and CI

```sh
python -m pytest -q
```

The suite uses a fake opom server, so it exercises port reuse, proxying, token storage
and the install verification without needing a Player. `.github/workflows/macos.yml`
runs it on `macos-latest` for Python 3.11 and 3.13, then smoke-tests the real opom
server and the panel on the runner.

## Notes

- `runtime/` and `installers/` are gitignored; the token lives only in `runtime/state/`.
- The vendored opom keeps its own MIT license; see `vendor/opom/LICENSE`.
- `vendor/opom/VENDORED_COMMIT` records the upstream commit the copy came from.

## Live panel over cloudflared

`.github/workflows/live.yml` boots a `macos-latest` runner, installs the apps (when you
pass download URLs), starts opom and the panel, and publishes it through a cloudflared
quick tunnel. It prints the URL and a generated access token to the job summary.

Because the tunnel is public, the panel refuses every `/api/*` request unless the
request carries `Authorization: Bearer <token>` or the `whoisauto_token` cookie that
`/?token=<token>` sets. `WHOISAUTO_ACCESS_TOKEN` enables this; leave it unset for
localhost-only use.

The job keeps the panel alive for the requested number of minutes and dies with the
runner — it is not a permanent deployment.
