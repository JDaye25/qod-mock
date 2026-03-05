# QoD Mock (qod-mock) — v0.3

## What this project is
This repo is a small **Quality-on-Demand (QoD) proof service + test harness**. It lets you:

- Create a QoD “intent” (what performance you want)
- Post telemetry measurements (what actually happened)
- Finalize and fetch a signed proof record for a session
- Produce a validated **runtime artifact** (JSON) that follows a contract
- Verify tamper-evidence using:
  - runtime artifact hashing
  - a ledger hash chain
  - **Ed25519 signatures**
  - public key discovery via **JWKS**

The goal is reproducibility: a clean environment should be able to run the project and produce the same expected artifact format and verification behavior.

---

# Architecture (high level)


Client/Test Harness
|
| POST /intent
| POST /telemetry
| POST /proof/{sid}/finalize
v
QoD API (FastAPI)
|
| writes:
| - SQLite rows (sessions/telemetry/proof_ledger)
| - runtime artifact JSON (artifacts/{sid}/artifact.json)
| - wrapper artifact JSON (artifacts/{sid}/artifact_v1.json)
| - proof snapshot JSON (artifacts/proof_{sid}_*.json)
v
Artifacts + Ledger
|
| independent verification:
| - ledger hash recompute
| - runtime artifact hash recompute
| - signature verify (Ed25519)
| - key discovery from JWKS
v
Verifier (scripts/verify_signature.py)


---

# Inputs / Outputs / Success Criteria

## Purpose
Take a user request plus telemetry outcomes and produce a validated “artifact” (structured JSON) so downstream systems can reliably consume the result — and so third parties can verify results independently.

---

## Inputs

### Accepted input formats
- JSON HTTP requests to the API endpoints (FastAPI server)

### Core API flow (high level)
1. `POST /intent` → creates a session based on requested targets  
2. `POST /telemetry` → submits measurements for that session  
3. `POST /proof/{session_id}/finalize` → finalizes a proof + writes artifacts  
4. `GET /proof/{session_id}` → fetches the proof record  
5. `GET /proof/{session_id}/verify` → server-side verification report (hashes + signature + chain)

---

# Outputs

## Exact output files (artifacts directory)

On success, the system produces:

- `artifacts/{session_id}/artifact.json`  
  The **runtime artifact** (contract JSON consumed by downstream systems)

- `artifacts/{session_id}/artifact_v1.json`  
  A wrapper “run output” artifact containing the proof record

- `artifacts/proof_{session_id}_YYYYMMDD_HHMMSS.json`  
  A snapshot containing `{ prev_hash, this_hash, signature, kid, proof }`

- `artifacts/run_summaries/run_YYYYMMDD_HHMMSS_{session_id}.json`  
  A run summary with timing + file paths

---

# Tamper-evident proof design (what gets verified)

Each proof has multiple layers of checks:

1) **Runtime artifact hash**
- Proof stores: `runtime_artifact_sha256`
- Verifier recomputes hash of `artifacts/{sid}/artifact.json`

2) **Ledger hash chain**
- Each ledger entry stores `prev_hash` and `this_hash`
- `this_hash = sha256(prev_hash + "|" + canonical_json(proof))`

3) **Ed25519 signature**
- The service signs `this_hash` using Ed25519
- Stored fields: `signature` + `kid`
- Verifier checks signature using the public key

---

# Public key discovery (JWKS + friendly endpoint)

The service exposes public verification keys so external verifiers can validate signatures.

### JWKS (standard)
- `GET /.well-known/jwks.json`

Example:
```bash
curl http://127.0.0.1:8010/.well-known/jwks.json