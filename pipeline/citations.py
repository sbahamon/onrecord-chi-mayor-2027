"""Resolve stance citations back to the media-hit statement that supports them.

A citation string is ``"<evidence-id>#<statement-index>"``.
"""
from __future__ import annotations

from typing import Tuple


class CitationError(ValueError):
    """A citation is malformed or points at something that does not exist."""


def parse_citation(citation: str) -> Tuple[str, int]:
    """Split ``"id#index"`` into ``(id, index)``."""
    if "#" not in citation:
        raise CitationError(f"citation missing '#<index>': {citation!r}")
    evidence_id, _, index = citation.rpartition("#")
    if not evidence_id or not index.isdigit():
        raise CitationError(f"malformed citation: {citation!r}")
    return evidence_id, int(index)


def resolve_citation(citation: str, evidence_index: dict) -> Tuple[dict, dict]:
    """Return ``(statement, evidence)`` for a citation.

    ``evidence_index`` maps evidence id -> evidence record.
    Raises ``CitationError`` for unknown ids or out-of-range indices.
    """
    evidence_id, index = parse_citation(citation)
    evidence = evidence_index.get(evidence_id)
    if evidence is None:
        raise CitationError(f"unknown evidence id: {evidence_id!r}")
    statements = evidence.get("statements", [])
    if index >= len(statements):
        raise CitationError(
            f"citation index {index} out of range for {evidence_id!r} "
            f"({len(statements)} statements)"
        )
    return statements[index], evidence
