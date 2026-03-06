from __future__ import annotations

from dotenv import load_dotenv
import os

load_dotenv()

import base64
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
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, ConfigDict, model_validator

from backend.obs import setup_logging

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives import serialization
from cryptography.exceptions import InvalidSignature


# ----------------------------
# API auth (Bearer token)
# ----------------------------
_auth_scheme = HTTPBearer(auto_error=False)


def _auth_required() -> bool:
    """
    Auth is required unless explicitly disabled with:
      QOD_AUTH_REQUIRED=0
    """
    raw = (os.getenv("QOD_AUTH_REQUIRED") or "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def require_api_key(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_auth_scheme),
) -> str:
    """
    Behavior:
      - If QOD_AUTH_REQUIRED=0 -> auth bypassed
      - If QOD_AUTH_REQUIRED=1 and QOD_API_TOKEN missing -> 500
      - If auth required and no/wrong header -> 401
      - If auth required and token correct -> success
    """
    if not _auth_required():
        return "auth-disabled"

    expected = (os.getenv("QOD_API_TOKEN") or "").strip()

    if not expected:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server misconfigured: QOD_API_TOKEN is not set",
        )

    if creds is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if (creds.scheme or "").lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization scheme must be Bearer",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if creds.credentials != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return creds.credentials


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
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(obj: Any) -> str:
    return sha256_hex(canonical_json_bytes(obj))


# ----------------------------
# Signing helpers (Ed25519) + kid + JWKS
# ----------------------------
def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(s: str) -> bytes:
    s = (s or "").strip()
    pad = "=" * ((4 - (len(s) % 4)) % 4)
    return base64.urlsafe_b64decode((s + pad).encode("ascii"))


def _ed25519_public_bytes_raw(pub: Ed25519PublicKey) -> bytes:
    raw_fn = getattr(pub, "public_bytes_raw", None)
    if callable(raw_fn):
        return raw_fn()
    return pub.public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)


def _ed25519_private_bytes_raw(priv: Ed25519PrivateKey) -> bytes:
    raw_fn = getattr(priv, "private_bytes_raw", None)
    if callable(raw_fn):
        return raw_fn()
    return priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _load_ed25519_private_key_from_env() -> Optional[Ed25519PrivateKey]:
    b64u = (os.getenv("QOD_SIGNING_PRIVATE_KEY_B64URL") or "").strip()
    if not b64u:
        return None
    try:
        raw = _b64url_decode(b64u)
        if len(raw) != 32:
            return None
        return Ed25519PrivateKey.from_private_bytes(raw)
    except Exception:
        return None


def _load_ed25519_public_key_from_env_single() -> Optional[Ed25519PublicKey]:
    b64u = (os.getenv("QOD_SIGNING_PUBLIC_KEY_B64URL") or "").strip()
    if b64u:
        try:
            raw = _b64url_decode(b64u)
            if len(raw) != 32:
                return None
            return Ed25519PublicKey.from_public_bytes(raw)
        except Exception:
            return None

    priv = _load_ed25519_private_key_from_env()
    if priv is None:
        return None
    return priv.public_key()


def _active_kid() -> str:
    return (os.getenv("QOD_ACTIVE_SIGNING_KID") or "default").strip() or "default"


def _parse_signing_keys(env_value: Optional[str] = None) -> Dict[str, str]:
    if env_value is None:
        env_value = (
            os.environ.get("QOD_SIGNING_KEYS", "")
            or os.environ.get("QOD_PUBLIC_KEYS", "")
            or os.environ.get("QOD_SIGNING_PUBLIC_KEYS", "")
            or os.environ.get("QOD_PUBLIC_KEY_RING", "")
            or os.environ.get("QOD_KEY_RING", "")
            or os.environ.get("QOD_KEYS", "")
        )

    spec = (env_value or "").strip()
    if not spec:
        return {}

    spec = spec.replace("\r\n", "\n").replace("\r", "\n")
    spec = spec.replace("\n", ";").replace(",", ";")

    out: Dict[str, str] = {}
    for entry in [p.strip() for p in spec.split(";") if p.strip()]:
        if ":" in entry:
            kid, val = entry.split(":", 1)
        elif "=" in entry:
            kid, val = entry.split("=", 1)
        else:
            continue

        kid = kid.strip()
        val = val.strip()
        if kid and val:
            out[kid] = val

    return out


