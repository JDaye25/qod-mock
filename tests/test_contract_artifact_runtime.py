import json
import unittest
import uuid
from datetime import datetime
from pathlib import Path

from jsonschema import Draft202012Validator


REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "docs" / "artifact.schema.json"
ARTIFACTS_ROOT = REPO_ROOT / "artifacts"


def load_json(path: Path):
    # utf-8-sig handles BOM if Windows wrote a BOM
    return json.loads(path.read_text(encoding="utf-8-sig"))


def find_latest_artifact():
    if not ARTIFACTS_ROOT.exists():
        return None

    candidates = list(ARTIFACTS_ROOT.glob("*/artifact.json"))
    if not candidates:
        return None

    # newest by modified time
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


class TestContractArtifactRuntime(unittest.TestCase):
    def test_artifact_matches_schema_and_invariants(self):
        self.assertTrue(SCHEMA_PATH.exists(), f"Missing schema at {SCHEMA_PATH}")

        artifact_path = find_latest_artifact()
        self.assertIsNotNone(
            artifact_path,
            "No artifact.json found under ./artifacts. Run the integration test or clean_room script first.",
        )

        schema = load_json(SCHEMA_PATH)
        artifact = load_json(artifact_path)

        # 1) JSON Schema validation (shape)
        v = Draft202012Validator(schema)
        errors = sorted(v.iter_errors(artifact), key=lambda e: e.path)
        if errors:
            msg = "\n".join([f"{list(e.path)}: {e.message}" for e in errors])
            self.fail("Artifact failed schema validation:\n" + msg)

        # 2) Simple invariants (truthiness checks)
        # session_id should be a UUID
        uuid.UUID(artifact["session_id"])

        # created_at should parse as datetime-ish
        # (schema says date-time; we verify it’s at least parseable)
        datetime.fromisoformat(artifact["created_at"].replace("Z", "+00:00"))

        # decision.result must be pass/fail already ensured by schema, but let's sanity check reasons not empty
        self.assertIsInstance(artifact["decision"]["reasons"], list)
        self.assertGreaterEqual(len(artifact["decision"]["reasons"]), 1)

        # measured numbers must be >= 0 (schema enforces, but we check anyway)
        self.assertGreaterEqual(artifact["measured"]["latency_ms"], 0)
        self.assertGreaterEqual(artifact["measured"]["throughput_mbps"], 0)
        self.assertGreaterEqual(artifact["measured"]["availability_pct"], 0)
        