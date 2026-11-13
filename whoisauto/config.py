"""Paths, ports and small filesystem helpers.

Everything the panel writes at runtime lives under ``runtime/`` (gitignored):
the Opiumware/Roblox apps, the opom state, logs and the account record.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import socket
import tempfile

REPO = Path(__file__).resolve().parents[1]
OPOM_DIR = REPO / "vendor" / "opom"
DEFAULT_OPOM_PORT = 8765
DEFAULT_WEB_PORT = 8080
PORT_SPAN = 25


class ConfigError(RuntimeError):
    pass


def _path(value: str | os.PathLike | None, default: Path) -> Path:
    return Path(value).expanduser().resolve() if value else default


@dataclass(frozen=True)
class Paths:
    repo: Path
    runtime: Path
    state: Path
    root: Path
    workspace: Path
    installers: Path

    @property
    def opom_dir(self) -> Path:
        return OPOM_DIR

    @property
    def opom_state(self) -> Path:
        return self.state / "opom"

    @property
    def editor_app(self) -> Path:
        return self.root / "Apps" / "Opiumware.app"

    @property
    def roblox_app(self) -> Path:
        return self.root / "Apps" / "Roblox.app"

    @property
    def account_file(self) -> Path:
        return self.state / "account.json"

    @property
    def manifest_file(self) -> Path:
        return self.state / "install.json"

    @property
    def port_file(self) -> Path:
        return self.state / "opom-port.json"

    @property
    def log_file(self) -> Path:
        return self.state / "opom.log"

    def ensure(self) -> None:
        for path in (self.runtime, self.state, self.opom_state, self.root,
                     self.workspace, self.installers):
            path.mkdir(parents=True, exist_ok=True)

    def env(self, port: int) -> dict:
        """Environment for the opom child process."""
        return {
            "OPOM_ROOT": str(self.root),
            "OPOM_STATE": str(self.opom_state),
            "OPOM_WORKSPACE": str(self.workspace),
            "OPOM_ROBLOX_APP": str(self.roblox_app),
            "OPOM_EDITOR_APP": str(self.editor_app),
            "PYTHONPATH": str(self.opom_dir),
            "PYTHONUNBUFFERED": "1",
            "WHOISAUTO_PORT": str(port),
        }

    @classmethod
    def load(cls, runtime: str | os.PathLike | None = None) -> "Paths":
        runtime_path = _path(runtime or os.environ.get("WHOISAUTO_RUNTIME"), REPO / "runtime")
        state = runtime_path / "state"
        root = _path(os.environ.get("OPOM_ROOT"), runtime_path / "opom-root")
        workspace = _path(os.environ.get("OPOM_WORKSPACE"), root / "Data" / "workspace")
        installers = _path(os.environ.get("WHOISAUTO_INSTALLERS"), REPO / "installers")
        return cls(REPO, runtime_path, state, root, workspace, installers)


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def find_free_port(preferred: int, span: int = PORT_SPAN, host: str = "127.0.0.1") -> int:
    """First free port at or after ``preferred``.

    Opom's default 8765 is a popular port for other local dev servers, so a
    hard bind failure would otherwise surface as a dead panel.
    """
    if not 1 <= preferred <= 65535:
        raise ConfigError(f"port must be from 1 to 65535, got {preferred}")
    last = min(preferred + span, 65535)
    for port in range(preferred, last + 1):
        if port_free(port, host):
            return port
    raise ConfigError(f"no free port in {preferred}-{last}; pass --opom-port explicitly")


def read_json(path: Path, default=None):
    try:
        with path.open("rb") as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return default


def write_json(path: Path, value, mode: int | None = None) -> None:
    """Atomic write; ``mode`` is applied before the rename for secret files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".whoisauto-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
