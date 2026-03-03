\# Validation Evidence (Release)



\## What was validated

\- Unit tests: `py -m unittest -v` (pass)

\- Canonical run command (Windows): `.\\run.ps1`

&nbsp; - Loads `.env`

&nbsp; - Starts API server

\- Docker Compose mini-prod:

&nbsp; - `docker compose up -d --build`

&nbsp; - Health check: `curl http://127.0.0.1:8000/health` -> {"status":"ok"}

&nbsp; - Restart resilience: `docker compose restart` then health check still OK



\## Repro steps (Windows)

1\) Set secrets (local only):

&nbsp;  - Create `.env` in repo root with:

&nbsp;    - QOD\_CLIENT\_SECRET=...

2\) Run locally:

&nbsp;  - `.\\run.ps1`

3\) Run tests:

&nbsp;  - `py -m unittest -v`

4\) Run in Docker:

&nbsp;  - `docker compose up -d --build`

&nbsp;  - `curl http://127.0.0.1:8000/health`

&nbsp;  - `docker compose restart`

&nbsp;  - `curl http://127.0.0.1:8000/health`

