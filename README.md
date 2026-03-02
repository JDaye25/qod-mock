## Inputs / Outputs / Success Criteria

### Purpose
This project takes user requests plus provided sources (text/files/URLs) and produces a validated “artifact” (structured JSON, optionally with a readable report) so downstream systems can reliably consume the result.

---

### Inputs

#### Accepted input formats
- Plain text (UTF-8)
- Local files: PDF, DOCX, TXT (via upload or local path depending on runner)
- URLs (publicly accessible), if enabled

#### Primary run modes
- Local script: `python main.py ...`
- API mode: `POST /artifact` (if backend is enabled)

---

### Outputs

#### Exact output files
On success, the system produces:
- `artifacts/<artifact_id>/artifact.json` (always)
- `artifacts/<artifact_id>/artifact.md` (if enabled)
- `artifacts/<artifact_id>/sources.json` (recommended)

---

### Success Criteria (Pass/Fail)

A run is a PASS only if all are true:
1) Process exits with code 0 (or API returns `201 Created`)
2) `artifact.json` is valid JSON
3) `artifact.json` contains required fields: `schema_version`, `task`, `summary`, `outputs`
4) If strict citations are enabled, every non-trivial claim in outputs has at least one citation
5) Output files are written to the expected `artifacts/<artifact_id>/` folder

Otherwise the run is a FAIL.

---

### Stranger Test
A stranger can read this section and answer:
- What goes in?
- What comes out?
- How do we know it worked?