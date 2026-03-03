import json
import os
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ARTIFACT_SCHEMA_PATH = os.path.join(REPO_ROOT, "docs", "artifact.schema.json")
OPENAPI_SNAPSHOT_PATH = os.path.join(REPO_ROOT, "docs", "openapi.snapshot.yaml")

class TestContractDocs(unittest.TestCase):
    def test_docs_are_pinned(self):
        self.assertTrue(os.path.exists(ARTIFACT_SCHEMA_PATH),
                        f"Missing required contract file: {ARTIFACT_SCHEMA_PATH}")
        with open(ARTIFACT_SCHEMA_PATH, "r", encoding="utf-8") as f:
            json.load(f)  # must be valid JSON

        self.assertTrue(os.path.exists(OPENAPI_SNAPSHOT_PATH),
                        f"Missing required contract file: {OPENAPI_SNAPSHOT_PATH}")
        with open(OPENAPI_SNAPSHOT_PATH, "r", encoding="utf-8") as f:
            self.assertTrue(f.read().strip(),
                            "openapi.snapshot.yaml exists but is empty (contract must be pinned).")

if __name__ == "__main__":
    unittest.main()
