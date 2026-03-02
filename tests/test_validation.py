import json
import socket
import subprocess
import sys
import time
import unittest
import urllib.request
import urllib.error
from pathlib import Path


def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_json_with_body(base_url: str, method: str, path: str, body: dict, timeout: float = 5.0):
    url = base_url + path
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8") if resp.length != 0 else ""
            parsed = json.loads(raw) if raw else None
            return resp.status, parsed
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8") if getattr(e, "fp", None) else ""
        try:
            parsed = json.loads(raw) if raw else None
        except Exception:
            parsed = raw or None
        return e.code, parsed


class ValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[1]
        cls.port = get_free_port()
        cls.base_url = f"http://127.0.0.1:{cls.port}"

        cls.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "backend.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(cls.port),
            ],
            cwd=str(cls.repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                req = urllib.request.Request(cls.base_url + "/health", method="GET")
                with urllib.request.urlopen(req, timeout=1.0):
                    return
            except Exception:
                time.sleep(0.25)

        raise RuntimeError("Server did not start for validation tests.")

    @classmethod
    def tearDownClass(cls):
        if cls.proc:
            cls.proc.terminate()
            try:
                cls.proc.wait(timeout=5)
            except Exception:
                cls.proc.kill()

    def test_intent_missing_field(self):
        status, payload = http_json_with_body(
            self.base_url,
            "POST",
            "/intent",
            body={
                # missing required numeric fields
                "text": "invalid intent"
            },
        )
        self.assertEqual(status, 422)

    def test_telemetry_invalid_range(self):
        # First create a valid session
        status, payload = http_json_with_body(
            self.base_url,
            "POST",
            "/intent",
            body={
                "text": "test",
                "target_p95_latency_ms": 100,
                "target_jitter_ms": 20,
                "duration_s": 30,
            },
        )
        self.assertIn(status, (200, 201))
        session_id = payload["session_id"]

        # Now send invalid telemetry (p95 < p50)
        status2, payload2 = http_json_with_body(
            self.base_url,
            "POST",
            "/telemetry",
            body={
                "session_id": session_id,
                "n": 10,
                "p50_ms": 100,
                "p95_ms": 50,  # invalid
                "jitter_ms": 5,
            },
        )

        self.assertEqual(status2, 422)

    def test_finalize_unknown_session(self):
        status, payload = http_json_with_body(
            self.base_url,
            "POST",
            "/proof/00000000-0000-0000-0000-000000000000/finalize",
            body={},
        )
        self.assertEqual(status, 404)
        