import json
import os
import time
import unittest
from pathlib import Path

import requests


class TestIntegrationCompose(unittest.TestCase):
    BASE_URL = os.getenv("TEST_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    API_TOKEN = os.getenv("QOD_API_TOKEN", "dev-token-123")
    ARTIFACTS_DIR = Path(os.getenv("QOD_ARTIFACTS_DIR", "artifacts"))

    @classmethod
    def setUpClass(cls):
        cls.headers = {
            "Authorization": f"Bearer {cls.API_TOKEN}",
            "Content-Type": "application/json",
        }

    def _post(self, path: str, payload: dict, expected_status: int = 200):
        r = requests.post(
            f"{self.BASE_URL}{path}",
            headers=self.headers,
            json=payload,
            timeout=30,
        )
        self.assertEqual(r.status_code, expected_status, r.text)
        return r

    def _get(self, path: str, expected_status: int = 200):
        r = requests.get(
            f"{self.BASE_URL}{path}",
            headers=self.headers,
            timeout=30,
        )
        self.assertEqual(r.status_code, expected_status, r.text)
        return r

    def test_end_to_end_flow_creates_proof_and_artifacts(self):
        intent_payload = {
            "text": "compose integration test",
            "target_p95_latency_ms": 120,
            "target_jitter_ms": 25,
            "duration_s": 15,
            "flow_label": "compose-integration",
        }

        r = self._post("/intent", intent_payload)
        intent_data = r.json()

        session_id = intent_data["session_id"]
        self.assertTrue(session_id)
        self.assertIn("qos_profile", intent_data)
        self.assertIn("qos_status", intent_data)

        telemetry_samples = [
            {
                "session_id": session_id,
                "n": 100,
                "p50_ms": 40,
                "p95_ms": 90,
                "jitter_ms": 10,
                "notes": "sample-1",
            },
            {
                "session_id": session_id,
                "n": 100,
                "p50_ms": 45,
                "p95_ms": 110,
                "jitter_ms": 12,
                "notes": "sample-2",
            },
            {
                "session_id": session_id,
                "n": 100,
                "p50_ms": 42,
                "p95_ms": 100,
                "jitter_ms": 11,
                "notes": "sample-3",
            },
        ]

        for sample in telemetry_samples:
            tr = self._post("/telemetry", sample)
            self.assertEqual(tr.json()["status"], "stored")

        time.sleep(2.5)

        fr = requests.post(
            f"{self.BASE_URL}/proof/{session_id}/finalize",
            headers={"Authorization": f"Bearer {self.API_TOKEN}"},
            timeout=30,
        )
        self.assertEqual(fr.status_code, 200, fr.text)
        final_data = fr.json()

        self.assertIn("prev_hash", final_data)
        self.assertIn("this_hash", final_data)
        self.assertIn("signature", final_data)
        self.assertIn("kid", final_data)
        self.assertIn("proof", final_data)

        vr = self._get(f"/proof/{session_id}/verify")
        verify_data = vr.json()

        self.assertTrue(verify_data["verified"], json.dumps(verify_data, indent=2))
        self.assertEqual(verify_data["reason"], "ok")
        self.assertTrue(verify_data["runtime_artifact_verified"])
        self.assertTrue(verify_data["ledger_hash_verified"])
        self.assertTrue(verify_data["chain_continuity_verified"])
        self.assertTrue(verify_data["signature_verified"])

        runtime_artifact_path = self.ARTIFACTS_DIR / session_id / "artifact.json"
        wrapper_artifact_path = self.ARTIFACTS_DIR / session_id / "artifact_v1.json"

        self.assertTrue(
            runtime_artifact_path.exists(),
            f"Missing runtime artifact: {runtime_artifact_path}",
        )
        self.assertTrue(
            wrapper_artifact_path.exists(),
            f"Missing wrapper artifact: {wrapper_artifact_path}",
        )

        proof_files = list(self.ARTIFACTS_DIR.glob(f"proof_{session_id}_*.json"))
        self.assertTrue(
            proof_files,
            f"No proof artifact files found in {self.ARTIFACTS_DIR} for session {session_id}",
        )