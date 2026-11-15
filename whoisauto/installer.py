"""Install the modified Opiumware Player into the opom root.

opom expects this layout and refuses to launch anything without it::

    <root>/Apps/Opiumware.app
    <root>/Apps/Roblox.app        # Roblox.app patched with libOpiumware.dylib
    <root>/Data/workspace

The panel can only assemble that layout from an artifact you supply: drop a
``.app``/``.dmg``/``.zip`` into ``installers/`` or pass a path/URL. Nothing is
downloaded from a third party by default.
"""

from __future__ import annotations

import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
import uuid

from .config import Paths, read_json, write_json

EDITOR_HINTS = ("opiumware", "opium")
ROBLOX_HINTS = ("roblox",)


class InstallError(RuntimeError):
    pass


def is_darwin() -> bool:
    return sys.platform == "darwin"


def bundle_executable(app: Path) -> Path | None:
    try:
        with (app / "Contents" / "Info.plist").open("rb") as stream:
            info = plistlib.load(stream)
        name = info["CFBundleExecutable"]
    except (OSError, ValueError, KeyError, TypeError, plistlib.InvalidFileException):
        return None
    if not isinstance(name, str) or not name or Path(name).name != name:
        return None
    candidate = app / "Contents" / "MacOS" / str(name)
    return candidate if candidate.is_file() and candidate.resolve().is_relative_to(app.resolve()) else None


def verify_app(app: Path, require_loader: bool = False) -> dict:
    executable = bundle_executable(app)
    if executable is None:
        raise InstallError(f"{app.name} is not a runnable macOS app bundle")
    result = {"path": str(app), "executable": str(executable)}
    if require_loader:
        loader = app / "Contents" / "Resources" / "libOpiumware.dylib"
        if not loader.is_file():
            raise InstallError(
                f"{app.name} has no Contents/Resources/libOpiumware.dylib; "
                "this is the stock Roblox app, not the Opiumware-patched build")
        result["loader"] = str(loader)
    return result


def find_artifact(paths: Paths, kind: str, source: str | os.PathLike | None = None) -> Path | None:
    if source:
        candidate = Path(source).expanduser()
        if not candidate.exists():
            raise InstallError(f"{candidate} does not exist")
        return candidate.resolve()
    hints = EDITOR_HINTS if kind == "editor" else ROBLOX_HINTS
    if not paths.installers.is_dir():
        return None
    for entry in sorted(paths.installers.iterdir()):
        name = entry.name.lower()
        if entry.suffix.lower() in (".app", ".dmg", ".zip", ".pkg") and any(h in name for h in hints):
            return entry.resolve()
    return None


def _download(url: str, target_dir: Path) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    name = Path(urllib.parse.urlsplit(url).path).name or "download.bin"
    target = target_dir / name
    with urllib.request.urlopen(url, timeout=120) as response, target.open("wb") as stream:
        shutil.copyfileobj(response, stream)
    return target


def _resolve_source(source: str | os.PathLike | None, paths: Paths, kind: str, workdir: Path) -> Path:
    if source and str(source).startswith(("http://", "https://")):
        return _download(str(source), workdir)
    found = find_artifact(paths, kind, source)
    if found is None:
        label = "Opiumware" if kind == "editor" else "Roblox"
        raise InstallError(
            f"No {label} artifact found. Drop a .app/.dmg/.zip into {paths.installers} "
            f"or pass --source. whoisauto does not download {label} on its own.")
    return found


