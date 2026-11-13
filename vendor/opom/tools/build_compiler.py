#!/usr/bin/env python3
import argparse
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description="Build the Luau syntax checker")
    parser.add_argument("--source", type=Path, help="Existing Luau source checkout")
    parser.add_argument("--state", type=Path, default=ROOT / ".state")
    parser.add_argument("--existing-build", type=Path, help="Reuse an existing Luau static-library build")
    args = parser.parse_args()
    args.state.mkdir(parents=True, exist_ok=True)
    target = args.state / "opom-compiler"
    if args.existing_build:
        if not args.source:
            parser.error("--existing-build requires --source")
        libraries = [args.existing_build / f"libLuau.{name}.a" for name in ["Compiler", "Ast", "Bytecode", "Common"]]
        command = ["c++", "-std=c++17", "-O2", str(ROOT / "native/compiler.cpp")]
        for name in ["Compiler", "Ast", "Bytecode", "Common"]:
            command.extend(["-I", str(args.source / name / "include")])
        command.extend(str(path) for path in libraries)
        command.extend(["-o", str(target)])
        subprocess.run(command, check=True)
    else:
        build = args.state / "compiler-build"
        command = ["cmake", "-S", str(ROOT), "-B", str(build), "-DCMAKE_BUILD_TYPE=Release"]
        if args.source:
            command.append("-DOPOM_LUAU_SOURCE=" + str(args.source.resolve()))
        subprocess.run(command, check=True)
        subprocess.run(["cmake", "--build", str(build), "--target", "opom-compiler", "-j4"], check=True)
        shutil.copy2(build / "opom-compiler", target)
    print(target)


if __name__ == "__main__":
    main()
