import json
import os
import subprocess
import time
import uuid
import unittest
from pathlib import Path

import requests


REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"

API_BASE = os.environ.get("QOD_API_BASE", "http://127.0.0.1:8010")
ARTIFACTS_ROOT = REPO_ROOT / "artifacts"

MAX_WAIT_SECONDS = 90
POLL_SECONDS = 2


def run(cmd, cwd=REPO_ROOT):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(
            f"Command failed: {' '.join(cmd)}\n\nSTDOUT:\n{p.stdout}\n\nSTDERR:\n{p.stderr}"
        )
    return p.stdout


def wait_for_health():
    deadline = time.time() + MAX_WAIT_SECONDS
    while time.time() < deadline:
        try:
            r = requests.get(API_BASE + "/health", timeout=2)
            if r.status_code == 200:
                return r.text
        except Exception:
            pass
        time.sleep(POLL_SECONDS)
    raise TimeoutError(f"API did not become healthy within {MAX_WAIT_SECONDS}s at {API_BASE}/health")


class TestIntegrationCompose(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ARTIFACTS_ROOT.mkdir(exist_ok=True)

        run(["docker", "compose", "-f", str(COMPOSE_FILE), "down", "--remove-orphans", "--volumes"])
        run(["docker", "compose", "-f", str(COMPOSE_FILE), "build"])
        run(["docker", "compose", "-f", str(COMPOSE_FILE), "up", "-d"])

        wait_for_health()

    @classmethod
    def tearDownClass(cls):
        # Keep compose running if debugging (so you can docker compose exec after failures)
        if os.environ.get("KEEP_COMPOSE_UP") == "1":
            return
        try:
            run(["docker", "compose", "-f", str(COMPOSE_FILE), "down", "--remove-orphans", "--volumes"])
        except Exception:
            pass

    def test_end_to_end_flow_creates_proof_and_artifacts(self):
        # 1) Create intent/session
        intent_payload = {
            "text": "turbo qos request",
            "target_p95_latency_ms": 200,
            "target_jitter_ms": 20,
            "duration_s": 30,
        }
        r = requests.post(API_BASE + "/intent", json=intent_payload, timeout=10)
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertIn("session_id", data, f"Missing session_id. Response: {data}")
        session_id = data["session_id"]

        # Basic sanity: looks like UUID
        uuid.UUID(session_id)

        # 2) Post telemetry (required)
        telemetry_payload = {
            "session_id": session_id,
            "n": 100,
            "p50_ms": 50.0,
            "p95_ms": 120.0,
            "jitter_ms": 10.0,
            "notes": "integration-test",
        }
        t = requests.post(API_BASE + "/telemetry", json=telemetry_payload, timeout=10)
        self.assertEqual(t.status_code, 200, t.text)

        # 3) Finalize proof
        f = requests.post(API_BASE + f"/proof/{session_id}/finalize", timeout=10)
        self.assertEqual(f.status_code, 200, f.text)
        finalize_data = f.json()
        self.assertTrue(isinstance(finalize_data, dict), f"Finalize JSON not an object: {finalize_data}")

        # 4) Verify proof exists via API
        g = requests.get(API_BASE + f"/proof/{session_id}", timeout=10)
        self.assertEqual(g.status_code, 200, g.text)

        # 5) Verify artifacts exist on host:
        # Your service writes flat files like:
        #   artifacts/proof_<session_id>_<timestamp>.json
        # not artifacts/<session_id>/artifact.json
        deadline = time.time() + 15
        proof_files = []
        while time.time() < deadline:
            proof_files = list(ARTIFACTS_ROOT.glob(f"proof_{session_id}_*.json"))
            if proof_files:
                break
            time.sleep(0.5)

        if not proof_files:
            # Dump helpful info before failing
            try:
                ls_host = "\n".join([p.name for p in sorted(ARTIFACTS_ROOT.glob("*"))])
                print("\n--- host artifacts dir listing ---\n", ls_host)

                ls_container = run(
                    ["docker", "compose", "-f", str(COMPOSE_FILE), "exec", "-T", "qod-api",
                     "sh", "-lc", "ls -la /app/artifacts || true"]
                )
                print("\n--- container /app/artifacts listing ---\n", ls_container)
            except Exception as e:
                print("\n(debug dump failed)", e)

        self.assertTrue(
            proof_files,
            f"Expected at least one proof file matching proof_{session_id}_*.json in {ARTIFACTS_ROOT}"
        )

        # Make sure each proof file is valid JSON (handle BOM)
        for p in proof_files:
            json.loads(p.read_text(encoding="utf-8-sig"))