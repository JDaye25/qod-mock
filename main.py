from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

DB_PATH = os.path.join(os.path.dirname(__file__), "qod_mock.sqlite3")

app = FastAPI(title="QoD Assurance Mock (Local)", version="0.1.0")

# ----------------------------
# DB helpers
# ----------------------------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
              session_id TEXT PRIMARY KEY,
              created_at REAL NOT NULL,
              intent_json TEXT NOT NULL,
              qos_profile TEXT NOT NULL,
              qos_status TEXT NOT NULL,
              provider_note TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS telemetry (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              session_id TEXT NOT NULL,
              created_at REAL NOT NULL,
              sample_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS proof_ledger (
              session_id TEXT PRIMARY KEY,
              created_at REAL NOT NULL,
              proof_json TEXT NOT NULL,
              prev_hash TEXT NOT NULL,
              this_hash TEXT NOT NULL
            )
            """
        )


@app.on_event("startup")
def _startup() -> None:
    init_db()


# ----------------------------
# Models
# ----------------------------
class Intent(BaseModel):
    """A simplified "SLO-like" intent. In real QoD, you would map this to offered QoS profiles."""
    target_p95_latency_ms: int = Field(..., ge=1, le=5000)
    target_jitter_ms: int = Field(..., ge=0, le=5000)
    duration_s: int = Field(..., ge=10, le=24 * 3600)
    flow_label: str = Field("demo-flow", min_length=1, max_length=200)


class TelemetrySample(BaseModel):
    """A minimal telemetry payload from a measurement agent."""
    session_id: str
    n: int = Field(..., ge=1, le=100000)
    p50_ms: float = Field(..., ge=0)
    p95_ms: float = Field(..., ge=0)
    jitter_ms: float = Field(..., ge=0)
    notes: str = Field("", max_length=500)


# ----------------------------
# "Operator QoD Provider" simulation
# ----------------------------
def choose_qos_profile(intent: Intent) -> str:
    # A toy mapping. Real QoD is profile-driven and operator-dependent.
    if intent.target_p95_latency_ms <= 50:
        return "QOS_LOW_LATENCY"
    if intent.target_p95_latency_ms <= 150:
        return "QOS_BALANCED"
    return "QOS_BEST_EFFORT"


def simulated_provider_create_session(qos_profile: str, duration_s: int) -> Dict[str, Any]:
    """
    Mimics the existence of a QoS session resource with a status.
    In the real CAMARA QoD API, you'd call POST /sessions and receive a resource + status.
    """
    session_id = str(uuid.uuid4())
    # Start as REQUESTED then become AVAILABLE shortly after.
    return {
        "sessionId": session_id,
        "qosProfile": qos_profile,
        "qosStatus": "REQUESTED",
        "duration_s": duration_s,
        "providerNote": "local-simulated-provider",
    }


def simulated_provider_current_status(created_at: float) -> str:
    # After ~2 seconds, mark it AVAILABLE.
    age = time.time() - created_at
    return "AVAILABLE" if age >= 2.0 else "REQUESTED"


# ----------------------------
# API endpoints
# ----------------------------
@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/intent")
def create_intent_and_session(intent: Intent) -> Dict[str, Any]:
    qos_profile = choose_qos_profile(intent)
    provider_resp = simulated_provider_create_session(qos_profile, intent.duration_s)
    created_at = time.time()
    session_id = provider_resp["sessionId"]

    with db() as conn:
        conn.execute(
            """
            INSERT INTO sessions(session_id, created_at, intent_json, qos_profile, qos_status, provider_note)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                created_at,
                intent.model_dump_json(),
                qos_profile,
                provider_resp["qosStatus"],
                provider_resp["providerNote"],
            ),
        )

    return {
        "session_id": session_id,
        "qos_profile": qos_profile,
        "qos_status": provider_resp["qosStatus"],
        "message": "Session created (simulated). It should become AVAILABLE in ~2s.",
    }


@app.get("/sessions")
def list_sessions() -> List[Dict[str, Any]]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM sessions ORDER BY created_at DESC").fetchall()

    out: List[Dict[str, Any]] = []
    for r in rows:
        created_at = float(r["created_at"])
        qos_status = simulated_provider_current_status(created_at)
        out.append(
            {
                "session_id": r["session_id"],
                "created_at": created_at,
                "qos_profile": r["qos_profile"],
                "qos_status": qos_status,
                "intent": json.loads(r["intent_json"]),
            }
        )
    return out


