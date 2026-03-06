import os
import subprocess
import sys


def _base_url() -> str:
    return os.getenv("TEST_BASE_URL", "http://localhost:8000")


def test_good_flow():
    result = subprocess.run(
        [
            sys.executable,
            "demo_flow.py",
            "--mode",
            "good",
            "--base-url",
            _base_url(),
        ],
        capture_output=True,
        text=True,
    )

    print(result.stdout)
    print(result.stderr)

    assert result.returncode == 0
    assert "GOOD FLOW PASSED" in result.stdout


def test_tamper_flow():
    result = subprocess.run(
        [
            sys.executable,
            "demo_flow.py",
            "--mode",
            "tamper",
            "--base-url",
            _base_url(),
        ],
        capture_output=True,
        text=True,
    )

    print(result.stdout)
    print(result.stderr)

    assert result.returncode == 0
    assert "TAMPER FLOW PASSED" in result.stdout