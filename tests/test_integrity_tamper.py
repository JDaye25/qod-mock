import json
import os
import sqlite3
import hashlib
import time
import unittest
import urllib.request
import urllib.error
import subprocess
from pathlib import Path


def _http_json(method: str, url: str, body: dict | None = None) -> dict:
    data = None
    headers = {"Content-Type": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        raise AssertionError(f"HTTP {e.code} for {method} {url}: {raw}") from e


def _url_ok(url: str, timeout_s: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def _wait_ready(base: str, timeout_s: int = 60) -> None:
    ready_url = f"{base}/ready"
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(ready_url, timeout=2) as resp:
                if 200 <= resp.status < 300:
                    return
                last = f"HTTP {resp.status}"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(1)
    raise AssertionError(f"Service never became ready at {ready_url}. Last seen: {last}")


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(
            "Command failed:\n"
            f"  cmd: {' '.join(cmd)}\n"
            f"  rc: {proc.returncode}\n"
            f"  stdout:\n{proc.stdout}\n"
            f"  stderr:\n{proc.stderr}\n"
        )


def _compose_up() -> None:
    # Use docker compose (v2). Assumes docker is installed on the machine/CI runner.
    _run(["docker", "compose", "up", "-d", "--build"])
    # In your compose, published port is 8010.
    _wait_ready("http://127.0.0.1:8010", timeout_s=60)


def _compose_down() -> None:
    _run(["docker", "compose", "down", "--remove-orphans"])


def _get_base_url_and_ensure_running() -> tuple[str, bool]:
    """
    Returns (base_url, started_compose_bool)
    started_compose_bool indicates whether THIS test started compose (so we may optionally tear down).
    """
    # 1) Explicit override
    if os.getenv("QOD_BASE_URL"):
        base = os.environ["QOD_BASE_URL"].rstrip("/")
        _wait_ready(base, timeout_s=30)
        return base, False

    # 2) Probe common bases
    candidates = ["http://127.0.0.1:8010", "http://127.0.0.1:8000"]
    for base in candidates:
        if _url_ok(f"{base}/health", timeout_s=2) or _url_ok(f"{base}/ready", timeout_s=2):
            _wait_ready(base, timeout_s=30)
            return base, False

    # 3) Not reachable → bring up compose ourselves
    _compose_up()
    return "http://127.0.0.1:8010", True


def canonical_bytes(obj) -> bytes:
    # MUST match server canonical_json_bytes
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def find_db_path() -> Path:
    env_path = os.getenv("QOD_DB_PATH", "").strip()
    if env_path:
        p = Path(env_path)
        if str(p).startswith("/app/data/"):
            candidate = Path("data") / p.name
            if candidate.exists():
                return candidate
        if p.exists():
            return p

    candidates = [
        Path("data") / "qod_mock.sqlite3",
        Path("backend") / "qod_mock.sqlite3",
        Path("qod_mock.sqlite3"),
    ]
    for c in candidates:
        if c.exists():
            return c

    raise AssertionError(
        "Could not locate sqlite DB. Looked in data/qod_mock.sqlite3, backend/qod_mock.sqlite3, ./qod_mock.sqlite3"
    )


def find_runtime_artifact_path(session_id: str) -> Path:
    artifacts_dir = Path(os.getenv("QOD_ARTIFACTS_DIR", "artifacts"))
    p = artifacts_dir / session_id / "artifact.json"
    if p.exists():
        return p

    if artifacts_dir.exists():
        matches = list(artifacts_dir.rglob("artifact.json"))
        for m in matches:
            if session_id in str(m):
                return m

    raise AssertionError(f"Could not locate runtime artifact for session {session_id} under {artifacts_dir}")


class TestIntegrityTamper(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base, cls._started_compose = _get_base_url_and_ensure_running()

    @classmethod
    def tearDownClass(cls) -> None:
        # Optional teardown. Default is to leave services running (faster dev loop).
        # Set QOD_COMPOSE_DOWN=1 to auto-stop compose after tests.
        if getattr(cls, "_started_compose", False) and os.getenv("QOD_COMPOSE_DOWN", "") == "1":
            _compose_down()

    def _create_session_and_finalize(self) -> str:
        intent = {
            "text": "integrity tamper test",
            "target_p95_latency_ms": 120,
            "target_jitter_ms": 10,
            "duration_s": 60,
            "flow_label": "integrity-tamper",
        }
        resp = _http_json("POST", f"{self.base}/intent", intent)
        sid = resp["session_id"]

        telemetry = {
            "session_id": sid,
            "n": 100,
            "p50_ms": 40,
            "p95_ms": 110,
            "jitter_ms": 8,
            "notes": "sample-1",
        }
        _http_json("POST", f"{self.base}/telemetry", telemetry)

        _http_json("POST", f"{self.base}/proof/{sid}/finalize", {})

        v = _http_json("GET", f"{self.base}/proof/{sid}/verify")
        self.assertTrue(v.get("verified"), f"Expected verified True, got: {v}")
        self.assertEqual(v.get("reason"), "ok", f"Expected reason ok, got: {v}")
        return sid

    def test_db_tamper_signature_mismatch(self):
        sid = self._create_session_and_finalize()

        db_path = find_db_path()
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute(
            "SELECT proof_json, prev_hash, this_hash, signature FROM proof_ledger WHERE session_id = ?",
            (sid,),
        )
        row = cur.fetchone()
        self.assertIsNotNone(row, f"No proof_ledger row found for session_id={sid}")

        proof = json.loads(row["proof_json"])
        prev_hash = row["prev_hash"]
        old_sig = row["signature"]

        proof.setdefault("provider_observed", {})
        proof["provider_observed"]["provider_note"] = "evil-simulated-provider"

        msg = (str(prev_hash) + "|").encode("utf-8") + canonical_bytes(proof)
        new_this_hash = sha256_hex(msg)

        new_proof_json = json.dumps(proof, sort_keys=True)
        cur.execute(
            "UPDATE proof_ledger SET proof_json = ?, this_hash = ? WHERE session_id = ?",
            (new_proof_json, new_this_hash, sid),
        )
        conn.commit()
        conn.close()

        v = _http_json("GET", f"{self.base}/proof/{sid}/verify")
        self.assertFalse(v.get("verified"), f"Expected verified False, got: {v}")
        self.assertEqual(v.get("reason"), "signature_mismatch", f"Expected signature_mismatch, got: {v}")
        self.assertEqual(v.get("ledger_hash_verified"), True, f"Expected ledger_hash_verified True, got: {v}")
        self.assertEqual(v.get("signature_verified"), False, f"Expected signature_verified False, got: {v}")
        self.assertTrue(old_sig, "Expected stored signature to be non-empty")

    def test_runtime_artifact_tamper_runtime_hash_mismatch(self):
        sid = self._create_session_and_finalize()

        runtime_path = find_runtime_artifact_path(sid)

        obj = json.loads(runtime_path.read_text(encoding="utf-8"))
        obj["tampered"] = True
        runtime_path.write_text(json.dumps(obj, indent=2), encoding="utf-8")

        v = _http_json("GET", f"{self.base}/proof/{sid}/verify")
        self.assertFalse(v.get("verified"), f"Expected verified False after artifact tamper, got: {v}")
        self.assertEqual(v.get("runtime_artifact_verified"), False, f"Expected runtime_artifact_verified False, got: {v}")
        self.assertNotEqual(v.get("reason"), "ok", f"Expected non-ok reason after tamper, got: {v}")


if __name__ == "__main__":
    unittest.main()