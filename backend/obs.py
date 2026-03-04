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
    Configure logging for the app. Must NEVER crash if artifacts_dir is not writable
    (e.g., GitHub Actions runner permissions / container user mismatch).

    Behavior:
      - Always logs JSON lines to stdout
      - Attempts to also log to artifacts_dir/run.log
      - If file logging fails, falls back to stdout-only (and emits a warning)
    """
    artifacts_dir = Path(artifacts_dir)

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Avoid duplicate handlers (especially during reload)
    root.handlers.clear()

    formatter = JsonLineFormatter()

    # Always have stdout logging
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(formatter)
    root.addHandler(sh)

    # Best-effort file logging (never fatal)
    log_file = artifacts_dir / "run.log"
    try:
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        # write test so we fail here instead of raising inside FileHandler
        with open(log_file, "a", encoding="utf-8"):
            pass

        fh = logging.FileHandler(str(log_file), encoding="utf-8")
        fh.setFormatter(formatter)
        root.addHandler(fh)
    except Exception as e:
        root.warning("File logging disabled (cannot write %s): %r", str(log_file), e)

    # Keep uvicorn access logs from making weird double formats
    logging.getLogger("uvicorn.access").handlers.clear()
    logging.getLogger("uvicorn.access").propagate = True

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
    summaries_dir = artifacts_dir / "run_summaries"
    summaries_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    session_part = summary.get("session_id", "nosession")
    out = summaries_dir / f"run_{ts}_{session_part}.json"

    out.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def build_version_info() -> Dict[str, str]:
    return {
        "git_sha": os.getenv("QOD_GIT_SHA", "unknown"),
        "image_tag": os.getenv("QOD_IMAGE_TAG", "unknown"),
    }