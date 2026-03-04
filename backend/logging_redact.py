# backend/logging_redact.py
from __future__ import annotations

import logging
import re

REDACT_PATTERNS = [
    # Bearer tokens
    re.compile(r"(Authorization:\s*Bearer\s+)([A-Za-z0-9\-\._~\+\/]+=*)", re.IGNORECASE),
    # Generic api_key= / token= patterns
    re.compile(r"((api[_-]?key|token|secret|password)\s*=\s*)([^&\s]+)", re.IGNORECASE),
]

REPLACEMENT = r"\1[REDACTED]"


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for pat in REDACT_PATTERNS:
            msg = pat.sub(REPLACEMENT, msg)
        # Replace the computed message back into record
        record.msg = msg
        record.args = ()
        return True


def configure_redaction() -> None:
    root = logging.getLogger()
    root.addFilter(RedactFilter())