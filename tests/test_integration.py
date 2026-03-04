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


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[1]
        cls.port = get_free_port()
        cls.base_url = f"http://127.0.0.1:{cls.port}"

        logs_dir = cls.repo_root / "artifacts" / "test_logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        cls.log_path = logs_dir / f"uvicorn_integration_{cls.port}.log"

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
        raise RuntimeError(f"Server did not become healthy. Last: {last}\nLogs:\n{logs}")

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

    def test_end_to_end_pipeline_creates_proof_file(self):
        # 1) intent
        s1, p1 = http_json_with_body(
            self.base_url,
            "POST",
            "/intent",
            {
                "text": "integration test",
                "target_p95_latency_ms": 100,
                "target_jitter_ms": 20,
                "duration_s": 30,
            },
        )
        self.assertIn(s1, (200, 201), f"/intent: {s1} {p1}")
        session_id = p1["session_id"]

        # 2) telemetry
        s2, p2 = http_json_with_body(
            self.base_url,
            "POST",
            "/telemetry",
            {"session_id": session_id, "n": 5, "p50_ms": 40, "p95_ms": 80, "jitter_ms": 5},
        )
        self.assertIn(s2, (200, 201), f"/telemetry: {s2} {p2}")

        # 3) finalize
        s3, p3 = http_json_with_body(self.base_url, "POST", f"/proof/{session_id}/finalize", {}, timeout=20.0)
        self.assertIn(s3, (200, 201), f"/finalize: {s3} {p3}")

        # 4) proof GET
        s4, p4 = http_json(self.base_url, "GET", f"/proof/{session_id}")
        self.assertEqual(s4, 200, f"/proof GET: {s4} {p4}")

        # 5) backend should write artifacts/proof_<sid>_<ts>.json
        artifacts_dir = self.repo_root / "artifacts"
        matches = list(artifacts_dir.glob(f"proof_{session_id}_*.json"))
        self.assertTrue(matches, f"No proof file found matching proof_{session_id}_*.json in {artifacts_dir}")