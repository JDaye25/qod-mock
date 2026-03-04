from __future__ import annotations

from dotenv import load_dotenv
import os

load_dotenv()

import json
import sqlite3
import time
import uuid
import logging
import platform
import traceback
import hashlib
import hmac
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ConfigDict, model_validator

from backend.obs import setup_logging


# ----------------------------
# Paths / build identifiers
# ----------------------------
ARTIFACTS_DIR = Path(os.getenv("QOD_ARTIFACTS_DIR", "artifacts")).resolve()
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

RUN_SUMMARIES_DIR = ARTIFACTS_DIR / "run_summaries"
RUN_SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)

BUILD_GIT_SHA = os.getenv("GIT_SHA", os.getenv("QOD_GIT_SHA", "unknown"))
BUILD_IMAGE_TAG = os.getenv("IMAGE_TAG", os.getenv("QOD_IMAGE_TAG", "unknown"))


def utc_now_iso_z() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_run_summary(summary: dict) -> str:
    RUN_SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    sid = summary.get("session_id", "unknown")
    out = RUN_SUMMARIES_DIR / f"run_{ts}_{sid}.json"
    out.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    return str(out)


# ----------------------------
# Deterministic hashing helpers
# ----------------------------
def canonical_json_bytes(obj: Any) -> bytes:
    """
    Deterministic JSON encoding so hashes are stable across platforms/formatting.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(obj: Any) -> str:
    return sha256_hex(canonical_json_bytes(obj))


# ----------------------------
# Signing helpers (HMAC)
# ----------------------------
def _signing_key_bytes() -> Optional[bytes]:
    key = os.getenv("QOD_SIGNING_KEY", "").strip()
    if not key:
        return None
    return key.encode("utf-8")


def hmac_signature_hex(message: str) -> Optional[str]:
    """
    Returns hex HMAC-SHA256(signature_key, message) or None if key missing.
    We sign the ledger entry's this_hash (not the whole proof) to keep it simple and stable.
    """
    key = _signing_key_bytes()
    if key is None:
        return None
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).hexdigest()


# ----------------------------
# App + logging
# ----------------------------
app = FastAPI(title="QoD Assurance Mock (Local)", version="0.1.0")

logger = setup_logging(ARTIFACTS_DIR)
log = logging.getLogger("qod")
log.info("QoD service starting up")

try:
    from backend.logging_redact import configure_redaction  # type: ignore

    configure_redaction()
    log.info("Log redaction filter enabled")
except Exception:
    log.info("Log redaction filter not enabled (backend.logging_redact not found)")


# ----------------------------
# Request size limit middleware (optional)
# ----------------------------
try:
    from backend.middleware.limits import MaxBodySizeMiddleware  # type: ignore

    MAX_BODY_BYTES = int(os.getenv("MAX_BODY_BYTES", str(1_000_000)))
    app.add_middleware(MaxBodySizeMiddleware, max_bytes=MAX_BODY_BYTES)
    log.info("Max body size middleware enabled: %s bytes", MAX_BODY_BYTES)
except Exception:
    log.info("Max body size middleware not enabled (backend.middleware.limits not found)")


# ----------------------------
# Request ID + structured request logging
# ----------------------------
@app.middleware("http")
async def add_request_id_and_log(request: Request, call_next):
    request_id = (
        request.headers.get("x-request-id")
        or request.headers.get("x-correlation-id")
        or str(uuid.uuid4())
    )
    start = time.time()

    try:
        response = await call_next(request)
    except Exception as e:
        duration_ms = int((time.time() - start) * 1000)
        logger.exception(
            "request_failed",
            extra={
                "event": "http_request",
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": 500,
                "duration_ms": duration_ms,
                "error": str(e),
            },
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal Server Error", "request_id": request_id},
        )

    duration_ms = int((time.time() - start) * 1000)
    logger.info(
        "request",
        extra={
            "event": "http_request",
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
        },
    )
    response.headers["x-request-id"] = request_id
    return response


# ----------------------------
# CORS
# ----------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ----------------------------
# DB config + helpers
# ----------------------------
DB_PATH = os.getenv("QOD_DB_PATH") or os.path.join(os.path.dirname(__file__), "qod_mock.sqlite3")
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_proof_ledger_signature_column(conn: sqlite3.Connection) -> None:
    """
    SQLite-friendly "migration": add signature column if missing.
    """
    cols = conn.execute("PRAGMA table_info(proof_ledger);").fetchall()
    colnames = {c["name"] for c in cols}
    if "signature" not in colnames:
        conn.execute("ALTER TABLE proof_ledger ADD COLUMN signature TEXT NOT NULL DEFAULT '';")
        log.info("DB migration applied: added proof_ledger.signature column")


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
              this_hash TEXT NOT NULL,
              signature TEXT NOT NULL
            )
            """
        )
        _ensure_proof_ledger_signature_column(conn)


