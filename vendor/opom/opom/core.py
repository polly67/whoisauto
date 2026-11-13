import base64
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

REPO = Path(__file__).resolve().parents[1]
SCRIPT_LIMIT = 4 * 1024 * 1024
FILE_LIMIT = 8 * 1024 * 1024
RESULT_LIMIT = 2 * 1024 * 1024


class APIError(Exception):
    def __init__(self, status, code, message, **details):
        self.status = status
        self.payload = {"ok": False, "error": code, "message": message, **details}
        super().__init__(message)


@dataclass
class Config:
    root: Path
    state: Path
    workspace: Path
    roblox: Path
    editor: Path
    compiler: Path
    token: str = ""

    @classmethod
    def load(cls, root=None, state=None):
        default_root = Path("/Volumes/devbox/Opiumware")
        if not default_root.exists():
            default_root = Path.home() / "Opiumware"
        root = Path(root or os.environ.get("OPOM_ROOT", default_root)).expanduser().resolve()
        state = Path(state or os.environ.get("OPOM_STATE", REPO / ".state")).expanduser().resolve()
        data = root / "Data" if (root / "Data").is_dir() else root
        roblox = root / "Apps/Roblox.app"
        editor = root / "Apps/Opiumware.app"
        if not roblox.exists():
            roblox = Path("/Applications/Roblox.app")
        if not editor.exists():
            editor = Path("/Applications/Opiumware.app")
        return cls(root, state,
                   Path(os.environ.get("OPOM_WORKSPACE", data / "workspace")).expanduser().resolve(),
                   Path(os.environ.get("OPOM_ROBLOX_APP", roblox)).expanduser().resolve(),
                   Path(os.environ.get("OPOM_EDITOR_APP", editor)).expanduser().resolve(),
                   Path(os.environ.get("OPOM_COMPILER", state / "opom-compiler")).expanduser().resolve(),
                   os.environ.get("OPOM_API_TOKEN", ""))

    @property
    def autoexec(self):
        data = self.root / "Data" if (self.root / "Data").is_dir() else self.root
        return Path(os.environ.get("OPOM_AUTOEXEC", data / "autoexec")).expanduser().resolve()


def text(value, name, nonempty=True):
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise APIError(400, "invalid_argument", f"{name} must be a {'nonempty ' if nonempty else ''}string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise APIError(400, "invalid_unicode", f"{name} contains invalid Unicode") from None
    return value


def lua_string(value):
    # Byte-preserving Lua quotes, including NUL and control characters. Unicode
    # stays UTF-8 instead of JSON's unsupported Lua \uXXXX escape syntax.
    return '"' + ''.join('\\' + c if c in '\\"' else f'\\{ord(c):03d}' if ord(c) < 32 else c for c in value) + '"'


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".opom-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class Compiler:
    def __init__(self, config):
        self.config = config

    def check(self, source):
        source = text(source, "script")
        if len(source.encode("utf-8")) > SCRIPT_LIMIT:
            raise APIError(413, "script_too_large", "Script exceeds 4 MiB")
        if not self.config.compiler.is_file():
            raise APIError(503, "compiler_missing", "Run python3 tools/build_compiler.py before captured execution")
        try:
            result = subprocess.run([str(self.config.compiler)], input=source, capture_output=True,
                                    text=True, timeout=20)
            checked = json.loads(result.stdout)
        except (OSError, subprocess.TimeoutExpired, ValueError) as error:
            raise APIError(502, "compiler_failed", str(error)) from None
        if result.returncode not in (0, 1) or not isinstance(checked.get("ok"), bool):
            raise APIError(502, "compiler_failed", "Compiler returned an invalid response")
        return checked


class Files:
    def __init__(self, config):
        self.config = config

    def path(self, area, relative, root_ok=False):
        if area not in ("workspace", "autoexec"):
            raise APIError(400, "invalid_area", "area must be workspace or autoexec")
        relative = text(relative, "path", nonempty=not root_ok)
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or "\x00" in relative:
            raise APIError(400, "invalid_path", "Use a relative path inside the selected area")
        root = self.config.workspace if area == "workspace" else self.config.autoexec
        root = root.resolve()
        resolved = (root / path).resolve()
        if not resolved.is_relative_to(root) or (not root_ok and resolved == root):
            raise APIError(400, "invalid_path", "Path escapes the selected area")
        return root, resolved

    def read(self, area="workspace", relative=""):
        root, path = self.path(area, relative, root_ok=True)
        if not path.exists():
            raise APIError(404, "file_not_found", "Path does not exist")
        if path.is_dir():
            return {"ok": True, "area": area, "path": relative,
                    "entries": [{"path": str(p.relative_to(root)), "directory": p.is_dir(),
                                 "bytes": p.stat().st_size if p.is_file() else None}
                                for p in sorted(path.iterdir())[:1000]]}
        if path.stat().st_size > FILE_LIMIT:
            raise APIError(413, "file_too_large", "File exceeds 8 MiB")
        data = path.read_bytes()
        try:
            content, encoding = data.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            content, encoding = base64.b64encode(data).decode(), "base64"
        return {"ok": True, "area": area, "path": relative, "encoding": encoding,
                "content": content, "bytes": len(data)}

    def put(self, body):
        area = body.get("area", "workspace")
        _, path = self.path(area, body.get("path"))
        content = text(body.get("content"), "content", nonempty=False)
        encoding = body.get("encoding", "utf-8")
        if encoding == "utf-8":
            data = content.encode()
        elif encoding == "base64":
            try:
                data = base64.b64decode(content, validate=True)
            except ValueError:
                raise APIError(400, "invalid_base64", "content is not valid Base64") from None
        else:
            raise APIError(400, "invalid_encoding", "encoding must be utf-8 or base64")
        if len(data) > FILE_LIMIT:
            raise APIError(413, "file_too_large", "File exceeds 8 MiB")
        if path.is_dir():
            raise APIError(409, "is_directory", "Path is a directory")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".opom-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return {"ok": True, "area": area, "path": body["path"], "bytes": len(data)}

    def delete(self, area, relative):
        _, path = self.path(area, relative)
        if not path.is_file():
            raise APIError(404, "file_not_found", "Only existing files can be deleted")
        path.unlink()
        return {"ok": True, "deleted": relative, "area": area}
