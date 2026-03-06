from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any


def canonical_json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def candidate_db_paths() -> list[Path]:
    candidates: list[Path] = []

    env_db = (os.getenv("QOD_DB_PATH") or "").strip()
    if env_db:
        candidates.append(Path(env_db))

    # Common locations for local + container runs
    candidates.extend(
        [
            Path("/app/backend/qod_mock.sqlite3"),
            Path("/app/qod_mock.sqlite3"),
            Path("backend/qod_mock.sqlite3"),
            Path("qod_mock.sqlite3"),
        ]
    )

    # De-duplicate while preserving order
    seen = set()
    unique: list[Path] = []
    for p in candidates:
        rp = str(p)
        if rp not in seen:
            seen.add(rp)
            unique.append(p)
    return unique


def find_real_db() -> Path:
    for path in candidate_db_paths():
        if not path.exists():
            continue
        try:
            conn = sqlite3.connect(path)
            cur = conn.cursor()
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='proof_ledger'"
            )
            row = cur.fetchone()
            conn.close()
            if row:
                return path
        except Exception:
            continue

    searched = "\n".join(str(p) for p in candidate_db_paths())
    raise RuntimeError(
        "Could not find a SQLite database containing proof_ledger.\n"
        f"Searched:\n{searched}"
    )


def main() -> None:
    sid = (os.getenv("SID") or "").strip()
    if not sid:
        raise SystemExit("SID env var missing.")

    db_path = find_real_db()
    print(f"Using DB: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute(
        "SELECT proof_json, prev_hash, this_hash, signature FROM proof_ledger WHERE session_id = ?",
        (sid,),
    )
    row = cur.fetchone()
    if not row:
        conn.close()
        raise SystemExit(f"No proof_ledger row found for session_id={sid}")

    proof = json.loads(row["proof_json"])
    prev_hash = row["prev_hash"]

    # Tamper with the proof JSON but leave signature unchanged
    proof["tampered"] = True
    proof["tampered_reason"] = "signature_mismatch_test"

    new_proof_json = json.dumps(proof, sort_keys=True)
    new_this_hash = sha256_hex((str(prev_hash) + "|").encode("utf-8") + canonical_json_bytes(proof))

    cur.execute(
        """
        UPDATE proof_ledger
        SET proof_json = ?, this_hash = ?
        WHERE session_id = ?
        """,
        (new_proof_json, new_this_hash, sid),
    )
    rows_updated = cur.rowcount
    conn.commit()
    conn.close()

    print(f"Tampered session: {sid}")
    print(f"Rows updated: {rows_updated}")
    print("Signature intentionally left unchanged.")


if __name__ == "__main__":
    main()