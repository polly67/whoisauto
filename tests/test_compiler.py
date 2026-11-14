import textwrap

import pytest

from whoisauto import compiler
from whoisauto.config import Paths


def _paths(tmp_path) -> Paths:
    return Paths(repo=tmp_path, runtime=tmp_path / "rt", state=tmp_path / "rt" / "state",
                 root=tmp_path / "root", workspace=tmp_path / "ws",
                 installers=tmp_path / "installers")


def _fake_opom(tmp_path, body: str, monkeypatch) -> Paths:
    from whoisauto import config
    opom = tmp_path / "opom"
    (opom / "tools").mkdir(parents=True)
    (opom / "tools" / "build_compiler.py").write_text(textwrap.dedent(body))
    monkeypatch.setattr(config, "OPOM_DIR", opom)
    return _paths(tmp_path)


def test_build_places_the_binary_in_the_opom_state(tmp_path, monkeypatch):
    paths = _fake_opom(tmp_path, """
        import argparse, pathlib
        parser = argparse.ArgumentParser()
        parser.add_argument("--state", type=pathlib.Path)
        args = parser.parse_args()
        target = args.state / "opom-compiler"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\\x7fELF fake")
        print(target)
    """, monkeypatch)

    assert compiler.available(paths) is False
    result = compiler.build(paths)

    assert result["ok"] is True
    assert result["bytes"] == len(b"\x7fELF fake")
    assert compiler.available(paths) is True
    assert compiler.binary(paths) == paths.opom_state / "opom-compiler"


def test_build_surfaces_the_script_output_on_failure(tmp_path, monkeypatch):
    paths = _fake_opom(tmp_path, """
        import sys
        print("cmake: no such file")
        sys.exit(2)
    """, monkeypatch)

    with pytest.raises(compiler.CompilerError) as error:
        compiler.build(paths)
    assert "exit 2" in error.value.message
    assert "cmake" in error.value.log


def test_build_without_the_vendored_script_fails_clearly(tmp_path, monkeypatch):
    from whoisauto import config
    monkeypatch.setattr(config, "OPOM_DIR", tmp_path / "absent")
    with pytest.raises(compiler.CompilerError) as error:
        compiler.build(_paths(tmp_path))
    assert "build_compiler.py" in error.value.message
