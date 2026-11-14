"""Build the Luau syntax checker that opom runs before every execution.

opom compiles a script to bytecode first and refuses to run when the checker
binary is absent (``compiler_missing``). The binary is produced from the
vendored Luau sources, so it has to be built per machine — the CI runners start
from a clean checkout every time.
"""

from __future__ import annotations

import subprocess
import sys

from .config import Paths


class CompilerError(RuntimeError):
    def __init__(self, message: str, log: str = ""):
        self.message = message
        self.log = log
        super().__init__(message)


def binary(paths: Paths):
    """Where opom looks for the checker (``OPOM_COMPILER`` default)."""
    return paths.opom_state / "opom-compiler"


def available(paths: Paths) -> bool:
    return binary(paths).is_file()


def build(paths: Paths, timeout: float = 1200) -> dict:
    paths.opom_state.mkdir(parents=True, exist_ok=True)
    script = paths.opom_dir / "tools" / "build_compiler.py"
    if not script.is_file():
        raise CompilerError(f"missing {script}")

    command = [sys.executable, str(script), "--state", str(paths.opom_state)]
    try:
        result = subprocess.run(command, cwd=str(paths.opom_dir), capture_output=True,
                                text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise CompilerError(f"compiler build exceeded {timeout:g}s") from None
    except OSError as error:
        raise CompilerError(str(error)) from None

    log = (result.stdout or "") + (result.stderr or "")
    target = binary(paths)
    if result.returncode != 0 or not target.is_file():
        tail = "\n".join(log.strip().splitlines()[-20:])
        raise CompilerError(f"compiler build failed (exit {result.returncode})", tail)
    return {"ok": True, "path": str(target), "bytes": target.stat().st_size,
            "log": "\n".join(log.strip().splitlines()[-20:])}
