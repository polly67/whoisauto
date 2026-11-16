"""Inspect the current Mac before any desktop software is installed."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import subprocess

GIB = 1024 ** 3
REQUIRED_CPUS = 5
REQUIRED_MEMORY = 14 * GIB


def inspect_runner(slot: int) -> dict:
    cpus = int(subprocess.check_output(["sysctl", "-n", "hw.logicalcpu"], text=True).strip())
    memory = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip())
    architecture = platform.machine()
    return {
        "candidate": slot,
        "runner_name": os.environ.get("RUNNER_NAME", ""),
        "architecture": architecture,
        "logical_cpus": cpus,
        "memory_bytes": memory,
        "memory_gib": memory / GIB,
        "required_cpus": REQUIRED_CPUS,
        "required_memory_gib": REQUIRED_MEMORY // GIB,
        "eligible": architecture == "arm64" and cpus == REQUIRED_CPUS and memory == REQUIRED_MEMORY,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot", type=int, required=True, choices=range(1, 6))
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    report = inspect_runner(args.slot)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report), flush=True)
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a") as stream:
            stream.write(f"eligible={str(report['eligible']).lower()}\n")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a") as stream:
            stream.write(f"### Candidate {args.slot}: {'matches' if report['eligible'] else 'rejected'}\n\n")
            stream.write(f"{report['architecture']}, {report['logical_cpus']} vCPUs, "
                         f"{report['memory_gib']:g} GiB RAM. Required: arm64, 5 vCPUs and exactly 14 GiB.\n\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
