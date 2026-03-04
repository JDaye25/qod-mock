# backend/routers/health.py
from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Response, status

router = APIRouter()


@router.get("/health")
def health() -> Dict[str, Any]:
    # Process liveness only: "the app is running"
    return {"status": "ok", "check": "liveness"}


def _try_db_ready() -> Optional[str]:
    """
    Optional DB readiness check.
    Returns None if DB looks OK, otherwise returns a human-readable error string.

    This is intentionally "soft": if you haven't added a DB yet, it won't break your app.
    It only runs if DATABASE_URL is set.
    """
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        return None  # No DB configured => skip DB check

    # Try SQLAlchemy if available
    try:
        from sqlalchemy import text  # type: ignore
        from sqlalchemy import create_engine  # type: ignore
    except Exception:
        return "DATABASE_URL is set but SQLAlchemy is not installed."

    try:
        engine = create_engine(database_url, pool_pre_ping=True, future=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return None
    except Exception as e:
        return f"DB check failed: {type(e).__name__}: {e}"


def _try_migrations_applied() -> Optional[str]:
    """
    Optional migration check for Alembic.
    This assumes an Alembic table named 'alembic_version' exists if migrations have run.
    If you don't use Alembic, leave it as a future upgrade (it won't fail by default).
    """
    if os.getenv("REQUIRE_MIGRATIONS", "0") != "1":
        return None  # not enforced unless you turn it on

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        return "REQUIRE_MIGRATIONS=1 but DATABASE_URL is not set."

    try:
        from sqlalchemy import text  # type: ignore
        from sqlalchemy import create_engine  # type: ignore
    except Exception:
        return "REQUIRE_MIGRATIONS=1 but SQLAlchemy is not installed."

    try:
        engine = create_engine(database_url, pool_pre_ping=True, future=True)
        with engine.connect() as conn:
            # This will fail if the table doesn't exist (meaning migrations likely not applied)
            conn.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))
        return None
    except Exception as e:
        return f"Migration check failed: {type(e).__name__}: {e}"


@router.get("/ready")
def ready(response: Response) -> Dict[str, Any]:
    # Readiness: "dependencies are good enough to serve real traffic"
    started = time.time()

    problems = []

    db_problem = _try_db_ready()
    if db_problem:
        problems.append(db_problem)

    mig_problem = _try_migrations_applied()
    if mig_problem:
        problems.append(mig_problem)

    elapsed_ms = int((time.time() - started) * 1000)

    if problems:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "not-ready",
            "check": "readiness",
            "elapsed_ms": elapsed_ms,
            "problems": problems,
        }

    return {
        "status": "ok",
        "check": "readiness",
        "elapsed_ms": elapsed_ms,
    }