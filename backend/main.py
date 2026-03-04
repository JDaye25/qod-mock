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
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ConfigDict, model_validator

from backend.obs import setup_logging

# ----------------------------
# Artifacts / build identifiers
# ----------------------------
# In docker-compose you mount: ./artifacts:/app/artifacts
# So we default to /app/artifacts when available.
ARTIFACTS_DIR = Path(os.getenv("QOD_ARTIFACTS_DIR", "/app/artifacts"))
if not ARTIFACTS_DIR.exists():
    # fallback for non-docker local runs
    ARTIFACTS_DIR = Path("artifacts")

RUN_SUMMARIES_DIR = ARTIFACTS_DIR / "run_summaries"
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

BUILD_GIT_SHA = os.getenv("GIT_SHA", "unknown")
BUILD_IMAGE_TAG = os.getenv("IMAGE_TAG", "unknown")


def write_run_summary(summary: dict) -> str:
    RUN_SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    sid = summary.get("session_id", "unknown")
    out = RUN_SUMMARIES_DIR / f"run_{ts}_{sid}.json"
    out.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    return str(out)


# ----------------------------
# Basic logging + optional redaction hook
# ----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(str(ARTIFACTS_DIR / "run.log"), encoding="utf-8"),
    ],
)

log = logging.getLogger("qod")
log.info("QoD service starting up")

# If you created backend/logging_redact.py from the earlier steps, this will enable it.
# If you DIDN'T create it yet, this will simply skip without breaking startup.
try:
    from backend.logging_redact import configure_redaction  # type: ignore

    configure_redaction()
    log.info("Log redaction filter enabled")
except Exception:
    log.info("Log redaction filter not enabled (backend.logging_redact not found)")

client_id = os.getenv("QOD_CLIENT_ID")
client_secret = os.getenv("QOD_CLIENT_SECRET")

DB_PATH = os.path.join(os.path.dirname(__file__), "qod_mock.sqlite3")

app = FastAPI(title="QoD Assurance Mock (Local)", version="0.1.0")

# NOTE: Keep ARTIFACTS_DIR consistent
ARTIFACTS_DIR = Path(os.getenv("QOD_ARTIFACTS_DIR", str(ARTIFACTS_DIR))).resolve()
logger = setup_logging(ARTIFACTS_DIR)

# ----------------------------
# Request size limit middleware (optional)
# ----------------------------
# If you created backend/middleware/limits.py from the earlier steps,
# this will enforce MAX_BODY_BYTES. Otherwise it will be skipped.
try:
    from backend.middleware.limits import MaxBodySizeMiddleware  # type: ignore

    MAX_BODY_BYTES = int(os.getenv("MAX_BODY_BYTES", str(1_000_000)))  # 1 MB default
    app.add_middleware(MaxBodySizeMiddleware, max_bytes=MAX_BODY_BYTES)
    log.info("Max body size middleware enabled: %s bytes", MAX_BODY_BYTES)
except Exception:
    log.info("Max body size middleware not enabled (backend.middleware.limits not found)")

# ----------------------------
# Request ID + structured request logging
# ----------------------------
@app.middleware("http")
async def add_request_id_and_log(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
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
    allow_origins=[
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
# Readiness checks
# ----------------------------
def _sqlite_ready() -> Optional[str]:
    """
    For this repo, your "real dependency" is the SQLite file.
    Readiness here means: we can connect AND the expected tables exist.
    """
    try:
        with db() as conn:
            # basic query to ensure DB opens
            conn.execute("SELECT 1").fetchone()

            # ensure schema exists (tables)
            needed = {"sessions", "telemetry", "proof_ledger"}
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table';"
            ).fetchall()
            have = {r["name"] for r in rows}
            missing = sorted(list(needed - have))
            if missing:
                return f"DB schema not ready, missing tables: {missing}"
        return None
    except Exception as e:
        return f"SQLite readiness failed: {type(e).__name__}: {e}"


@app.get("/health")
def health() -> Dict[str, str]:
    # Liveness only: process is alive
    return {"status": "ok", "check": "liveness"}


@app.get("/ready")
def ready(response: Response) -> Dict[str, Any]:
    # Readiness: dependencies OK (for now, SQLite + schema)
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


# ----------------------------
# Models (fail-fast validation)
# ----------------------------
class Intent(BaseModel):
    """
    A simplified "SLO-like" intent. In real QoD, you would map this to offered QoS profiles.

    Fail-fast rules:
    - text must be present and non-empty (human-friendly)
    - numeric bounds prevent ridiculous values
    """

    model_config = ConfigDict(extra="forbid")  # reject unknown fields fast

    text: str = Field(..., min_length=1, max_length=5000)

    target_p95_latency_ms: int = Field(..., ge=1, le=60_000)
    target_jitter_ms: int = Field(..., ge=0, le=60_000)
    duration_s: int = Field(..., ge=1, le=24 * 3600)

    flow_label: str = Field("demo-flow", min_length=1, max_length=200)


class TelemetrySample(BaseModel):
    """
    Minimal telemetry payload from a measurement agent.

    Fail-fast rules:
    - session_id must be a UUID
    - p95_ms >= p50_ms
    - ranges keep garbage out
    """

    model_config = ConfigDict(extra="forbid")  # reject unknown fields fast

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
# "Operator QoD Provider" simulation
# ----------------------------
def choose_qos_profile(intent: Intent) -> str:
    if intent.target_p95_latency_ms <= 50:
        return "QOS_LOW_LATENCY"
    if intent.target_p95_latency_ms <= 150:
        return "QOS_BALANCED"
    return "QOS_BEST_EFFORT"


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
    log.info("POST /intent called")

    qos_profile = choose_qos_profile(intent)
    provider_resp = simulated_provider_create_session(qos_profile, intent.duration_s)
    created_at = time.time()
    session_id = provider_resp["sessionId"]

    log.info("Created session %s", session_id)

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
        r = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?",
            (sid,),
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

    # Fail fast: session must exist
    with db() as conn:
        r = conn.execute(
            "SELECT 1 FROM sessions WHERE session_id = ?",
            (sid,),
        ).fetchone()

        if not r:
            raise HTTPException(
                status_code=404,
                detail="Unknown session_id. Create a session first via POST /intent.",
            )

        # Store telemetry
        conn.execute(
            """
            INSERT INTO telemetry(session_id, created_at, sample_json)
            VALUES (?, ?, ?)
            """,
            (sid, time.time(), sample.model_dump_json()),
        )

    return {"status": "stored"}


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@app.post("/proof/{session_id}/finalize")
def finalize_proof(session_id: UUID, request: Request) -> Dict[str, Any]:
    sid = str(session_id)

    # A simple correlator: use a header if provided, otherwise create one.
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
        "build": {
            "git_sha": BUILD_GIT_SHA,
            "image_tag": BUILD_IMAGE_TAG,
        },
        "timestamps": {
            "start_utc": datetime.utcnow().isoformat() + "Z",
            "end_utc": None,
            "duration_ms": None,
        },
        "result": {
            "success": False,
            "reason": None,
        },
        "ids": {
            "prev_hash": None,
            "this_hash": None,
            "proof_artifact_path": None,
            "run_summary_path": None,
        },
        "env": {
            "hostname": platform.node(),
        },
    }

    try:
        with db() as conn:
            sess = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                (sid,),
            ).fetchone()

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

        # Fail fast: must have telemetry
        if not samples:
            raise HTTPException(
                status_code=400,
                detail="No telemetry samples found for this session. POST /telemetry first.",
            )

        parsed = [json.loads(s["sample_json"]) for s in samples]

        # Extra sanity: avoid mysterious math errors
        if not parsed:
            raise HTTPException(status_code=400, detail="Telemetry payloads could not be parsed.")

        avg_p50 = sum(p["p50_ms"] for p in parsed) / len(parsed)
        avg_p95 = sum(p["p95_ms"] for p in parsed) / len(parsed)
        avg_jitter = sum(p["jitter_ms"] for p in parsed) / len(parsed)

        intent = json.loads(sess["intent_json"])
        created_at = float(sess["created_at"])
        qos_status = simulated_provider_current_status(created_at)

        proof = {
            "session_id": sid,
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

        # Save proof response artifact for audit/debug (to the mounted artifacts dir)
        ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        proof_path = ARTIFACTS_DIR / f"proof_{sid}_{timestamp}.json"
        proof_path.write_text(json.dumps(response_obj, indent=2), encoding="utf-8")

        summary["ids"]["proof_artifact_path"] = str(proof_path)
        summary["result"]["success"] = True
        summary["result"]["reason"] = "ok"

        log.info("Saved proof artifact to %s", proof_path)

        return response_obj

    except HTTPException as e:
        summary["result"]["success"] = False
        summary["result"]["reason"] = f"http_{e.status_code}: {e.detail}"
        raise

    except Exception as e:
        summary["result"]["success"] = False
        summary["result"]["reason"] = f"exception: {type(e).__name__}"
        summary["exception"] = traceback.format_exc()
        raise

    finally:
        end_ts = time.time()
        summary["timestamps"]["end_utc"] = datetime.utcnow().isoformat() + "Z"
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
        row = conn.execute(
            "SELECT * FROM proof_ledger WHERE session_id = ?",
            (sid,),
        ).fetchone()

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