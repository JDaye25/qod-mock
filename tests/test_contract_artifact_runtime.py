import json
import unittest
from pathlib import Path

import jsonschema

REQUIRED_RUNTIME_KEYS = {
    "session_id",
    "created_at",
    "qos_profile",
    "inputs",
    "measured",
    "decision",
}


def _looks_like_runtime_artifact(obj) -> bool:
    if not isinstance(obj, dict):
        return False
    return REQUIRED_RUNTIME_KEYS.issubset(set(obj.keys()))


def _find_runtime_artifact_json(artifacts_root: Path):
    """
    Find the correct runtime contract artifact.json under ./artifacts.

    Important: some tests/tools may write a "wrapper" artifact that is NOT the runtime contract shape.
    The runtime schema test must validate the runtime artifact, not the wrapper.
    """
    candidates = list(artifacts_root.rglob("artifact.json"))
    if not candidates:
        return None

    # Prefer an artifact.json that actually looks like the runtime schema shape
    for p in candidates:
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if _looks_like_runtime_artifact(obj):
            return p

    # If we got here, artifact.json files exist but none match the runtime shape
    return candidates[0]  # fallback: return something so error message is informative


class TestContractArtifactRuntime(unittest.TestCase):
    def test_artifact_matches_schema_and_invariants(self):
        artifacts_root = Path("artifacts")
        artifact_path = _find_runtime_artifact_json(artifacts_root)

        self.assertIsNotNone(
            artifact_path,
            "No artifact.json found under ./artifacts. Run the integration test or clean_room script first.",
        )

        schema_path = Path("docs") / "artifact.schema.json"
        self.assertTrue(schema_path.exists(), f"Missing schema file: {schema_path}")

        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        artifact = json.loads(Path(artifact_path).read_text(encoding="utf-8"))

        # If we found an artifact.json but it's the wrapper shape, make the failure crystal clear
        if not _looks_like_runtime_artifact(artifact):
            keys = sorted(list(artifact.keys())) if isinstance(artifact, dict) else [str(type(artifact))]
            self.fail(
                "Found artifact.json but it does NOT look like the runtime contract artifact.\n"
                f"Picked: {artifact_path}\n"
                f"Top-level keys: {keys}\n"
                "This usually means a wrapper artifact was written as artifact.json somewhere.\n"
                "Expected runtime keys: "
                + ", ".join(sorted(REQUIRED_RUNTIME_KEYS))
            )

        validator = jsonschema.Draft202012Validator(schema)
        errors = sorted(validator.iter_errors(artifact), key=lambda e: e.path)

        if errors:
            msg = "\n".join([f"{list(e.path)}: {e.message}" for e in errors])
            self.fail("Artifact failed schema validation:\n" + msg)

        # Invariants (light sanity checks)
        self.assertIn(artifact["decision"]["result"], ["pass", "fail"])
        self.assertIsInstance(artifact["decision"]["reasons"], list)
        self.assertGreaterEqual(len(artifact["decision"]["reasons"]), 1)