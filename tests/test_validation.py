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


def http_json(base_url: str, method: str, path: str, timeout: float = 5.0):
    url = base_url + path
    req = urllib.request.Request(url, method=method)
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
    except Exception as e:
        return 0, {"error": str(e)}


def http_json_with_body(base_url: str, method: str, path: str, body: dict, timeout: float = 10.0):
    url = base_url + path
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
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

        logs_dir = cls.repo_root / "artifacts" / "test_logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        cls.log_path = logs_dir / f"uvicorn_validation_{cls.port}.log"

        cmd = [
            sys.executable,
            "-m",
            "uvicorn",
            "backend.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(cls.port),
        ]

        cls.log_fh = cls.log_path.open("w", encoding="utf-8")
        cls.proc = subprocess.Popen(
            cmd,
            cwd=str(cls.repo_root),
            stdout=cls.log_fh,
            stderr=subprocess.STDOUT,
            text=True,
        )

        deadline = time.time() + 25
        last = None
        while time.time() < deadline:
            status, payload = http_json(cls.base_url, "GET", "/health", timeout=2.0)
            if status == 200 and isinstance(payload, dict) and payload.get("status") == "ok":
                return
            last = (status, payload)
            time.sleep(0.25)

        logs = ""
        try:
            logs = cls.log_path.read_text(encoding="utf-8")[-6000:]
        except Exception:
            pass
        cls._kill_proc()
        raise RuntimeError(f"Server did not start for validation tests. Last: {last}\nLogs:\n{logs}")

    @classmethod
    def _kill_proc(cls):
        if getattr(cls, "proc", None):
            try:
                cls.proc.terminate()
                cls.proc.wait(timeout=5)
            except Exception:
                try:
                    cls.proc.kill()
                except Exception:
                    pass
        if getattr(cls, "log_fh", None):
            try:
                cls.log_fh.close()
            except Exception:
                pass

    @classmethod
    def tearDownClass(cls):
        cls._kill_proc()

    def test_intent_missing_field(self):
        status, payload = http_json_with_body(
            self.base_url,
            "POST",
            "/intent",
            body={"text": "test", "target_p95_latency_ms": 100, "target_jitter_ms": 10},
            timeout=10.0,
        )
        self.assertEqual(status, 422)

    def test_telemetry_invalid_range(self):
        # Create session
        s1, p1 = http_json_with_body(
            self.base_url,
            "POST",
            "/intent",
            body={"text": "test", "target_p95_latency_ms": 100, "target_jitter_ms": 10, "duration_s": 30},
            timeout=10.0,
        )
        self.assertIn(s1, (200, 201))
        session_id = p1["session_id"]

        # p95 < p50 should fail validation
        s2, p2 = http_json_with_body(
            self.base_url,
            "POST",
            "/telemetry",
            body={"session_id": session_id, "n": 1, "p50_ms": 50, "p95_ms": 40, "jitter_ms": 1},
            timeout=10.0,
        )
        self.assertEqual(s2, 422, p2)

    def test_finalize_unknown_session(self):
        status, payload = http_json_with_body(
            self.base_url,
            "POST",
            "/proof/00000000-0000-0000-0000-000000000000/finalize",
            body={},
            timeout=10.0,
        )
        self.assertEqual(status, 404)