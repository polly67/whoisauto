# opom

Local macOS HTTP API for Opiumware Player execution and control. It provides
**25 API operations**, captured jobs/results, generic calls to available
executor functions, an OpenAPI reference and an interactive API explorer.

## Requirements and setup

- macOS with an active GUI session and a working modified Opiumware Roblox.
- Python 3.11+.
- CMake, Git and a C++ compiler for the one-time Luau syntax-checker build.
- Apple's Swift toolchain for the first `POST /join` call.

The repository contains the API and its helpers. Install Roblox and Opiumware
separately. Use an already logged-in Player account for joining.

Run from the repository checkout:

```sh
git clone https://github.com/polly67/opom.git
cd opom
python3 -m opom build-compiler
python3 -m opom start
```

The syntax checker builds against pinned official Luau source. To reuse an
existing checkout/build, see `python3 tools/build_compiler.py --help`.

Open **http://127.0.0.1:8765/docs** for the API explorer. The machine used for
development already has its saved installation at `/Volumes/devbox/Opiumware`.
Elsewhere, opom uses `~/Opiumware` and `/Applications` by default; override paths
with `OPOM_ROOT`, `OPOM_ROBLOX_APP`, `OPOM_EDITOR_APP`, `OPOM_WORKSPACE`,
`OPOM_AUTOEXEC`, `OPOM_STATE` or `OPOM_COMPILER`.

Optional HTTP authentication uses `OPOM_API_TOKEN` and an
`Authorization: Bearer …` header. This API token is separate from GitHub and
Roblox account credentials. Shell environment variables are read directly;
`.env.example` documents them and is not loaded automatically.

## Execute with output and results

```sh
curl http://127.0.0.1:8765/execute \
  -H 'Content-Type: application/json' \
  -d '{"script":"print(\"hello\"); return game.PlaceId, nil, true","wait":5}'
```

`wait` is optional and ranges from 0 to 30 seconds. Without it, the API returns
a job immediately. Poll `GET /jobs/{id}` or `GET /jobs/{id}/result` afterward.
The job's `result` contains `ok`, `output`, typed `returns`, `place_id`,
duration and any runtime error. Nil return positions are preserved.

Jobs transition through `submitted`, `completed`, `failed` and `timed_out`.
`timeout` ranges from 0.1 to 300 seconds and defaults to 30. A timeout does not
cancel native code. A later result can complete a timed-out job, marked
`completed_after_timeout: true`. Deleting a job forgets its history; it does not
cancel the script.

Captured execution wraps source in a function and provides local print/warn
capture. Set `capture: false` to keep the native print/warn bindings while
retaining a job/result. For unchanged source and call-stack-sensitive checks,
use **`raw: true`** on `/execute`: this sends the source directly and returns
only a submission receipt. Raw mode is appropriate for original UNC/sUNC
benchmarks that expect native print/closure behavior.

## Evaluate and call executor functions

```sh
curl http://127.0.0.1:8765/eval \
  -H 'Content-Type: application/json' \
  -d '{"expression":"workspace.ClassName","wait":5}'

curl http://127.0.0.1:8765/call \
  -H 'Content-Type: application/json' \
  -d '{"function":"crypt.hash","args":["hello","sha256"],"wait":5}'
```

`/call` accepts any available dotted function path, not only the discovery
catalog. JSON arguments become Lua values; trailing nils are preserved. Use
`method: true` for colon calls such as `game:GetService`:

```json
{"function":"game.GetService","method":true,"args":["Players"],"wait":5}
```

Instance, function and other Lua-only arguments can use a `$lua` expression:

```json
{"function":"typeof","args":[{"$lua":"workspace"}],"wait":5}
```

`/call` and `/eval` keep native print/warn bindings by default. Function calls
still run inside an RPC wrapper; use raw execution when an exact caller stack
is needed. Unsupported functions report their actual runtime error.
`GET /functions` inspects presence/type in the live Player. Presence does not
establish compatibility or a UNC/sUNC score.