def _copy_tree(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if is_darwin() and shutil.which("ditto"):
        # ditto preserves code signatures and extended attributes
        result = subprocess.run(["/usr/bin/ditto", str(source), str(destination)],
                                capture_output=True, text=True, timeout=300)
        if result.returncode:
            raise InstallError(f"ditto failed: {result.stderr.strip()[-500:]}")
        return
    shutil.copytree(source, destination, symlinks=True)


def _replace_app(source: Path, destination: Path, require_loader: bool) -> None:
    """Verify a staged copy before replacing an existing installation."""
    if source.resolve() == destination.resolve():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup = destination.with_name(f".{destination.name}.backup-{uuid.uuid4().hex}")
    with tempfile.TemporaryDirectory(prefix=".whoisauto-install-", dir=destination.parent) as temporary:
        staged = Path(temporary) / destination.name
        _copy_tree(source, staged)
        verify_app(staged, require_loader)
        if destination.exists() or destination.is_symlink():
            os.replace(destination, backup)
        try:
            os.replace(staged, destination)
        except OSError:
            if backup.exists() or backup.is_symlink():
                # Keep the backup outside the temporary directory so even a
                # failed rollback never deletes the user's previous bundle.
                try:
                    os.replace(backup, destination)
                except OSError as error:
                    raise InstallError(f"Rollback failed: {error}; previous bundle remains at {backup}") from None
            raise
    if backup.is_symlink():
        backup.unlink()
    elif backup.exists():
        shutil.rmtree(backup)


def _extract(artifact: Path, workdir: Path) -> Path:
    suffix = artifact.suffix.lower()
    if suffix == ".app" or artifact.is_dir():
        return artifact
    if suffix == ".zip":
        unpacked = workdir / "unpacked"
        unpacked.mkdir(parents=True, exist_ok=True)
        if is_darwin() and shutil.which("ditto"):
            result = subprocess.run(["/usr/bin/ditto", "-x", "-k", str(artifact), str(unpacked)],
                                    capture_output=True, text=True, timeout=300)
            if result.returncode:
                raise InstallError(f"unzip failed: {result.stderr.strip()[-500:]}")
        else:
            with zipfile.ZipFile(artifact) as archive:
                archive.extractall(unpacked)
        return unpacked
    if suffix == ".dmg":
        if not is_darwin():
            raise InstallError("Mounting a .dmg needs macOS; use a .app or .zip on this platform")
        mount = workdir / "mnt"
        mount.mkdir(parents=True, exist_ok=True)
        attached = subprocess.run(["/usr/bin/hdiutil", "attach", str(artifact), "-nobrowse",
                                   "-readonly", "-mountpoint", str(mount)],
                                  capture_output=True, text=True, timeout=120)
        if attached.returncode:
            raise InstallError(f"hdiutil attach failed: {attached.stderr.strip()[-500:]}")
        return mount
    if suffix == ".pkg":
        raise InstallError(
            "A .pkg installs into system locations; extract the .app from the vendor "
            "package or drop a .dmg/.zip instead")
    raise InstallError(f"Unsupported artifact type: {artifact.name}")


def _locate_app(root: Path) -> Path | None:
    for candidate in sorted(root.rglob("*.app")):
        if candidate.name.startswith("._"):
            continue
        if bundle_executable(candidate) is not None:
            return candidate
    return None


def install(paths: Paths, kind: str, source: str | os.PathLike | None = None,
            destination: Path | None = None) -> dict:
    """Install one app bundle into the opom root and verify it."""
    if kind not in ("editor", "roblox"):
        raise InstallError("kind must be editor or roblox")
    try:
        return _install(paths, kind, source, destination)
    except (OSError, subprocess.TimeoutExpired, zipfile.BadZipFile) as error:
        raise InstallError(f"Could not install {kind}: {error}") from None


def _install(paths: Paths, kind: str, source, destination: Path | None) -> dict:
    paths.ensure()
    destination = destination or (paths.editor_app if kind == "editor" else paths.roblox_app)
    with tempfile.TemporaryDirectory(prefix="whoisauto-install-") as temporary:
        workdir = Path(temporary)
        artifact = _resolve_source(source, paths, kind, workdir)
        extracted = _extract(artifact, workdir)
        try:
            app = extracted if extracted.suffix.lower() == ".app" and bundle_executable(extracted) else _locate_app(extracted)
            if app is None:
                raise InstallError(f"{artifact.name} contains no runnable .app bundle")
            # Verify before copying so a rejected bundle never leaves a half-install.
            verify_app(app, require_loader=(kind == "roblox"))
            _replace_app(app, destination, require_loader=(kind == "roblox"))
        finally:
            if artifact.suffix.lower() == ".dmg" and is_darwin():
                subprocess.run(["/usr/bin/hdiutil", "detach", str(extracted), "-quiet"],
                               capture_output=True, timeout=60)
    return verify_app(destination, require_loader=(kind == "roblox"))


def launch_editor(paths: Paths) -> dict:
    """Open Opiumware once so it can finish its own first-run install."""
    if not is_darwin():
        raise InstallError("Launching Opiumware needs macOS")
    if bundle_executable(paths.editor_app) is None:
        raise InstallError("Opiumware is not installed yet")
    result = subprocess.run(["/usr/bin/open", "-a", str(paths.editor_app)],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise InstallError(f"open failed: {result.stderr.strip()[-500:]}")
    return {"launched": str(paths.editor_app)}


def probe(paths: Paths) -> dict:
    """Read-only install status, safe to call on any platform."""
    def describe(app: Path, require_loader: bool = False):
        entry = {"path": str(app), "present": app.is_dir(), "available": False}
        if not app.is_dir():
            return entry
        try:
            info = verify_app(app, require_loader)
            entry.update(available=True, **info)
        except InstallError as error:
            entry["problem"] = str(error)
        return entry

    return {
        "root": str(paths.root),
        "opom": {"path": str(paths.opom_dir), "present": (paths.opom_dir / "opom" / "api.py").is_file()},
        "editor": describe(paths.editor_app),
        "roblox": describe(paths.roblox_app, require_loader=True),
        "manifest": _read_manifest(paths),
    }


def _read_manifest(paths: Paths) -> dict | None:
    value = read_json(paths.manifest_file)
    return value if isinstance(value, dict) else None


def install_all(paths: Paths, editor_source=None, roblox_source=None, launch: bool = False) -> dict:
    result = {"ok": True, "editor": install(paths, "editor", editor_source)}
    try:
        result["roblox"] = install(paths, "roblox", roblox_source)
    except InstallError as error:
        # Keep the editor install; report the missing patched Player clearly.
        result.update(ok=False, error="roblox_install_failed", message=str(error))
        write_json(paths.manifest_file, result, mode=0o644)
        return result
    if launch:
        result["launch"] = launch_editor(paths)
    write_json(paths.manifest_file, result, mode=0o644)
    return result
