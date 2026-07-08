"""Load JSON Schemas and validate data records against them.

Schemas live as .json files in the repo-root ``schemas/`` directory, one per
record type (``evidence``, ``stance``, ``candidates``, ``sources``, ``topics``).
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"


@lru_cache(maxsize=None)
def _validator(name: str) -> Draft202012Validator:
    path = SCHEMA_DIR / f"{name}.schema.json"
    if not path.exists():
        raise KeyError(f"No schema named {name!r} (looked in {path})")
    schema = json.loads(path.read_text())
    return Draft202012Validator(schema)


def validate(record: dict, schema_name: str) -> None:
    """Validate ``record`` against the named schema.

    Raises ``jsonschema.exceptions.ValidationError`` on the first violation,
    or ``KeyError`` if ``schema_name`` has no schema file.
    """
    _validator(schema_name).validate(record)
