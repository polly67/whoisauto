#!/usr/bin/env python3
"""Benchmark the macOS host CPU and Metal GPU and write a markdown report.

Compiles the two small harnesses next to this file, runs them, merges the
JSON they emit with the host facts, and writes bench.json + bench.md into
--out-dir. Standard library only; safe to run on a stock macOS runner.
"""

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def sh(cmd, timeout=120):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def sysctl(name):
    p = sh(["sysctl", "-n", name], timeout=30)
    return p.stdout.strip() if p.returncode == 0 else ""


def sysctl_int(name):
    value = sysctl(name)
    try:
        return int(value)
    except ValueError:
        return 0


def human_bytes(n):
    if not n:
        return "unknown"
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    value = float(n)
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{n} B"


def host_info():
    return {
        "model": sysctl("hw.model"),
        "cpu_brand": sysctl("machdep.cpu.brand_string"),
        "physical_cpus": sysctl_int("hw.physicalcpu"),
        "logical_cpus": sysctl_int("hw.logicalcpu"),
        "memory_bytes": sysctl_int("hw.memsize"),
        "macos": sh(["sw_vers", "-productVersion"], timeout=30).stdout.strip(),
        "build": sh(["sw_vers", "-buildVersion"], timeout=30).stdout.strip(),
        "arch": platform.machine(),
        "kernel": platform.release(),
        "image_os": os.environ.get("ImageOS", ""),
        "image_version": os.environ.get("ImageVersion", ""),
    }


def display_info():
    p = sh(["system_profiler", "SPDisplaysDataType", "-json"], timeout=120)
    if p.returncode != 0 or not p.stdout.strip():
        return []
    try:
        data = json.loads(p.stdout)
    except ValueError:
        return []
    out = []
    for item in data.get("SPDisplaysDataType", []):
        entry = {
            "name": item.get("_name", ""),
            "model": item.get("sppci_model", ""),
            "metal": item.get("spdisplays_metal", item.get("spdisplays_metal_support", "")),
            "vram": item.get("sppci_vram", item.get("sppci_vram_shared", "")),
            "vendor": item.get("spdisplays_vendor", ""),
        }
        out.append({k: v for k, v in entry.items() if v})
    return out


def build(src, out, flags, log):
    """Compile src -> out, trying -march=native first for the C harness."""
    attempts = []
    if src.suffix == ".c":
        attempts.append(flags + ["-march=native"])
    attempts.append(flags)

    errors = []
    for extra in attempts:
        cmd = ["clang"] + extra + [str(src), "-o", str(out)]
        p = sh(cmd, timeout=180)
        if p.returncode == 0:
            log.append("$ " + " ".join(cmd) + "\n" + (p.stderr or ""))
            return True, extra
        errors.append("$ " + " ".join(cmd) + "\n" + (p.stderr or p.stdout))
    log.append("\n".join(errors))
    return False, []


def run_json(binary, timeout, log):
    p = sh([str(binary)], timeout=timeout)
    log.append(f"$ {binary}\n[exit {p.returncode}]\n{p.stderr}")
    if p.returncode != 0:
        return {"error": (p.stderr or p.stdout).strip() or f"exit {p.returncode}"}
    try:
        return json.loads(p.stdout)
    except ValueError:
        return {"error": "unparsable output", "raw": p.stdout[-2000:]}


def gpu_section(gpu, displays):
    lines = []
    if not gpu:
        lines.append("_No GPU results: harness did not run._")
        return lines

    if not gpu.get("available"):
        lines.append(
            "No Metal device is exposed to this virtual machine — "
            "`MTLCopyAllDevices()` returned 0 devices"
            + (f" ({gpu['reason']})." if gpu.get("reason") else ".")
        )
        if displays:
            lines.append("")
            lines.append("| Display adapter (system_profiler) | Value |")
            lines.append("|---|---|")
            for d in displays:
                name = d.get("name") or d.get("model") or "unknown"
                bits = ", ".join(
                    f"{k}={v}" for k, v in d.items() if k not in ("name", "model")
                )
                lines.append(f"| {name} | {bits or '—'} |")
        return lines

    if gpu.get("error"):
        lines.append(f"Metal device present but the benchmark failed: `{gpu['error']}`")
        return lines

    dev = (gpu.get("devices") or [{}])[0]
    lines.append("| Field | Value |")
    lines.append("|---|---|")
    lines.append(f"| Device | {dev.get('name', 'unknown')} |")
    lines.append(f"| Unified memory | {dev.get('unified_memory')} |")
    lines.append(f"| Low power | {dev.get('low_power')} |")
    lines.append(f"| Headless | {dev.get('headless')} |")
    lines.append(
        f"| Max working set | {human_bytes(dev.get('recommended_max_working_set', 0))} |"
    )
    lines.append(
        f"| Max buffer | {human_bytes(dev.get('max_buffer_length', 0))} |"
    )
    lines.append(f"| Max threads/group | {dev.get('max_threads_per_group', '?')} |")
    lines.append("")

    copy = gpu.get("copy") or {}
    fma = gpu.get("fma") or {}
    lines.append("| Benchmark | Result |")
    lines.append("|---|---|")
    if copy.get("gbs"):
        lines.append(
            f"| Device memory copy (read+write) | {copy['gbs']:.1f} GB/s "
            f"({human_bytes(copy.get('bytes', 0))} per dispatch) |"
        )
    if fma.get("gflops"):
        lines.append(
            f"| FP32 FMA | {fma['gflops'] / 1000.0:.2f} TFLOP/s "
            f"({fma.get('flops', 0) / 1e9:.1f} GFLOP per dispatch) |"
        )
    return lines