@app.on_event("startup")
def _startup() -> None:
    init_db()


# ----------------------------
# Readiness checks
# ----------------------------
def _sqlite_ready() -> Optional[str]:
    try:
        with db() as conn:
            conn.execute("SELECT 1").fetchone()

            needed = {"sessions", "telemetry", "proof_ledger"}
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()
            have = {r["name"] for r in rows}
            missing = sorted(list(needed - have))
            if missing:
                return f"DB schema not ready, missing tables: {missing}"

            cols = conn.execute("PRAGMA table_info(proof_ledger);").fetchall()
            colnames = {c["name"] for c in cols}
            if "signature" not in colnames:
                return "DB schema not ready: proof_ledger.signature column missing (restart server to auto-migrate)"
        return None
    except Exception as e:
        return f"SQLite readiness failed: {type(e).__name__}: {e}"


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok", "check": "liveness"}


@app.get("/ready")
def ready(response: Response) -> Dict[str, Any]:
    start = time.time()
    problems: List[str] = []

    db_problem = _sqlite_ready()
    if db_problem:
        problems.append(db_problem)

    elapsed_ms = int((time.time() - start) * 1000)

    if problems:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not-ready", "check": "readiness", "elapsed_ms": elapsed_ms, "problems": problems}

    return {"status": "ok", "check": "readiness", "elapsed_ms": elapsed_ms}


# ----------------------------
# Models
# ----------------------------
class Intent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=1, max_length=5000)
    target_p95_latency_ms: int = Field(..., ge=1, le=60_000)
    target_jitter_ms: int = Field(..., ge=0, le=60_000)
    duration_s: int = Field(..., ge=1, le=24 * 3600)
    flow_label: str = Field("demo-flow", min_length=1, max_length=200)


class TelemetrySample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    n: int = Field(..., ge=1, le=100_000)
    p50_ms: float = Field(..., ge=0, le=60_000)
    p95_ms: float = Field(..., ge=0, le=60_000)
    jitter_ms: float = Field(..., ge=0, le=60_000)
    notes: str = Field("", max_length=500)

    @model_validator(mode="after")
    def _sanity(self):
        if self.p95_ms < self.p50_ms:
            raise ValueError("Invalid telemetry: p95_ms must be >= p50_ms.")
        return self


# ----------------------------
# Provider simulation
# ----------------------------
def choose_qos_profile(intent: Intent) -> str:
    if intent.target_p95_latency_ms <= 50:
        return "QOS_LOW_LATENCY"
    if intent.target_p95_latency_ms <= 150:
        return "QOS_BALANCED"
    return "QOS_BEST_EFFORT"


def map_qos_to_schema_enum(qos_profile: str) -> str:
    mapping = {
        "QOS_LOW_LATENCY": "turbo",
        "QOS_BALANCED": "standard",
        "QOS_BEST_EFFORT": "strict",
    }
    return mapping.get(qos_profile, "standard")


def simulated_provider_create_session(qos_profile: str, duration_s: int) -> Dict[str, Any]:
    session_id = str(uuid.uuid4())
    return {
        "sessionId": session_id,
        "qosProfile": qos_profile,
        "qosStatus": "REQUESTED",
        "duration_s": duration_s,
        "providerNote": "local-simulated-provider",
    }


def simulated_provider_current_status(created_at: float) -> str:
    age = time.time() - created_at
    return "AVAILABLE" if age >= 2.0 else "REQUESTED"


