from __future__ import annotations

from dotenv import load_dotenv
import os

load_dotenv()

import hashlib
import json
import sqlite3
import time
import uuid
import logging
import platform
import traceback
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ConfigDict, model_validator

from backend.obs import setup_logging, write_run_summary as obs_write_run_summary


# ============================================================
# Artifacts directory (MUST be writable; tests expect ./artifacts)
# ============================================================

def _pick_writable_artifacts_dir(preferred: Path) -> Path:
    """
    Prefer env-configured artifacts dir, but if it's not writable (common in CI or
    when env points to /app/artifacts on Windows), fall back to ./artifacts.
    """
    fallback = Path("artifacts")

    for candidate in (preferred, fallback):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            test_path = candidate / ".write_test"
            test_path.write_text("ok", encoding="utf-8")
            test_path.unlink(missing_ok=True)
            return candidate
        except Exception:
            continue

    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


RAW_ARTIFACTS_DIR = Path(os.getenv("QOD_ARTIFACTS_DIR", "artifacts"))
ARTIFACTS_DIR = _pick_writable_artifacts_dir(RAW_ARTIFACTS_DIR).resolve()

BUILD_GIT_SHA = os.getenv("GIT_SHA", os.getenv("QOD_GIT_SHA", "unknown"))
BUILD_IMAGE_TAG = os.getenv("IMAGE_TAG", os.getenv("QOD_IMAGE_TAG", "unknown"))


def utc_now_iso_z() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ============================================================
# Logging + optional redaction
# ============================================================

logger = setup_logging(ARTIFACTS_DIR)
log = logger
log.info("QoD service starting up")

try:
    from backend.logging_redact import configure_redaction  # type: ignore

    configure_redaction()
    log.info("Log redaction filter enabled")
except Exception:
    log.info("Log redaction filter not enabled (backend.logging_redact not found)")


# ============================================================
# App + DB config
# ============================================================

DB_PATH = os.getenv("QOD_DB_PATH") or os.path.join(os.path.dirname(__file__), "qod_mock.sqlite3")
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="QoD Assurance Mock (Local)", version="0.1.0")


# ============================================================
# Request size limit middleware (optional)
# ============================================================

try:
    from backend.middleware.limits import MaxBodySizeMiddleware  # type: ignore

    MAX_BODY_BYTES = int(os.getenv("MAX_BODY_BYTES", str(1_000_000)))
    app.add_middleware(MaxBodySizeMiddleware, max_bytes=MAX_BODY_BYTES)
    log.info("Max body size middleware enabled: %s bytes", MAX_BODY_BYTES)
except Exception:
    log.info("Max body size middleware not enabled (backend.middleware.limits not found)")


# ============================================================
# Request ID + structured request logging
# ============================================================

