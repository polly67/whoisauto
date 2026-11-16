import importlib.util
import json
from pathlib import Path

import pytest

script = Path(__file__).resolve().parents[1] / "scripts/ci/check_runner.py"
spec = importlib.util.spec_from_file_location("check_runner", script)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


@pytest.mark.parametrize("cpus,memory,accepted", [
    (5, 14 * runner.GIB, True),
    (3, 7 * runner.GIB, False),
    (4, 14 * runner.GIB, False),
    (6, 14 * runner.GIB, False),
    (5, 14 * runner.GIB - 1, False),
    (5, 16 * runner.GIB, False),
])
def test_candidate_report_and_gate_follow_exact_specs(tmp_path, monkeypatch, cpus, memory, accepted):
    monkeypatch.setattr(runner.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(runner.subprocess, "check_output", lambda command, **kwargs:
                        str(cpus if command[-1] == "hw.logicalcpu" else memory))
    report = tmp_path / "report.json"
    output = tmp_path / "output"
    summary = tmp_path / "summary"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert runner.main(["--slot", "2", "--report", str(report)]) == 0
    value = json.loads(report.read_text())
    assert value["eligible"] is accepted
    assert value["logical_cpus"] == cpus
    assert value["memory_bytes"] == memory
    assert output.read_text() == f"eligible={str(accepted).lower()}\n"
    assert ("matches" if accepted else "rejected") in summary.read_text()


def test_x86_is_rejected_even_when_cpu_and_memory_match(monkeypatch):
    monkeypatch.setattr(runner.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(runner.subprocess, "check_output", lambda command, **kwargs:
                        str(5 if command[-1] == "hw.logicalcpu" else 14 * runner.GIB))
    assert runner.inspect_runner(1)["eligible"] is False


def test_unknown_hardware_never_marks_a_runner_eligible(tmp_path, monkeypatch):
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(runner.subprocess, "check_output", lambda *args, **kwargs: "unknown")
    with pytest.raises(ValueError):
        runner.main(["--slot", "1", "--report", str(tmp_path / "report.json")])
    assert not output.exists()
