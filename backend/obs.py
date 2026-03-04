import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base: Dict[str, Any] = {
            "ts": utc_now_iso(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        # include selected structured fields if present
        for key in (
            "event",
            "request_id",
            "method",
            "path",
            "status_code",
            "duration_ms",
            "session_id",
            "error",
        ):
            if hasattr(record, key):
                base[key] = getattr(record, key)

        if record.exc_info:
            base["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(base, ensure_ascii=False)


def setup_logging(artifacts_dir: Path) -> logging.Logger:
    """
    Configure root logging for the app.

    IMPORTANT for tests/CI:
    - Tests often run uvicorn with stdout=PIPE but don't read continuously.
      If logs are too chatty, the pipe fills and uvicorn blocks, causing HTTP timeouts.
    - Therefore: disable uvicorn access logs and keep output compact.
    """
    # Always try to create artifacts dir (but don't crash if not permitted)
    try:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    # Build log file path safely (even if artifacts_dir isn't writable)
    log_file = artifacts_dir / "run.log"

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Avoid duplicate handlers (reload/tests)
    root.handlers.clear()

    formatter = JsonLineFormatter()

    # Always log to stdout (JSON lines)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(formatter)
    root.addHandler(sh)

    # Optional file logging (disable gracefully if not permitted)
    try:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(formatter)
        root.addHandler(fh)
    except Exception as e:
        # Keep this as a single WARNING line; don't spam.
        root.warning("File logging disabled (cannot write %s): %r", str(log_file), e)

    # ---- Critical: prevent access-log spam (fills stdout PIPE in tests/CI) ----
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    access.disabled = True

    # Uvicorn error logs are useful; keep them but avoid duplicate formatting
    uv_err = logging.getLogger("uvicorn.error")
    uv_err.propagate = True

    return logging.getLogger("qod")


def sha256_file(path: Path) -> Optional[str]:
    try:
        import hashlib

        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def write_run_summary(artifacts_dir: Path, summary: Dict[str, Any]) -> Path:
    """
    Write a JSON run summary under <artifacts_dir>/run_summaries.

    This function MUST NOT crash the app if the filesystem is read-only.
    If it cannot write, it falls back to ./artifacts/run_summaries.
    """
    # Preferred location
    summaries_dir = artifacts_dir / "run_summaries"

    try:
        summaries_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        # Fallback for read-only paths in CI/containers
        summaries_dir = Path("artifacts") / "run_summaries"
        summaries_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    session_part = summary.get("session_id", "nosession")
    out = summaries_dir / f"run_{ts}_{session_part}.json"

    out.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def build_version_info() -> Dict[str, str]:
    # Support both naming conventions (some environments set one or the other)
    return {
        "git_sha": os.getenv("QOD_GIT_SHA") or os.getenv("GIT_SHA", "unknown"),
        "image_tag": os.getenv("QOD_IMAGE_TAG") or os.getenv("IMAGE_TAG", "unknown"),
    }