@app.middleware("http")
async def add_request_id_and_log(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or request.headers.get("x-correlation-id") or str(uuid.uuid4())
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


# ============================================================
# CORS
# ============================================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# DB helpers
# ============================================================

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


# ============================================================
# Readiness checks
# ============================================================

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
        return {
            "status": "not-ready",
            "check": "readiness",
            "elapsed_ms": elapsed_ms,
            "problems": problems,
        }

    return {"status": "ok", "check": "readiness", "elapsed_ms": elapsed_ms}


# ============================================================
# Models
# ============================================================

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


# ============================================================
# Provider simulation
# ============================================================

def choose_qos_profile(intent: Intent) -> str:
    if intent.target_p95_latency_ms <= 50:
        return "QOS_LOW_LATENCY"
    if intent.target_p95_latency_ms <= 150:
        return "QOS_BALANCED"
    return "QOS_BEST_EFFORT"


def map_qos_to_schema_enum(qos_profile: str) -> str:
    # docs/artifact.schema.json requires: turbo | standard | strict
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


# ============================================================
# Hashing helpers (fixed)
# ============================================================

def canonical_json_bytes(obj: Any) -> bytes:
    """
    Deterministic JSON encoding so hashes are stable across platforms/formatting.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(obj: Any) -> str:
    return sha256_hex(canonical_json_bytes(obj))


# ============================================================
# API endpoints
# ============================================================

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
    """
    Finalize a session:
    - reads telemetry
    - writes proof ledger (prev_hash/this_hash)
    - writes:
        artifacts/proof_<sid>_<ts>.json  (debug proof)
        artifacts/<sid>/artifact.json   (RUNTIME CONTRACT artifact)
        artifacts/<sid>/artifact_v1.json (WRAPPER artifact; NOT artifact.json)
        artifacts/run_summaries/run_<ts>_<sid>.json
    """
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

        avg_p50 = sum(p["p50_ms"] for p in parsed) / len(parsed)
        avg_p95 = sum(p["p95_ms"] for p in parsed) / len(parsed)
        avg_jitter = sum(p["jitter_ms"] for p in parsed) / len(parsed)

        intent = json.loads(sess["intent_json"])
        created_at = float(sess["created_at"])
        qos_status = simulated_provider_current_status(created_at)

        # ------------------------------------------------------------
        # Build runtime artifact first, hash it, then commit that hash in proof
        # ------------------------------------------------------------
        session_dir = ARTIFACTS_DIR / sid
        session_dir.mkdir(parents=True, exist_ok=True)

        max_latency_ms = int(min(max(1, int(intent.get("target_p95_latency_ms", 100))), 5000))
        min_throughput_mbps = 0.1
        min_availability_pct = 0.0

        reasons: List[str] = []
        passed = True

        if avg_p95 <= max_latency_ms:
            reasons.append(f"latency {round(avg_p95, 2)}ms <= target max_latency_ms {max_latency_ms}ms")
        else:
            passed = False
            reasons.append(f"latency {round(avg_p95, 2)}ms > target max_latency_ms {max_latency_ms}ms")

        reasons.append(f"jitter observed {round(avg_jitter, 2)}ms (informational)")

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
                "latency_ms": float(round(avg_p95, 2)),
                "throughput_mbps": 0.0,
                "availability_pct": 100.0,
                "jitter_ms": float(round(avg_jitter, 2)),
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
                "avg_p50_ms": round(avg_p50, 2),
                "avg_p95_ms": round(avg_p95, 2),
                "avg_jitter_ms": round(avg_jitter, 2),
            },
            "artifacts": {
                "runtime_artifact_sha256": runtime_artifact_sha256,
                "runtime_artifact_relpath": f"{sid}/artifact.json",
            },
            "created_at": time.time(),
        }

        proof_bytes = canonical_json_bytes(proof)
        this_hash = sha256_hex((prev_hash + "|").encode("utf-8") + proof_bytes)
        summary["ids"]["this_hash"] = this_hash

        with db() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO proof_ledger(session_id, created_at, proof_json, prev_hash, this_hash)
                VALUES (?, ?, ?, ?, ?)
                """,
                (sid, time.time(), json.dumps(proof, sort_keys=True), prev_hash, this_hash),
            )

        response_obj = {"prev_hash": prev_hash, "this_hash": this_hash, "proof": proof}

        # ---- debug proof file
        ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        proof_path = ARTIFACTS_DIR / f"proof_{sid}_{timestamp}.json"
        proof_path.write_text(json.dumps(response_obj, indent=2), encoding="utf-8")
        summary["ids"]["proof_artifact_path"] = str(proof_path)

        # ---- runtime artifact (reserved filename)
        runtime_path = session_dir / "artifact.json"
        runtime_path.write_text(json.dumps(runtime_artifact, indent=2), encoding="utf-8")
        summary["ids"]["runtime_artifact_path"] = str(runtime_path)

        # ---- wrapper artifact (NEVER artifact.json)
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
            # Prefer obs.py writer (it pins location and is already used elsewhere)
            path = obs_write_run_summary(ARTIFACTS_DIR, summary)
            summary["ids"]["run_summary_path"] = str(path)
            log.info("Wrote run summary to %s", path)
        except Exception:
            log.exception("Failed to write run summary")


@app.get("/proof/{session_id}")
def get_proof(session_id: UUID) -> Dict[str, Any]:
    sid = str(session_id)

    with db() as conn:
        row = conn.execute("SELECT * FROM proof_ledger WHERE session_id = ?", (sid,)).fetchone()

    if not row:
        raise HTTPException(
            status_code=404,
            detail="No proof record yet. Call /proof/{id}/finalize first.",
        )

    return {
        "session_id": sid,
        "created_at": float(row["created_at"]),
        "prev_hash": row["prev_hash"],
        "this_hash": row["this_hash"],
        "proof": json.loads(row["proof_json"]),
    }