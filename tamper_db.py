import os, sqlite3, json

sid = os.environ.get("SID")
db_path = r"backend\qod_mock.sqlite3"

if not sid:
    raise SystemExit("SID env var missing. Set in PowerShell: $env:SID = $sid")

conn = sqlite3.connect(db_path)
cur = conn.cursor()

cur.execute("SELECT proof_json FROM proof_ledger WHERE session_id = ?", (sid,))
row = cur.fetchone()
if not row:
    raise SystemExit(f"No proof_ledger row found for session_id={sid}")

proof = json.loads(row[0])

# Tamper: change proof content in DB only
proof["provider_observed"]["provider_note"] = "evil-simulated-provider"

new_json = json.dumps(proof, sort_keys=True)

# IMPORTANT: do NOT update this_hash or signature
cur.execute("UPDATE proof_ledger SET proof_json = ? WHERE session_id = ?", (new_json, sid))

conn.commit()
print("Tampered session:", sid)
print("Rows updated:", cur.rowcount)
conn.close()
