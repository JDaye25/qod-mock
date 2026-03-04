
import json
import hashlib
import sys
from pathlib import Path


def canonical_json_bytes(obj):
    """
    Must match the backend canonical JSON rules.
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def find_latest_proof(artifacts_dir: Path, sid: str):
    files = sorted(artifacts_dir.glob(f"proof_{sid}_*.json"))
    if not files:
        return None
    return files[-1]


def verify_proof(sid: str):

    artifacts_dir = Path("artifacts")

    proof_file = find_latest_proof(artifacts_dir, sid)

    if not proof_file:
        print("❌ No proof file found.")
        return

    runtime_artifact = artifacts_dir / sid / "artifact.json"

    if not runtime_artifact.exists():
        print("❌ Runtime artifact missing:", runtime_artifact)
        return

    proof_obj = json.loads(proof_file.read_text(encoding="utf-8"))

    proof = proof_obj["proof"]

    claimed_sha = proof["artifacts"]["runtime_artifact_sha256"]

    runtime = json.loads(runtime_artifact.read_text(encoding="utf-8"))

    computed_sha = sha256_hex(canonical_json_bytes(runtime))

    print("----- VERIFY PROOF -----")
    print("session_id:", sid)
    print("proof_file:", proof_file)
    print("runtime_artifact:", runtime_artifact)
    print()

    print("runtime_artifact_sha256 (computed):", computed_sha)
    print("runtime_artifact_sha256 (claimed) :", claimed_sha)

    if computed_sha == claimed_sha:
        print("\n✅ PASS — runtime artifact hash matches proof")
    else:
        print("\n❌ FAIL — runtime artifact hash mismatch")

    # optional chain verification
    prev_hash = proof_obj.get("prev_hash")
    this_hash = proof_obj.get("this_hash")

    if prev_hash and this_hash:

        proof_bytes = canonical_json_bytes(proof)

        computed_chain = sha256_hex((prev_hash + "|").encode("utf-8") + proof_bytes)

        print("\nchain hash (computed):", computed_chain)
        print("chain hash (stored)  :", this_hash)

        if computed_chain == this_hash:
            print("✅ PASS — proof chain valid")
        else:
            print("❌ FAIL — proof chain mismatch")


if __name__ == "__main__":

    if len(sys.argv) < 2:
        print("Usage: py verify_proof.py <session_id>")
        sys.exit(1)

    verify_proof(sys.argv[1])