import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest


REPO = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name != "posix", reason="CI startup uses a POSIX shell")
@pytest.mark.parametrize("failed_start", [False, True])
def test_panel_startup_checks_process_and_auth_before_tunnel(tmp_path, failed_start):
    commands = tmp_path / "bin"
    commands.mkdir()
    python = commands / "python"
    python.write_text(
        '#!/bin/sh\n'
        + ('if [ "$1" = "-u" ]; then exit 23; fi\n' if failed_start else '')
        + f'exec "{sys.executable}" "$@"\n'
    )
    python.chmod(0o755)
    tunnel_marker = tmp_path / "tunnel-started"
    cloudflared = commands / "cloudflared"
    cloudflared.write_text(
        '#!/bin/sh\n'
        'touch "$TEST_TUNNEL_MARKER"\n'
        'echo https://whoisauto-test.trycloudflare.com >&2\n'
        'exec sleep 60\n'
    )
    cloudflared.chmod(0o755)
    runtime = tmp_path / "runtime"
    state = runtime / "state"
    state.mkdir(parents=True)
    (state / "panel.json").write_text(json.dumps({"pid": -1, "port": 12345}))
    environment = {
        **os.environ, "PATH": str(commands) + os.pathsep + os.environ["PATH"],
        "PYTHONPATH": str(REPO), "WHOISAUTO_RUNTIME": str(runtime),
        "WHOISAUTO_INSTALLERS": str(tmp_path / "installers"),
        "OPOM_ROOT": str(tmp_path / "apps"), "OPOM_WORKSPACE": str(tmp_path / "workspace"),
        "OPOM_ROBLOX_APP": str(tmp_path / "Roblox.app"),
        "OPOM_EDITOR_APP": str(tmp_path / "Opiumware.app"),
        "GITHUB_ENV": str(tmp_path / "github-env"), "TEST_TUNNEL_MARKER": str(tunnel_marker),
    }
    process = subprocess.Popen(
        ["bash", str(REPO / "scripts" / "ci" / "start_panel.sh")],
        cwd=tmp_path, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
    )
    try:
        output, errors = process.communicate(timeout=15)
        if failed_start:
            assert process.returncode != 0, output + errors
            assert not tunnel_marker.exists()
            assert not (tmp_path / "panel-endpoint.json").exists()
        else:
            assert process.returncode == 0, output + errors
            assert tunnel_marker.exists()
            endpoint = tmp_path / "panel-endpoint.json"
            record = json.loads(endpoint.read_text())
            assert record["panel_url"] == "https://whoisauto-test.trycloudflare.com"
            assert record["access_token"]
            assert endpoint.stat().st_mode & 0o777 == 0o600
            assert "PANEL_PORT=" in (tmp_path / "github-env").read_text()
    finally:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