def build_report(data):
    host = data["host"]
    cpu = data.get("cpu_bench", {}).get("cpu", {})
    single = data.get("cpu_bench", {}).get("single", {})
    multi = data.get("cpu_bench", {}).get("multi", {})
    memory = data.get("cpu_bench", {}).get("memory", {})
    isa = data.get("cpu_bench", {}).get("isa", "?")

    model = host.get("model") or "unknown model"
    lines = [
        "# macOS Intel benchmark",
        "",
        f"**Host** — {model} · macOS {host.get('macos') or '?'} "
        f"({host.get('build') or '?'}) · {host.get('arch') or '?'}",
    ]
    if host.get("image_os") or host.get("image_version"):
        lines.append(
            f"**Runner image** — {host.get('image_os') or '?'} "
            f"{host.get('image_version') or ''}".rstrip()
        )
    lines += [
        f"**CPU** — {cpu.get('brand') or host.get('cpu_brand') or 'unknown'} · "
        f"{cpu.get('physical', host.get('physical_cpus', 0))} physical / "
        f"{cpu.get('logical', host.get('logical_cpus', 0))} logical · "
        f"{human_bytes(cpu.get('mem_bytes', host.get('memory_bytes', 0)))} RAM",
        "",
        "## CPU",
        "",
        "| Metric | Result |",
        "|---|---|",
    ]

    if single:
        lines.append(f"| Integer, dependent chain | {single.get('int_chain_ns', 0):.2f} ns/op |")
        lines.append(f"| Integer throughput, 1 thread | {single.get('int_gops', 0):.2f} Gop/s |")
        lines.append(f"| FP32 FMA, 1 thread | {single.get('fp32_gflops', 0):.1f} GFLOP/s |")
    if multi:
        lines.append(
            f"| Integer throughput, {multi.get('threads', '?')} threads | "
            f"{multi.get('int_gops', 0):.2f} Gop/s "
            f"({multi.get('int_scaling', 0):.2f}x) |"
        )
        lines.append(
            f"| FP32 FMA, {multi.get('threads', '?')} threads | "
            f"{multi.get('fp32_gflops', 0):.1f} GFLOP/s "
            f"({multi.get('fp32_scaling', 0):.2f}x) |"
        )
    if memory:
        lines.append(f"| Memory read | {memory.get('read_gbs', 0):.1f} GB/s |")
        lines.append(f"| Memory write | {memory.get('write_gbs', 0):.1f} GB/s |")
        lines.append(f"| Memory copy (read+write) | {memory.get('copy_gbs', 0):.1f} GB/s |")
    lines.append("")
    lines.append(f"_Codegen: `{isa}`; caches L1d {human_bytes(cpu.get('l1d_bytes', 0))}, "
                 f"L2 {human_bytes(cpu.get('l2_bytes', 0))}, "
                 f"L3 {human_bytes(cpu.get('l3_bytes', 0))}; "
                 f"nominal clock {cpu.get('freq_hz', 0) / 1e6:.0f} MHz._"
                 if cpu.get("freq_hz") else
                 f"_Codegen: `{isa}`; caches L1d {human_bytes(cpu.get('l1d_bytes', 0))}, "
                 f"L2 {human_bytes(cpu.get('l2_bytes', 0))}, "
                 f"L3 {human_bytes(cpu.get('l3_bytes', 0))}._")
    lines += ["", "## GPU", ""]
    lines += gpu_section(data.get("gpu"), data.get("displays", []))
    lines += [
        "",
        "## Method",
        "",
        "- CPU harness: `scripts/bench/cpu_bench.c`, clang `-O3 -ffp-contract=fast`, "
        "best of 3 runs. Integer chain is a dependent xorshift (latency); throughput "
        "is 8 independent LCG streams; FP32 is 8 independent FMA chains.",
        "- Memory: 512 MiB buffers, best of 3; copy counts read+write traffic.",
        "- GPU harness: `scripts/bench/gpu_bench.m`, Metal compute (float4 copy and "
        "float4 FMA), best of 3–5 dispatches, GPU timestamps when the device reports them.",
        "- These are virtualized GitHub-hosted runners; absolute numbers include "
        "neighbour noise and are not a substitute for bare-metal measurements.",
        "",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default="bench-out")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    build_log = []

    host = host_info()
    displays = display_info()

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        cpu_bin = tmp / "cpu_bench"
        cpu_ok, cpu_flags = build(
            HERE / "cpu_bench.c",
            cpu_bin,
            ["-O3", "-ffp-contract=fast", "-pthread"],
            build_log,
        )
        cpu_bench = run_json(cpu_bin, 300, build_log) if cpu_ok else {"error": "compile failed"}

        gpu_bin = tmp / "gpu_bench"
        gpu_ok, _ = build(
            HERE / "gpu_bench.m",
            gpu_bin,
            ["-O2", "-fobjc-arc", "-framework", "Foundation", "-framework", "Metal"],
            build_log,
        )
        gpu = run_json(gpu_bin, 180, build_log) if gpu_ok else {"error": "compile failed"}

    data = {
        "host": host,
        "displays": displays,
        "cpu_bench": cpu_bench,
        "gpu": gpu,
        "build": {
            "cpu_flags": cpu_flags,
            "log": "\n".join(build_log),
        },
    }

    (out_dir / "bench.json").write_text(json.dumps(data, indent=2) + "\n")
    (out_dir / "build.log").write_text("\n".join(build_log) + "\n")

    report = build_report(data)
    (out_dir / "bench.md").write_text(report + "\n")
    print(report)

    if cpu_bench.get("error") or gpu.get("error"):
        print("\nbench: one or more harnesses failed, see build.log", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