# ----------------------------
# API endpoints
# ----------------------------
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
def get_session(session_id: UUID) -> Dict[str, Any]:
    sid = str(session_id)

    with db() as conn:
        r = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (sid,)).fetchone()

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
def delete_session(session_id: UUID) -> Dict[str, str]:
    sid = str(session_id)

    with db() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))
        conn.execute("DELETE FROM telemetry WHERE session_id = ?", (sid,))
        conn.execute("DELETE FROM proof_ledger WHERE session_id = ?", (sid,))

    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Unknown session_id")

    return {"status": "deleted"}


@app.post("/telemetry")
def post_telemetry(sample: TelemetrySample) -> Dict[str, str]:
    sid = str(sample.session_id)

    with db() as conn:
        r = conn.execute("SELECT 1 FROM sessions WHERE session_id = ?", (sid,)).fetchone()
        if not r:
            raise HTTPException(
                status_code=404,
                detail="Unknown session_id. Create a session first via POST /intent.",
            )

        conn.execute(
            """
            INSERT INTO telemetry(session_id, created_at, sample_json)
            VALUES (?, ?, ?)
            """,
            (sid, time.time(), sample.model_dump_json()),
        )

    return {"status": "stored"}


@app.post("/proof/{session_id}/finalize")
def finalize_proof(session_id: UUID, request: Request) -> Dict[str, Any]:
    sid = str(session_id)

    request_id = (
        request.headers.get("x-request-id")
        or request.headers.get("x-correlation-id")
        or str(uuid.uuid4())
    )

    start_ts = time.time()
    summary: Dict[str, Any] = {
        "run_type": "finalize_proof",
        "request_id": request_id,
        "session_id": sid,
        "build": {"git_sha": BUILD_GIT_SHA, "image_tag": BUILD_IMAGE_TAG},
        "timestamps": {"start_utc": utc_now_iso_z(), "end_utc": None, "duration_ms": None},
        "result": {"success": False, "reason": None},
        "ids": {
            "prev_hash": None,
            "this_hash": None,
            "signature": None,
            "proof_artifact_path": None,
            "runtime_artifact_path": None,
            "wrapper_artifact_path": None,
            "run_summary_path": None,
        },
        "env": {"hostname": platform.node()},
    }

    try:
        with db() as conn:
            sess = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (sid,)).fetchone()
            if not sess:
                raise HTTPException(status_code=404, detail="Unknown session_id")

            samples = conn.execute(
                "SELECT sample_json FROM telemetry WHERE session_id = ? ORDER BY created_at ASC",
                (sid,),
            ).fetchall()

            prev = conn.execute(
                "SELECT this_hash FROM proof_ledger ORDER BY created_at DESC LIMIT 1"
            ).fetchone()

        prev_hash = (prev["this_hash"] if prev else "GENESIS")
        summary["ids"]["prev_hash"] = prev_hash

        if not samples:
            raise HTTPException(
                status_code=400,
                detail="No telemetry samples found for this session. POST /telemetry first.",
            )

        parsed = [json.loads(s["sample_json"]) for s in samples]
        if not parsed:
            raise HTTPException(status_code=400, detail="Telemetry payloads could not be parsed.")

        avg_p50 = round(sum(p["p50_ms"] for p in parsed) / len(parsed), 2)
        avg_p95 = round(sum(p["p95_ms"] for p in parsed) / len(parsed), 2)
        avg_jitter = round(sum(p["jitter_ms"] for p in parsed) / len(parsed), 2)

        avg_p50 = min(avg_p50, 5000.0)
        avg_p95 = min(avg_p95, 5000.0)
        avg_jitter = min(avg_jitter, 5000.0)

        intent = json.loads(sess["intent_json"])
        created_at_epoch = float(sess["created_at"])
        qos_status = simulated_provider_current_status(created_at_epoch)

        session_dir = ARTIFACTS_DIR / sid
        session_dir.mkdir(parents=True, exist_ok=True)

        max_latency_ms = int(min(max(1, int(intent.get("target_p95_latency_ms", 100))), 5000))
        min_throughput_mbps = 0.1
        min_availability_pct = 0.0

        reasons: List[str] = []
        passed = True

        if avg_p95 <= max_latency_ms:
            reasons.append(f"latency {avg_p95}ms <= target max_latency_ms {max_latency_ms}ms")
        else:
            passed = False
            reasons.append(f"latency {avg_p95}ms > target max_latency_ms {max_latency_ms}ms")

        reasons.append(f"jitter observed {avg_jitter}ms (informational)")

        runtime_artifact = {
            "schema_version": "v1",
            "session_id": sid,
            "created_at": utc_now_iso_z(),
            "qos_profile": map_qos_to_schema_enum(str(sess["qos_profile"])),
            "inputs": {
                "targets": {
                    "max_latency_ms": max_latency_ms,
                    "min_throughput_mbps": float(min_throughput_mbps),
                    "min_availability_pct": float(min_availability_pct),
                },
                "network": {"msisdn": "", "ip_address": "", "country": ""},
            },
            "measured": {
                "latency_ms": float(avg_p95),
                "throughput_mbps": 0.0,
                "availability_pct": 100.0,
                "jitter_ms": float(avg_jitter),
                "packet_loss_pct": 0.0,
            },
            "decision": {"result": "pass" if passed else "fail", "reasons": reasons},
        }

        runtime_artifact_sha256 = sha256_json(runtime_artifact)

        proof = {
            "session_id": sid,
            "requested": {"intent": intent, "qos_profile": sess["qos_profile"]},
            "provider_observed": {
                "qos_status_at_finalize": qos_status,
                "provider_note": sess["provider_note"],
            },
            "measured_outcomes": {
                "samples_count": len(parsed),
                "avg_p50_ms": avg_p50,
                "avg_p95_ms": avg_p95,
                "avg_jitter_ms": avg_jitter,
            },
            "artifacts": {
                "runtime_artifact_sha256": runtime_artifact_sha256,
                "runtime_artifact_relpath": f"{sid}/artifact.json",
            },
            "created_at": utc_now_iso_z(),
        }

        proof_bytes = canonical_json_bytes(proof)
        this_hash = sha256_hex((prev_hash + "|").encode("utf-8") + proof_bytes)
        summary["ids"]["this_hash"] = this_hash

        signature = hmac_signature_hex(this_hash)
        if signature is None:
            raise HTTPException(
                status_code=500,
                detail="QOD_SIGNING_KEY is not set. Set it and restart the server before finalizing proofs.",
            )
        summary["ids"]["signature"] = signature

        with db() as conn:
            _ensure_proof_ledger_signature_column(conn)
            conn.execute(
                """
                INSERT OR REPLACE INTO proof_ledger(session_id, created_at, proof_json, prev_hash, this_hash, signature)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (sid, time.time(), json.dumps(proof, sort_keys=True), prev_hash, this_hash, signature),
            )

        response_obj = {"prev_hash": prev_hash, "this_hash": this_hash, "signature": signature, "proof": proof}

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

        proof_path = ARTIFACTS_DIR / f"proof_{sid}_{timestamp}.json"
        proof_path.write_text(json.dumps(response_obj, indent=2), encoding="utf-8")
        summary["ids"]["proof_artifact_path"] = str(proof_path)

        runtime_path = session_dir / "artifact.json"
        runtime_path.write_text(json.dumps(runtime_artifact, indent=2), encoding="utf-8")
        summary["ids"]["runtime_artifact_path"] = str(runtime_path)

        wrapper_artifact = {
            "schema_version": "v1",
            "task": "QoD proof finalize (wrapper)",
            "summary": "Generated proof record and runtime contract artifact.",
            "outputs": {"proof_record": response_obj},
            "citations": [],
            "quality": {"has_telemetry": True, "validated_in_test": True},
        }

        wrapper_path = session_dir / "artifact_v1.json"
        wrapper_path.write_text(json.dumps(wrapper_artifact, indent=2), encoding="utf-8")
        summary["ids"]["wrapper_artifact_path"] = str(wrapper_path)

        summary["result"]["success"] = True
        summary["result"]["reason"] = "ok"

        log.info("Saved proof artifact to %s", proof_path)
        log.info("Saved runtime artifact to %s", runtime_path)
        log.info("Saved wrapper artifact to %s", wrapper_path)

        return response_obj

    except HTTPException as e:
        summary["result"]["success"] = False
        summary["result"]["reason"] = f"http_{e.status_code}: {e.detail}"
        raise

    except Exception:
        summary["result"]["success"] = False
        summary["result"]["reason"] = "exception"
        summary["exception"] = traceback.format_exc()
        raise

    finally:
        end_ts = time.time()
        summary["timestamps"]["end_utc"] = utc_now_iso_z()
        summary["timestamps"]["duration_ms"] = int((end_ts - start_ts) * 1000)

        try:
            path = write_run_summary(summary)
            summary["ids"]["run_summary_path"] = path
            log.info("Wrote run summary to %s", path)
        except Exception:
            log.exception("Failed to write run summary")


@app.get("/proof/{session_id}")
def get_proof(session_id: UUID) -> Dict[str, Any]:
    sid = str(session_id)

    with db() as conn:
        row = conn.execute("SELECT * FROM proof_ledger WHERE session_id = ?", (sid,)).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="No proof record yet. Call /proof/{id}/finalize first.")

    signature = row["signature"] if "signature" in row.keys() else ""

    return {
        "session_id": sid,
        "created_at": float(row["created_at"]),
        "prev_hash": row["prev_hash"],
        "this_hash": row["this_hash"],
        "signature": signature,
        "proof": json.loads(row["proof_json"]),
    }


@app.get("/proof/{session_id}/bundle")
def proof_bundle(session_id: UUID) -> Dict[str, Any]:
    sid = str(session_id)
    with db() as conn:
        row = conn.execute(
            "SELECT proof_json, prev_hash, this_hash, signature, created_at FROM proof_ledger WHERE session_id = ?",
            (sid,),
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="No proof record found for this session_id. Finalize first.")

    signature = row["signature"] if "signature" in row.keys() else ""
    proof = json.loads(row["proof_json"])

    return {
        "session_id": sid,
        "ledger": {
            "created_at": float(row["created_at"]),
            "prev_hash": row["prev_hash"],
            "this_hash": row["this_hash"],
            "signature": signature,
        },
        "proof": proof,
    }


# ----------------------------
# Verify proof endpoint (BOM-safe + ledger integrity + continuity + signature)
# ----------------------------
@app.get("/proof/{session_id}/verify")
def verify_proof(session_id: UUID) -> Dict[str, Any]:
    sid = str(session_id)

    with db() as conn:
        row = conn.execute(
            "SELECT proof_json, prev_hash, this_hash, signature, created_at FROM proof_ledger WHERE session_id = ?",
            (sid,),
        ).fetchone()

        prev_row = conn.execute(
            """
            SELECT this_hash FROM proof_ledger
            WHERE created_at < (SELECT created_at FROM proof_ledger WHERE session_id = ?)
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (sid,),
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="No proof record found for this session_id. Finalize first.")

    proof = json.loads(row["proof_json"])
    prev_hash = row["prev_hash"]
    stored_this_hash = row["this_hash"]
    stored_signature = row["signature"] if "signature" in row.keys() else ""

    computed_this_hash = sha256_hex((str(prev_hash) + "|").encode("utf-8") + canonical_json_bytes(proof))
    ledger_hash_verified = (computed_this_hash == stored_this_hash)

    if prev_hash == "GENESIS":
        chain_continuity_verified = True
        expected_prev_this_hash = None
    else:
        expected_prev_this_hash = prev_row["this_hash"] if prev_row else None
        chain_continuity_verified = (expected_prev_this_hash is not None and prev_hash == expected_prev_this_hash)

    computed_signature = hmac_signature_hex(stored_this_hash)
    if computed_signature is None:
        signature_verified = False
        signature_reason = "signing_key_missing"
    else:
        signature_verified = (stored_signature == computed_signature and stored_signature != "")
        signature_reason = "ok" if signature_verified else "signature_mismatch"

    claimed_runtime = ((proof.get("artifacts") or {}).get("runtime_artifact_sha256")) or None
    relpath = ((proof.get("artifacts") or {}).get("runtime_artifact_relpath")) or f"{sid}/artifact.json"

    if not claimed_runtime:
        return {
            "session_id": sid,
            "verified": False,
            "reason": "proof_missing_runtime_hash",
            "runtime_artifact_verified": False,
            "ledger_hash_verified": ledger_hash_verified,
            "chain_continuity_verified": chain_continuity_verified,
            "signature_verified": signature_verified,
            "signature_reason": signature_reason,
            "proof_prev_hash": prev_hash,
            "expected_prev_this_hash": expected_prev_this_hash,
            "proof_this_hash_stored": stored_this_hash,
            "proof_this_hash_computed": computed_this_hash,
            "proof_signature_stored": stored_signature,
            "proof_signature_computed": computed_signature,
            "proof_created_at": float(row["created_at"]),
        }

    runtime_path = ARTIFACTS_DIR / sid / "artifact.json"
    if not runtime_path.exists():
        return {
            "session_id": sid,
            "verified": False,
            "reason": "runtime_artifact_missing",
            "runtime_artifact_verified": False,
            "ledger_hash_verified": ledger_hash_verified,
            "chain_continuity_verified": chain_continuity_verified,
            "signature_verified": signature_verified,
            "signature_reason": signature_reason,
            "runtime_artifact_path": str(runtime_path),
            "claimed_runtime_sha256": claimed_runtime,
            "proof_prev_hash": prev_hash,
            "expected_prev_this_hash": expected_prev_this_hash,
            "proof_this_hash_stored": stored_this_hash,
            "proof_this_hash_computed": computed_this_hash,
            "proof_signature_stored": stored_signature,
            "proof_signature_computed": computed_signature,
            "proof_created_at": float(row["created_at"]),
        }

    try:
        runtime_text = runtime_path.read_text(encoding="utf-8-sig")
        runtime_artifact = json.loads(runtime_text)
    except Exception as e:
        return {
            "session_id": sid,
            "verified": False,
            "reason": "runtime_artifact_invalid_json",
            "runtime_artifact_verified": False,
            "ledger_hash_verified": ledger_hash_verified,
            "chain_continuity_verified": chain_continuity_verified,
            "signature_verified": signature_verified,
            "signature_reason": signature_reason,
            "runtime_artifact_path": str(runtime_path),
            "claimed_runtime_sha256": claimed_runtime,
            "proof_prev_hash": prev_hash,
            "expected_prev_this_hash": expected_prev_this_hash,
            "proof_this_hash_stored": stored_this_hash,
            "proof_this_hash_computed": computed_this_hash,
            "proof_signature_stored": stored_signature,
            "proof_signature_computed": computed_signature,
            "proof_created_at": float(row["created_at"]),
            "error": f"{type(e).__name__}: {e}",
        }

    computed_runtime = sha256_json(runtime_artifact)
    runtime_artifact_verified = (computed_runtime == claimed_runtime)

    verified = bool(runtime_artifact_verified and ledger_hash_verified and chain_continuity_verified and signature_verified)

    if verified:
        reason = "ok"
    elif not signature_verified:
        reason = "signature_mismatch" if signature_reason == "signature_mismatch" else "signing_key_missing"
    elif not ledger_hash_verified:
        reason = "ledger_hash_mismatch"
    elif not chain_continuity_verified:
        reason = "ledger_chain_broken"
    else:
        reason = "runtime_hash_mismatch"

    return {
        "session_id": sid,
        "verified": verified,
        "reason": reason,
        "runtime_artifact_verified": runtime_artifact_verified,
        "ledger_hash_verified": ledger_hash_verified,
        "chain_continuity_verified": chain_continuity_verified,
        "signature_verified": signature_verified,
        "signature_reason": signature_reason,
        "claimed_runtime_sha256": claimed_runtime,
        "computed_runtime_sha256": computed_runtime,
        "runtime_artifact_relpath": relpath,
        "runtime_artifact_path": str(runtime_path),
        "proof_prev_hash": prev_hash,
        "expected_prev_this_hash": expected_prev_this_hash,
        "proof_this_hash_stored": stored_this_hash,
        "proof_this_hash_computed": computed_this_hash,
        "proof_signature_stored": stored_signature,
        "proof_signature_computed": computed_signature,
        "proof_created_at": float(row["created_at"]),
    }