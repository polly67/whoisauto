import plistlib
import zipfile

import pytest

from whoisauto import installer


def make_app(directory, name="Fake.app", executable="Fake", loader=False):
    app = directory / name
    (app / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(
        {"CFBundleExecutable": executable, "CFBundleShortVersionString": "1.0"}))
    (app / "Contents" / "MacOS" / executable).write_text("#!/bin/sh\nexit 0\n")
    if loader:
        resources = app / "Contents" / "Resources"
        resources.mkdir(parents=True, exist_ok=True)
        (resources / "libOpiumware.dylib").write_bytes(b"\x00")
    return app


def test_install_editor_from_app_bundle(paths, tmp_path):
    app = make_app(tmp_path / "src", "Opiumware.app", "Opiumware")
    result = installer.install(paths, "editor", app)
    assert paths.editor_app.is_dir()
    assert result["path"] == str(paths.editor_app)
    assert result["executable"].endswith("Opiumware")


def test_install_roblox_requires_loader(paths, tmp_path):
    app = make_app(tmp_path / "src", "Roblox.app", "RobloxPlayer")
    with pytest.raises(installer.InstallError, match="libOpiumware"):
        installer.install(paths, "roblox", app)
    assert not paths.roblox_app.exists()


def test_install_roblox_with_loader(paths, tmp_path):
    app = make_app(tmp_path / "src", "Roblox.app", "RobloxPlayer", loader=True)
    result = installer.install(paths, "roblox", app)
    assert result["loader"].endswith("libOpiumware.dylib")


def test_install_from_zip(paths, tmp_path):
    app = make_app(tmp_path / "src", "Opiumware.app", "Opiumware")
    archive = tmp_path / "Opiumware.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for file in app.rglob("*"):
            if file.is_file():
                bundle.write(file, app.parent.name + "/" + str(file.relative_to(app.parent)))
    result = installer.install(paths, "editor", archive)
    assert paths.editor_app.is_dir()
    assert result["executable"].endswith("Opiumware")


def test_find_artifact_scans_installers(paths):
    make_app(paths.installers, "Opiumware.app", "Opiumware")
    make_app(paths.installers, "Roblox.app", "RobloxPlayer")
    assert installer.find_artifact(paths, "editor").name == "Opiumware.app"
    assert installer.find_artifact(paths, "roblox").name == "Roblox.app"


def test_missing_artifact_names_the_drop_folder(paths):
    with pytest.raises(installer.InstallError, match="installers"):
        installer.install(paths, "editor")


def test_install_all_reports_missing_roblox(paths, tmp_path):
    editor = make_app(tmp_path / "src", "Opiumware.app", "Opiumware")
    result = installer.install_all(paths, editor_source=editor, roblox_source=tmp_path / "nope.app")
    assert result["ok"] is False
    assert result["error"] == "roblox_install_failed"
    assert paths.editor_app.is_dir()


def test_probe_reports_state(paths, tmp_path):
    empty = installer.probe(paths)
    assert empty["editor"]["available"] is False
    make_app(paths.root / "Apps", "Opiumware.app", "Opiumware")
    make_app(paths.root / "Apps", "Roblox.app", "RobloxPlayer", loader=True)
    ready = installer.probe(paths)
    assert ready["editor"]["available"] is True
    assert ready["roblox"]["available"] is True


def test_verify_rejects_non_bundle(tmp_path):
    plain = tmp_path / "NotAnApp"
    plain.mkdir()
    with pytest.raises(installer.InstallError):
        installer.verify_app(plain)


def test_failed_copy_keeps_existing_install(paths, tmp_path, monkeypatch):
    old = make_app(paths.root / "Apps", "Opiumware.app", "Opiumware")
    marker = old / "keep.txt"
    marker.write_text("old install")
    source = make_app(tmp_path / "src", "Opiumware.app", "Opiumware")
    monkeypatch.setattr(installer, "is_darwin", lambda: False)

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(installer.shutil, "copytree", fail)
    with pytest.raises(installer.InstallError, match="disk full"):
        installer.install(paths, "editor", source)
    assert marker.read_text() == "old install"


def test_installing_bundle_onto_itself_is_safe(paths):
    source = make_app(paths.root / "Apps", "Opiumware.app", "Opiumware")
    result = installer.install(paths, "editor", source)
    assert result["path"] == str(source)
    assert source.is_dir()


def test_dmg_detaches_when_verification_fails(paths, tmp_path, monkeypatch):
    artifact = tmp_path / "Roblox.dmg"
    artifact.touch()
    source = make_app(tmp_path / "mounted", "Roblox.app", "RobloxPlayer")
    monkeypatch.setattr(installer, "is_darwin", lambda: True)
    monkeypatch.setattr(installer, "_extract", lambda *args: source.parent)
    calls = []
    monkeypatch.setattr(installer.subprocess, "run", lambda command, **kwargs: calls.append(command))
    with pytest.raises(installer.InstallError, match="libOpiumware"):
        installer.install(paths, "roblox", artifact)
    assert calls == [["/usr/bin/hdiutil", "detach", str(source.parent), "-quiet"]]


def test_failed_replacement_restores_existing_bundle(paths, tmp_path, monkeypatch):
    old = make_app(paths.root / "Apps", "Opiumware.app", "Opiumware")
    (old / "keep.txt").write_text("old install")
    source = make_app(tmp_path / "src", "Opiumware.app", "Opiumware")
    original = installer.os.replace

    def replace(source, destination):
        if ".whoisauto-install-" in str(source):
            raise OSError("rename failed")
        return original(source, destination)

    monkeypatch.setattr(installer.os, "replace", replace)
    with pytest.raises(installer.InstallError, match="rename failed"):
        installer.install(paths, "editor", source)
    assert (old / "keep.txt").read_text() == "old install"


def test_broken_zip_reports_install_error(paths, tmp_path):
    source = tmp_path / "Opiumware.zip"
    source.write_text("not a zip archive")
    with pytest.raises(installer.InstallError):
        installer.install(paths, "editor", source)