@app.get("/sessions/{session_id}")
def get_session(session_id: str) -> Dict[str, Any]:
    with db() as conn:
        r = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()

    if not r:
        raise HTTPException(status_code=404, detail="Unknown session_id")

    created_at = float(r["created_at"])
    qos_status = simulated_provider_current_status(created_at)

    return {
        "session_id": r["session_id"],
        "created_at": created_at,
        "qos_profile": r["qos_profile"],
        "qos_status": qos_status,
        "intent": json.loads(r["intent_json"]),
        "provider_note": r["provider_note"],
    }


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str) -> Dict[str, str]:
    with db() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM telemetry WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM proof_ledger WHERE session_id = ?", (session_id,))

    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Unknown session_id")

    return {"status": "deleted"}


@app.post("/telemetry")
def post_telemetry(sample: TelemetrySample) -> Dict[str, str]:
    # Ensure session exists
    with db() as conn:
        r = conn.execute(
            "SELECT 1 FROM sessions WHERE session_id = ?",
            (sample.session_id,),
        ).fetchone()

        if not r:
            raise HTTPException(status_code=404, detail="Unknown session_id")

        conn.execute(
            """
            INSERT INTO telemetry(session_id, created_at, sample_json)
            VALUES (?, ?, ?)
            """,
            (sample.session_id, time.time(), sample.model_dump_json()),
        )

    return {"status": "stored"}


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@app.post("/proof/{session_id}/finalize")
def finalize_proof(session_id: str) -> Dict[str, Any]:
    with db() as conn:
        sess = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()

        if not sess:
            raise HTTPException(status_code=404, detail="Unknown session_id")

        samples = conn.execute(
            "SELECT sample_json FROM telemetry WHERE session_id = ? ORDER BY created_at ASC",
            (session_id,),
        ).fetchall()

        prev = conn.execute(
            "SELECT this_hash FROM proof_ledger ORDER BY created_at DESC LIMIT 1"
        ).fetchone()

    prev_hash = (prev["this_hash"] if prev else "GENESIS")

    if not samples:
        raise HTTPException(status_code=400, detail="No telemetry samples found for this session")

    parsed = [json.loads(s["sample_json"]) for s in samples]
    avg_p50 = sum(p["p50_ms"] for p in parsed) / len(parsed)
    avg_p95 = sum(p["p95_ms"] for p in parsed) / len(parsed)
    avg_jitter = sum(p["jitter_ms"] for p in parsed) / len(parsed)

    intent = json.loads(sess["intent_json"])
    created_at = float(sess["created_at"])
    qos_status = simulated_provider_current_status(created_at)

    proof = {
        "session_id": session_id,
        "requested": {
            "intent": intent,
            "qos_profile": sess["qos_profile"],
        },
        "provider_observed": {
            "qos_status_at_finalize": qos_status,
            "provider_note": sess["provider_note"],
        },
        "measured_outcomes": {
            "samples_count": len(parsed),
            "avg_p50_ms": round(avg_p50, 2),
            "avg_p95_ms": round(avg_p95, 2),
            "avg_jitter_ms": round(avg_jitter, 2),
        },
        "created_at": time.time(),
    }

    proof_bytes = json.dumps(proof, sort_keys=True).encode("utf-8")
    this_hash = sha256_hex((prev_hash + "|").encode("utf-8") + proof_bytes)

    with db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO proof_ledger(session_id, created_at, proof_json, prev_hash, this_hash)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, time.time(), json.dumps(proof, sort_keys=True), prev_hash, this_hash),
        )

    return {"prev_hash": prev_hash, "this_hash": this_hash, "proof": proof}


@app.get("/proof/{session_id}")
def get_proof(session_id: str) -> Dict[str, Any]:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM proof_ledger WHERE session_id = ?",
            (session_id,),
        ).fetchone()

    if not row:
        raise HTTPException(
            status_code=404,
            detail="No proof record yet. Call /proof/{id}/finalize first.",
        )

    return {
        "session_id": session_id,
        "created_at": float(row["created_at"]),
        "prev_hash": row["prev_hash"],
        "this_hash": row["this_hash"],
        "proof": json.loads(row["proof_json"]),
    }