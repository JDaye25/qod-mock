import os, sqlite3, json, hashlib

sid = os.environ.get("SID")
db_path = r"backend\qod_mock.sqlite3"

if not sid:
    raise SystemExit("SID env var missing. In PowerShell: $env:SID = $sid")

def canonical_bytes(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

conn = sqlite3.connect(db_path)
cur = conn.cursor()

cur.execute("SELECT proof_json, prev_hash, this_hash, signature FROM proof_ledger WHERE session_id = ?", (sid,))
row = cur.fetchone()
if not row:
    raise SystemExit(f"No proof_ledger row found for session_id={sid}")

proof_json, prev_hash, old_this_hash, old_sig = row
proof = json.loads(proof_json)

# Tamper proof content
proof["provider_observed"]["provider_note"] = "evil-simulated-provider"
new_proof_json = json.dumps(proof, sort_keys=True)

# Recompute new this_hash for the modified proof (attacker can do this)
msg = (str(prev_hash) + "|").encode("utf-8") + canonical_bytes(proof)
new_this_hash = hashlib.sha256(msg).hexdigest()

# Update proof_json + this_hash ONLY. Leave signature unchanged.
cur.execute(
    "UPDATE proof_ledger SET proof_json = ?, this_hash = ? WHERE session_id = ?",
    (new_proof_json, new_this_hash, sid),
)

conn.commit()
print("Tampered session:", sid)
print("Rows updated:", cur.rowcount)
conn.close()