## Endpoint reference

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/health`, `/status` | App availability, compiler availability and execution listeners |
| GET | `/capabilities` | Operation and function catalog |
| GET | `/instances` | Ports, PIDs and executable paths of modified Players |
| POST | `/instances/launch` | Reuse Player, or launch an isolated copy with `new_instance: true` |
| POST | `/instances/stop` | Gracefully stop one verified Player selected by `port` |
| POST | `/join` | Deliver a place URL and optionally verify the joined place |
| POST | `/editor/open` | Open Opiumware's editor |
| GET | `/runtime` | Actual place ID, game instance ID and player information |
| GET | `/functions` | Inspect available function types |
| POST | `/execute`, `/eval`, `/call` | Script/expression/function jobs |
| POST | `/execute-file` | Execute a UTF-8 file in workspace or autoexec |
| POST | `/check` | Syntax-check without execution |
| GET | `/jobs`, `/jobs/{id}`, `/jobs/{id}/result` | Job history, state and result |
| DELETE | `/jobs/{id}` | Delete job history |
| GET, PUT, DELETE | `/files` | List/read/write/delete workspace or autoexec files |
| GET, POST | `/settings` | Inspect recorded requests or submit a native setting packet |
| POST | `/shutdown` | Stop opom while leaving Players running |

`GET /docs` serves the interactive explorer and `GET /openapi.json` serves the
machine-readable reference, including request examples. Query `port` selects
the Player for GET runtime/functions/settings; JSON `port` selects it for POST
operations. Otherwise, the lowest available native port is chosen.

## Player control

```sh
curl http://127.0.0.1:8765/instances/launch \
  -H 'Content-Type: application/json' -d '{}'

curl http://127.0.0.1:8765/join \
  -H 'Content-Type: application/json' \
  -d '{"place_id":123974602339071,"wait":15}'
```

Launch waits for the native socket. Join reports `url_delivered` separately
from verified `joined`. A listener can remain open after a game disconnects;
use `/runtime` to verify active game state. A Player in the menu may not handle
executor jobs until it joins an experience.

`new_instance: true` makes an isolated app copy under `.state/players` because
the same Roblox bundle can reject a second launch. Up to eight instances are
supported. The saved copied app must already have relocatable library paths.
Opom verifies process paths before forwarding scripts or stopping Players.
`/instances/stop` defaults to SIGTERM. Add `force: true` to use SIGKILL if the
selected verified Player does not exit within five seconds.

## Files and settings

```sh
curl -X PUT http://127.0.0.1:8765/files \
  -H 'Content-Type: application/json' \
  -d '{"path":"example.luau","content":"return 42"}'

curl 'http://127.0.0.1:8765/files?path=example.luau'
```

`area` is `workspace` or `autoexec`. Paths must remain inside that directory.
Files use `utf-8` or `base64` encoding and are limited to 8 MiB. Directory reads
return up to 1,000 entries. `PUT /files` replaces the selected file; directory
deletion is not exposed.

`POST /settings` takes `name`, boolean `enabled`, and optional `port`. It sends
the native `OpiumwareSetting` packet. Setting names depend on the installed
Opiumware version; the API cannot infer whether an unknown name was applied.
`GET /settings` returns saved submission receipts with `confirmed_state: null`.
Use actual getter functions through `/call` when one exists.

## Limits, persistence and verification

Source is limited to 4 MiB. Output and table serialization are bounded: up to
128 print/warn records, 64 return values, four table levels, and a shared
serialization budget. Truncation is reported. Userdata and functions retain
their type and textual representation; they are not persistent remote handles.
For large/binary results, write a file in Player and retrieve it with `/files`.

Jobs and logs are in `.state/`; native results use the workspace's
`.opom/results/` directory. These are local artifacts and are excluded from
Git. Opom does not publish Roblox applications, account cookies or job output.

```sh
python3 -m opom status
python3 -m opom stop
python3 -m unittest discover -s tests -v
```

`--port`, `--root` and `--state` are available on CLI commands. `Start opom.command`
and `Stop opom.command` provide desktop launch/stop actions. Stopping opom keeps
Player running; stopping Player is an explicit API action.

Development verification used macOS 15.7.5 on arm64 with an Intel Opiumware
Player `0.745.0.7451137` under Rosetta. Native `identifyexecutor` reported
`OpiumwareMac`, `v2.6.0`. Captured execution, real place values and function
discovery were verified in place `123974602339071`.

GitHub CI runs API tests with fixtures on Linux. Roblox cookie login and
automatic GitHub-runner installation/provisioning are separate follow-up work;
this repository currently assumes the modified apps and an account session
are available on the macOS host.

## License

Opom's code is MIT licensed. The syntax checker links official Luau, revision
`12a6ba75883272c79b66b59815efef4c574196f6`, under its MIT license. Preserve the
upstream license when distributing a checker binary. Roblox and Opiumware are
installed separately and retain their own rights and terms.
