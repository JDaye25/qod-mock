import json, hashlib
from pathlib import Path

sid = "e6ff3138-228d-4a14-9014-2eb0d16fd076"
runtime_path = Path("artifacts") / sid / "artifact.json"
runtime = json.loads(runtime_path.read_text(encoding="utf-8"))

canonical = json.dumps(runtime, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
digest = hashlib.sha256(canonical).hexdigest()

print("runtime_artifact_sha256 computed:", digest)

proof_files = sorted(Path("artifacts").glob(f"proof_{sid}_*.json"))
if proof_files:
    proof_obj = json.loads(proof_files[-1].read_text(encoding="utf-8"))
    claimed = proof_obj["proof"]["artifacts"]["runtime_artifact_sha256"]
    print("runtime_artifact_sha256 claimed :", claimed)
    print("MATCH:", digest == claimed)
else:
    print("No proof_<sid>_*.json found to compare; runtime hash computed only.")
