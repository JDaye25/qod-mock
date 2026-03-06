import subprocess
import sys


def test_good_flow():
    result = subprocess.run(
        [sys.executable, "demo_flow.py", "--mode", "good"],
        capture_output=True,
        text=True,
    )

    print(result.stdout)
    print(result.stderr)

    assert result.returncode == 0
    assert "GOOD FLOW PASSED" in result.stdout


def test_tamper_flow():
    result = subprocess.run(
        [sys.executable, "demo_flow.py", "--mode", "tamper"],
        capture_output=True,
        text=True,
    )

    print(result.stdout)
    print(result.stderr)

    assert result.returncode == 0
    assert "TAMPER FLOW PASSED" in result.stdout
