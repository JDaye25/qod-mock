import json
import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.request
import urllib.error
from pathlib import Path

from src.validate_artifact import validate_artifact_json


def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_json(base_url: str, method: str, path: str, timeout: float = 2.0):
    url = base_url + path
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8") if resp.length != 0 else ""
            data = json.loads(body) if body else None
            return resp.status, data
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8") if getattr(e, "fp", None) else ""
        try:
            parsed = json.loads(raw) if raw else None
        except Exception:
            parsed = raw or None
        return e.code, parsed


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


class SmokeTestQoD(unittest.TestCase):
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
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        deadline = time.time() + 20
        last_err = None
        while time.time() < deadline:
            try:
                status, payload = http_json(cls.base_url, "GET", "/health", timeout=1.0)
                if status == 200 and isinstance(payload, dict) and payload.get("status") == "ok":
                    return
            except Exception as e:
                last_err = e
                time.sleep(0.25)

        logs = ""
        try:
            if cls.proc.stdout:
                logs = cls.proc.stdout.read()[-4000:]
        except Exception:
            pass
        raise RuntimeError(f"Server did not become healthy. Last error: {last_err}\nLogs:\n{logs}")

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "proc", None):
            cls.proc.terminate()
            try:
                cls.proc.wait(timeout=5)
            except Exception:
                cls.proc.kill()
            if cls.proc.stdout:
                try:
                    cls.proc.stdout.close()
                except Exception:
                    pass

    def test_health_endpoint(self):
        status, payload = http_json(self.base_url, "GET", "/health")
        self.assertEqual(status, 200)
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload.get("status"), "ok")

    def test_finalize_produces_schema_valid_artifact(self):
        # 1) Create session
        status, payload = http_json_with_body(
            self.base_url,
            "POST",
            "/intent",
            body={
                "text": "smoke test: generate a minimal proof artifact",
                "target_p95_latency_ms": 100,
                "target_jitter_ms": 20,
                "duration_s": 30,
            },
            timeout=10.0,
        )
        self.assertIn(status, (200, 201), f"/intent returned {status}: {payload}")
        self.assertIsInstance(payload, dict)

        session_id = (
            payload.get("session_id")
            or payload.get("id")
            or (payload.get("session") or {}).get("id")
            or (payload.get("data") or {}).get("session_id")
        )
        self.assertTrue(session_id, f"Could not find session_id in /intent response: {payload}")

        # 2) Send telemetry (required before finalize)
        t_status, t_payload = http_json_with_body(
            self.base_url,
            "POST",
            "/telemetry",
            body={
                "session_id": session_id,
                "n": 30,
                "p50_ms": 60,
                "p95_ms": 90,
                "jitter_ms": 10,
            },
            timeout=10.0,
        )
        self.assertIn(t_status, (200, 201), f"/telemetry returned {t_status}: {t_payload}")

        # 3) Finalize
        status2, payload2 = http_json_with_body(
            self.base_url,
            "POST",
            f"/proof/{session_id}/finalize",
            body={},
            timeout=20.0,
        )
        self.assertIn(status2, (200, 201), f"/proof finalize returned {status2}: {payload2}")

        # 4) Fetch proof record (source of truth)
        status3, proof_payload = http_json(self.base_url, "GET", f"/proof/{session_id}", timeout=10.0)
        self.assertEqual(status3, 200, f"/proof GET returned {status3}: {proof_payload}")
        self.assertIsInstance(proof_payload, dict)

        # Wrap the proof record into the v1 artifact contract
        proof_record = proof_payload
        artifact_obj = {
            "schema_version": "v1",
            "task": "QoD proof finalize (smoke test)",
            "summary": "Generated a proof record from intent + telemetry and wrapped it into the v1 artifact contract.",
            "outputs": {
                "proof_record": proof_record
            },
            "citations": [],
            "quality": {
                "has_telemetry": True,
                "validated_in_test": True
            }
        }

        # Ensure artifacts directory exists and write artifact.json
        artifacts_dir = self.repo_root / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        out_dir = artifacts_dir / str(session_id)
        out_dir.mkdir(parents=True, exist_ok=True)

        artifact_json_path = out_dir / "artifact.json"
        artifact_json_path.write_text(json.dumps(artifact_obj, indent=2), encoding="utf-8")

        # 5) Validate schema
        validate_artifact_json(
            artifact_json_path,
            self.repo_root / "schemas" / "artifact.v1.json",
        )