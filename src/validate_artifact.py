import json
from pathlib import Path

from jsonschema import Draft202012Validator


def validate_artifact_json(artifact_path: str | Path, schema_path: str | Path) -> None:
    """Validate an artifact JSON file against a JSON Schema file.
    Raises AssertionError with readable messages if invalid.
    """
    artifact_path = Path(artifact_path)
    schema_path = Path(schema_path)

    with schema_path.open("r", encoding="utf-8") as f:
        schema = json.load(f)

    with artifact_path.open("r", encoding="utf-8") as f:
        artifact = json.load(f)

    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(artifact), key=lambda e: list(e.path))

    if errors:
        lines = ["Artifact failed schema validation:"]
        for e in errors:
            loc = "$" + "".join(f"[{repr(p)}]" for p in e.path)
            lines.append(f"- {loc}: {e.message}")
        raise AssertionError("\n".join(lines))
        