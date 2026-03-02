import os
import time
import glob
import unittest
import urllib.request
import urllib.error
import json

BASE_URL = os.getenv("QOD_API_BASE_URL", "http://127.0.0.1:8000")


def http_json(method: str, path: str, payload=None, timeout=10):
    url = BASE_URL + path
    data = None
    headers = {"Content-Type": "application/json"}

    if payload is not None:
        data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        raise AssertionError(f"{method} {path} failed: {e.code} {body}") from e


class SmokeTestQoD(unittest.TestCase):
    def test_end_to_end_session_creates_artifact(self):
        # 1) health
        status, _ = http_json("GET", "/health")
        self.assertEqual(status, 200)

        # 2) create intent -> session
        intent_payload = {
            "target_p95_latency_ms": 80,
            "target_jitter_ms": 10,
            "duration_s": 60,
            "flow_label": "smoke-test-flow",
        }
        status, resp = http_json("POST", "/intent", intent_payload)
        self.assertEqual(status, 200)
        self.assertIn("session_id", resp)
        session_id = resp["session_id"]

        # 3) telemetry
        telemetry_payload = {
            "session_id": session_id,
            "n": 10,
            "p50_ms": 40,
            "p95_ms": 70,
            "jitter_ms": 8,
            "notes": "smoke test",
        }
        status, _ = http_json("POST", "/telemetry", telemetry_payload)
        self.assertEqual(status, 200)

        # give the app a moment to write artifacts if needed
        time.sleep(0.2)

        # 4) finalize proof (should also save artifact)
        status, finalize = http_json("POST", f"/proof/{session_id}/finalize")
        self.assertEqual(status, 200)
        self.assertIn("proof", finalize)

        # 5) verify artifact file exists
        matches = glob.glob(f"artifacts/proof_{session_id}_*.json")
        self.assertTrue(matches, f"No artifact file found for session {session_id} in artifacts/")

        # (optional) verify the artifact contains the same session_id
        with open(matches[-1], "r", encoding="utf-8") as f:
            artifact = json.load(f)
        self.assertEqual(artifact["proof"]["session_id"], session_id)


if __name__ == "__main__":
    unittest.main()