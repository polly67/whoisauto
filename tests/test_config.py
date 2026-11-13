import json
import os
from pathlib import Path
import socket

import pytest

from whoisauto.config import ConfigError, Paths, find_free_port, port_free, read_json, write_json


def test_paths_derive_opom_layout(paths):
    assert paths.editor_app == paths.root / "Apps" / "Opiumware.app"
    assert paths.roblox_app == paths.root / "Apps" / "Roblox.app"
    assert paths.opom_state == paths.state / "opom"
    assert paths.port_file == paths.state / "opom-port.json"
    env = paths.env(9000)
    assert env["OPOM_ROOT"] == str(paths.root)
    assert env["OPOM_STATE"] == str(paths.opom_state)
    assert env["PYTHONPATH"] == str(paths.opom_dir)


def test_find_free_port_skips_busy_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        busy = listener.getsockname()[1]
        assert not port_free(busy)
        assert find_free_port(busy) != busy


def test_find_free_port_rejects_out_of_range():
    with pytest.raises(ConfigError):
        find_free_port(0)


def test_write_json_is_atomic_and_private(paths):
    target = paths.state / "secret.json"
    write_json(target, {"token": "x"}, mode=0o600)
    assert read_json(target) == {"token": "x"}
    assert oct(target.stat().st_mode & 0o777) == oct(0o600)
    assert not list(paths.state.glob(".whoisauto-*"))


def test_read_json_falls_back(paths):
    assert read_json(paths.state / "missing.json", {"ok": False}) == {"ok": False}
    broken = paths.state / "broken.json"
    broken.write_text("{not json")
    assert read_json(broken, None) is None


def test_paths_load_honours_runtime(tmp_path, monkeypatch):
    monkeypatch.delenv("OPOM_ROOT", raising=False)
    monkeypatch.delenv("OPOM_WORKSPACE", raising=False)
    value = Paths.load(tmp_path / "rt")
    assert value.runtime == (tmp_path / "rt").resolve()
    assert value.root == (tmp_path / "rt" / "opom-root").resolve()
    assert value.workspace == value.root / "Data" / "workspace"


def test_paths_load_honours_app_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("OPOM_ROBLOX_APP", str(tmp_path / "Roblox.app"))
    monkeypatch.setenv("OPOM_EDITOR_APP", str(tmp_path / "Opiumware.app"))
    value = Paths.load(tmp_path / "rt")
    assert value.roblox_app == (tmp_path / "Roblox.app").resolve()
    assert value.editor_app == (tmp_path / "Opiumware.app").resolve()


def test_paths_fall_back_to_applications(tmp_path, monkeypatch):
    monkeypatch.delenv("OPOM_ROBLOX_APP", raising=False)
    monkeypatch.delenv("OPOM_EDITOR_APP", raising=False)
    monkeypatch.delenv("OPOM_ROOT", raising=False)
    value = Paths.load(tmp_path / "rt")
    assert value.roblox_app == Path("/Applications/Roblox.app")
    assert value.editor_app == Path("/Applications/Opiumware.app")
