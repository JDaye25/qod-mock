# QoD Mock (qod-mock)

## What this project is
This project is a small Quality-on-Demand (QoD) proof service + test harness. It lets you:
- Create a QoD “intent” (what performance you want)
- Post telemetry measurements (what actually happened)
- Finalize and fetch a proof record for a session
- Produce a validated **artifact** (JSON) that follows a contract

The goal is reproducibility: a clean environment should be able to run the project and produce the same expected artifact format.

The system now also includes **tamper-evident verification** using:
- runtime artifact hashing
- a ledger hash chain
- an HMAC signature

---

# Inputs / Outputs / Success Criteria

## Purpose
Take a user request plus telemetry outcomes and produce a validated “artifact” (structured JSON) so downstream systems can reliably consume the result.

---

## Inputs

### Accepted input formats
- JSON HTTP requests to the API endpoints (local FastAPI server)

### Core API flow (high level)

1. `POST /intent` → creates a session based on requested targets  
2. `POST /telemetry` → submits measurements for that session  
3. `POST /proof/{session_id}/finalize` → finalizes a proof  
4. `GET /proof/{session_id}` → fetches the proof record  

---

# Outputs

## Exact output files

On success, the system produces an artifact JSON file:
