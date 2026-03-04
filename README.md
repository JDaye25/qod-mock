# QoD Mock (qod-mock)

## What this project is
This project is a small Quality-on-Demand (QoD) proof service + test harness. It lets you:
- Create a QoD “intent” (what performance you want)
- Post telemetry measurements (what actually happened)
- Finalize and fetch a proof record for a session
- Produce a validated **artifact** (JSON) that follows a contract

The goal is reproducibility: a clean environment should be able to run the project and produce the same expected artifact format.

---

## Inputs / Outputs / Success Criteria

### Purpose
Take a user request plus telemetry outcomes and produce a validated “artifact” (structured JSON) so downstream systems can reliably consume the result.

---

### Inputs

#### Accepted input formats
- JSON HTTP requests to the API endpoints (local FastAPI server)

#### Core API flow (high level)
1. `POST /intent` → creates a session based on requested targets
2. `POST /telemetry` → submits measurements for that session
3. `POST /proof/{session_id}/finalize` → finalizes a proof
4. `GET /proof/{session_id}` → fetches the proof record

---

### Outputs

#### Exact output files
On success, the system produces an artifact JSON file:

- `artifacts/<session_id>/artifact.json`

This artifact is validated against:

- `schemas/artifact.v1.json`

---

### Success Criteria (Pass/Fail)

A run is a **PASS** only if all are true:
1. Smoke tests pass (`py -m unittest -v`)
2. The artifact JSON is valid JSON
3. The artifact JSON matches `schemas/artifact.v1.json`
4. Evidence logs can be captured from a clean-room run

Otherwise the run is a **FAIL**.

## Artifact Contract (Non-Negotiable)

This service is considered correct only when it produces a schema-valid artifact + proof record after finalization.

### How to run (local)
```bash
py -m uvicorn backend.main:app --reload --port 8000

---

## Repo Layout (important folders)
- `backend/` — FastAPI app (QoD mock service)
- `tests/` — smoke tests (starts server, runs intent→telemetry→finalize→proof, writes + validates artifact)
- `schemas/` — JSON Schema contracts
- `src/` — helper code (schema validation)
- `artifacts/` — runtime outputs + clean-room evidence (ignored by git)

---

## Quick Start (normal dev run)

### 1) Create & activate venv
```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1

## Clean-Room Run (Reproducibility Check)

1. Create a new virtual environment:
   py -m venv .venv_clean
   .\.venv_clean\Scripts\Activate.ps1

2. Upgrade pip:
   py -m pip install --upgrade pip

3. Install pinned dependencies:
   py -m pip install -r requirements.txt

4. Run tests:
   py -m unittest -v

5. Start server:
   py -m uvicorn backend.main:app --host 127.0.0.1 --port 8000

Expected result:
- All tests pass
- Server starts successfully
- Artifact is written under ./artifacts/

## API Endpoint Inventory

Any new route must be added to this inventory in the same pull request.

| Method | Path                           | Auth required | Data sensitivity | Purpose                                | Notes                                                          |
| ------ | ------------------------------ | ------------: | ---------------- | -------------------------------------- | -------------------------------------------------------------- |
| GET    | `/health`                      |            No | None             | Health check / uptime probe            | Safe to expose publicly                                        |
| POST   | `/intent`                      |           No* | Low–Medium       | Start a QoD session / request intent   | *If you plan to monetize, this should require an API key later |
| GET    | `/sessions`                    |           No* | Medium           | List sessions                          | Consider restricting in production                             |
| GET    | `/sessions/{session_id}`       |           No* | Medium           | Get session details                    | Consider restricting in production                             |
| DELETE | `/sessions/{session_id}`       |           No* | Medium           | Delete session                         | Should require auth in production                              |
| POST   | `/telemetry`                   |           No* | Medium           | Client telemetry ingest                | Rate-limit + validate payload size                             |
| POST   | `/proof/{session_id}/finalize` |           No* | Medium–High      | Generate final proof/artifact          | This is your “money endpoint” — should require auth            |
| GET    | `/proof/{session_id}`          |           No* | Medium–High      | Retrieve proof/artifact status/details | Should require auth in production                              |