def _parse_public_keys_to_objects(env_value: str) -> Dict[str, Ed25519PublicKey]:
    raw_map = _parse_signing_keys(env_value)
    out: Dict[str, Ed25519PublicKey] = {}

    for kid, b64u in raw_map.items():
        try:
            raw = _b64url_decode(b64u)
            if len(raw) != 32:
                continue
            out[kid] = Ed25519PublicKey.from_public_bytes(raw)
        except Exception:
            continue

    return out


def _active_kid_and_key() -> Tuple[str, Ed25519PrivateKey]:
    kid = _active_kid()
    priv = _load_ed25519_private_key_from_env()
    if priv is None:
        raise ValueError("Ed25519 signing key missing")
    return kid, priv


def hmac_signature_hex(message: str, *, kid: str = "default") -> str:
    env_key = f"QOD_HMAC_SECRET_{(kid or 'default').upper()}"
    base_secret = os.getenv(env_key) or os.getenv("QOD_HMAC_SECRET") or ""
    key_material = f"{base_secret}|{kid or 'default'}".encode("utf-8")

    return hmac.new(
        key_material,
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _public_keys_from_env() -> Dict[str, Ed25519PublicKey]:
    out: Dict[str, Ed25519PublicKey] = {}

    spec = (os.getenv("QOD_PUBLIC_KEYS") or "").strip()
    if spec:
        out = _parse_public_keys_to_objects(spec)

    if not out:
        pub = _load_ed25519_public_key_from_env_single()
        if pub is not None:
            out[_active_kid()] = pub

    return out


def _get_public_key_for_kid(kid: str) -> Optional[Ed25519PublicKey]:
    keys = _public_keys_from_env()
    if not keys:
        return None
    if kid in keys:
        return keys[kid]
    if len(keys) == 1:
        return next(iter(keys.values()))
    return None


def ed25519_sign(message: bytes) -> Optional[str]:
    priv = _load_ed25519_private_key_from_env()
    if priv is None:
        return None
    sig = priv.sign(message)
    return _b64url_encode(sig)


def ed25519_verify(signature_b64url: str, message: bytes, pub: Ed25519PublicKey) -> bool:
    try:
        sig = _b64url_decode(signature_b64url)
        pub.verify(sig, message)
        return True
    except (InvalidSignature, ValueError, Exception):
        return False


def _jwks_from_public_keys(keys: Dict[str, Ed25519PublicKey]) -> Dict[str, Any]:
    jwk_list: List[Dict[str, Any]] = []
    for kid, pub in sorted(keys.items(), key=lambda kv: kv[0]):
        raw = _ed25519_public_bytes_raw(pub)
        jwk_list.append(
            {
                "kty": "OKP",
                "crv": "Ed25519",
                "kid": kid,
                "use": "sig",
                "alg": "EdDSA",
                "x": _b64url_encode(raw),
            }
        )
    return {"keys": jwk_list}


# ----------------------------
# App + logging
# ----------------------------
app = FastAPI(title="QoD Assurance Mock (Local)", version="0.1.0")

logger = setup_logging(ARTIFACTS_DIR)
log = logging.getLogger("qod")
log.info("QoD service starting up (file=%s pid=%s)", __file__, os.getpid())

try:
    from backend.logging_redact import configure_redaction  # type: ignore

    configure_redaction()
    log.info("Log redaction filter enabled")
except Exception:
    log.info("Log redaction filter not enabled (backend.logging_redact not found)")


# ----------------------------
# Debug endpoints
# ----------------------------
@app.get("/_debug/env")
def _debug_env() -> Dict[str, Any]:
    token = (os.getenv("QOD_API_TOKEN") or "").strip()
    return {
        "auth_required": _auth_required(),
        "token_present": bool(token),
        "token_len": len(token),
        "pid": os.getpid(),
        "file": __file__,
    }


@app.get("/_debug/protected", dependencies=[Depends(require_api_key)])
def _debug_protected() -> Dict[str, Any]:
    return {"ok": True, "pid": os.getpid(), "file": __file__}


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


def _ensure_proof_ledger_columns(conn: sqlite3.Connection) -> None:
    cols = conn.execute("PRAGMA table_info(proof_ledger);").fetchall()
    colnames = {c["name"] for c in cols}

    if "signature" not in colnames:
        conn.execute("ALTER TABLE proof_ledger ADD COLUMN signature TEXT NOT NULL DEFAULT '';")
        log.info("DB migration applied: added proof_ledger.signature column")

    if "kid" not in colnames:
        conn.execute("ALTER TABLE proof_ledger ADD COLUMN kid TEXT NOT NULL DEFAULT '';")
        log.info("DB migration applied: added proof_ledger.kid column")


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
              signature TEXT NOT NULL,
              kid TEXT NOT NULL
            )
            """
        )
        _ensure_proof_ledger_columns(conn)


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
            if "signature" not in colnames or "kid" not in colnames:
                return "DB schema not ready: proof_ledger.signature/kid column missing (restart server to auto-migrate)"
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


@app.get("/.well-known/jwks.json")
def jwks() -> Dict[str, Any]:
    keys = _public_keys_from_env()
    return _jwks_from_public_keys(keys)


@app.get("/public-keys")
def public_keys() -> Dict[str, Any]:
    keys = _public_keys_from_env()
    out = []
    for kid, pub in sorted(keys.items(), key=lambda kv: kv[0]):
        out.append(
            {
                "kid": kid,
                "alg": "ed25519",
                "public_key_b64url": _b64url_encode(_ed25519_public_bytes_raw(pub)),
            }
        )
    return {"keys": out}


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
def create_intent_and_session(intent: Intent, _: str = Depends(require_api_key)) -> Dict[str, Any]:
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


@app.post("/telemetry")
def post_telemetry(sample: TelemetrySample, _: str = Depends(require_api_key)) -> Dict[str, str]:
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
def finalize_proof(session_id: UUID, request: Request, _: str = Depends(require_api_key)) -> Dict[str, Any]:
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
            "kid": None,
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

            prev = conn.execute("SELECT this_hash FROM proof_ledger ORDER BY created_at DESC LIMIT 1").fetchone()

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
                    "min_throughput_mbps": 0.1,
                    "min_availability_pct": 0.0,
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

        kid = _active_kid()
        signature = ed25519_sign(this_hash.encode("utf-8"))
        if signature is None:
            raise HTTPException(
                status_code=500,
                detail=(
                    "Ed25519 signing key missing. Set QOD_SIGNING_PRIVATE_KEY_B64URL "
                    "(and optionally QOD_SIGNING_PUBLIC_KEY_B64URL) and restart."
                ),
            )

        summary["ids"]["signature"] = signature
        summary["ids"]["kid"] = kid

        with db() as conn:
            _ensure_proof_ledger_columns(conn)
            conn.execute(
                """
                INSERT OR REPLACE INTO proof_ledger(session_id, created_at, proof_json, prev_hash, this_hash, signature, kid)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (sid, time.time(), json.dumps(proof, sort_keys=True), prev_hash, this_hash, signature, kid),
            )

        response_obj = {
            "prev_hash": prev_hash,
            "this_hash": this_hash,
            "signature": signature,
            "kid": kid,
            "proof": proof,
        }

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        proof_path = ARTIFACTS_DIR / f"proof_{sid}_{timestamp}.json"
        proof_path.write_text(json.dumps(response_obj, indent=2), encoding="utf-8")
        summary["ids"]["proof_artifact_path"] = str(proof_path)

        runtime_path = session_dir / "artifact.json"
        runtime_path.write_text(json.dumps(runtime_artifact, indent=2), encoding="utf-8")
        summary["ids"]["runtime_artifact_path"] = str(runtime_path)

        wrapper_path = session_dir / "artifact_v1.json"
        wrapper_path.write_text(
            json.dumps(
                {
                    "schema_version": "v1",
                    "task": "QoD proof finalize (wrapper)",
                    "summary": "Generated proof record and runtime contract artifact.",
                    "outputs": {"proof_record": response_obj},
                    "citations": [],
                    "quality": {"has_telemetry": True, "validated_in_test": True},
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        summary["ids"]["wrapper_artifact_path"] = str(wrapper_path)

        summary["result"]["success"] = True
        summary["result"]["reason"] = "ok"
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
        except Exception:
            log.exception("Failed to write run summary")


@app.get("/proof/{session_id}")
def get_proof(session_id: UUID, _: str = Depends(require_api_key)) -> Dict[str, Any]:
    sid = str(session_id)

    with db() as conn:
        row = conn.execute(
            "SELECT proof_json, prev_hash, this_hash, signature, kid, created_at FROM proof_ledger WHERE session_id = ?",
            (sid,),
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="No proof record found for this session_id. Finalize first.")

    return {
        "session_id": sid,
        "created_at": float(row["created_at"]),
        "prev_hash": row["prev_hash"],
        "this_hash": row["this_hash"],
        "signature": row["signature"] or "",
        "kid": row["kid"] or "",
        "proof": json.loads(row["proof_json"]),
    }


@app.get("/proof/{session_id}/bundle")
def get_proof_bundle(session_id: UUID, _: str = Depends(require_api_key)) -> Dict[str, Any]:
    sid = str(session_id)

    with db() as conn:
        row = conn.execute(
            "SELECT proof_json, prev_hash, this_hash, signature, kid, created_at FROM proof_ledger WHERE session_id = ?",
            (sid,),
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="No proof record found for this session_id. Finalize first.")

    proof = json.loads(row["proof_json"])
    relpath = ((proof.get("artifacts") or {}).get("runtime_artifact_relpath")) or f"{sid}/artifact.json"
    runtime_path = ARTIFACTS_DIR / relpath

    runtime_obj: Optional[dict] = None
    if runtime_path.exists():
        try:
            runtime_obj = json.loads(runtime_path.read_text(encoding="utf-8-sig"))
        except Exception:
            runtime_obj = None

    return {
        "session_id": sid,
        "ledger": {
            "created_at": float(row["created_at"]),
            "prev_hash": row["prev_hash"],
            "this_hash": row["this_hash"],
            "signature": row["signature"] or "",
            "kid": row["kid"] or "",
        },
        "proof": proof,
        "runtime_artifact": runtime_obj,
        "runtime_artifact_relpath": relpath,
        "jwks_url": "/.well-known/jwks.json",
    }


@app.get("/proof/{session_id}/verify")
def verify_proof(session_id: UUID, _: str = Depends(require_api_key)) -> Dict[str, Any]:
    sid = str(session_id)

    with db() as conn:
        row = conn.execute(
            "SELECT proof_json, prev_hash, this_hash, signature, kid, created_at FROM proof_ledger WHERE session_id = ?",
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
    stored_sig = row["signature"] or ""
    stored_kid = row["kid"] or ""

    computed_this_hash = sha256_hex((str(prev_hash) + "|").encode("utf-8") + canonical_json_bytes(proof))
    ledger_hash_verified = (computed_this_hash == stored_this_hash)

    if prev_hash == "GENESIS":
        chain_continuity_verified = True
        expected_prev_this_hash = None
    else:
        expected_prev_this_hash = prev_row["this_hash"] if prev_row else None
        chain_continuity_verified = (expected_prev_this_hash is not None and prev_hash == expected_prev_this_hash)

    pub = _get_public_key_for_kid(stored_kid)
    if pub is None:
        signature_verified = False
        signature_reason = "public_key_missing"
    else:
        signature_verified = bool(stored_sig) and ed25519_verify(stored_sig, stored_this_hash.encode("utf-8"), pub)
        signature_reason = "ok" if signature_verified else "signature_mismatch"

    claimed_runtime = ((proof.get("artifacts") or {}).get("runtime_artifact_sha256")) or None
    runtime_path = ARTIFACTS_DIR / sid / "artifact.json"
    if claimed_runtime and runtime_path.exists():
        try:
            runtime_obj = json.loads(runtime_path.read_text(encoding="utf-8-sig"))
            computed_runtime = sha256_json(runtime_obj)
            runtime_artifact_verified = (computed_runtime == claimed_runtime)
        except Exception:
            runtime_artifact_verified = False
            computed_runtime = None
    else:
        runtime_artifact_verified = False
        computed_runtime = None

    verified = bool(runtime_artifact_verified and ledger_hash_verified and chain_continuity_verified and signature_verified)

    if verified:
        reason = "ok"
    elif not signature_verified:
        reason = signature_reason
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
        "kid": stored_kid,
        "proof_prev_hash": prev_hash,
        "expected_prev_this_hash": expected_prev_this_hash,
        "proof_this_hash_stored": stored_this_hash,
        "proof_this_hash_computed": computed_this_hash,
        "proof_signature_stored": stored_sig,
        "proof_created_at": float(row["created_at"]),
        "claimed_runtime_sha256": claimed_runtime,
        "computed_runtime_sha256": computed_runtime,
        "runtime_artifact_path": str(runtime_path),
    }