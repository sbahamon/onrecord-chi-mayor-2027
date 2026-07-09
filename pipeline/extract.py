"""Extract candidate policy statements from a transcript with an injected LLM.

The model does the reading; this module enforces the guarantees:

* every returned statement matches the ``statement`` schema,
* a statement's quote must actually occur in the transcript (fabricated
  quotes are dropped — an accountability tracker must never invent words),
* statements about unknown candidates are dropped,
* housing statements are separated from everything else.

The LLM is any object with ``complete_json(*, model, system, user) -> dict``.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field

from jsonschema.exceptions import ValidationError

from pipeline import schemas

SYSTEM_PROMPT = (
    "You extract Chicago mayoral candidates' policy positions from a transcript. "
    "Return JSON: {\"statements\": [...]}. Each statement has candidate (slug), "
    "topic (slug), stance (supports|supports-with-conditions|opposes|mixed|"
    "no-position), summary, quote (VERBATIM from the transcript), locator "
    "(timestamp/paragraph or null), confidence (0-1), is_housing (bool), and "
    "attribution_flag (true if the candidate is describing someone ELSE's "
    "position or speaking hypothetically rather than stating their own view). "
    "Quote candidates exactly; never paraphrase inside quote."
)


class ExtractionError(RuntimeError):
    """The model returned output that could not be used."""


@dataclass
class ExtractResult:
    housing: list = field(default_factory=list)
    other: list = field(default_factory=list)
    dropped: int = 0

    @property
    def statements(self):
        return self.housing + self.other


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def quote_in_transcript(quote: str, transcript: str) -> bool:
    return _normalize(quote) in _normalize(transcript)


def build_user_prompt(transcript: str, candidates, topics) -> str:
    return (
        f"Candidates (slugs): {', '.join(candidates)}\n"
        f"Housing topics (slugs): {', '.join(topics)}\n\n"
        f"Transcript:\n{transcript}"
    )


def extract(transcript: str, *, candidates, topics, llm, model: str) -> ExtractResult:
    payload = llm.complete_json(
        model=model,
        system=SYSTEM_PROMPT,
        user=build_user_prompt(transcript, candidates, topics),
    )

    if not isinstance(payload, dict) or "statements" not in payload:
        raise ExtractionError("model output missing 'statements' key")
    raw = payload["statements"]
    if not isinstance(raw, list):
        raise ExtractionError("'statements' is not a list")

    known = set(candidates)
    known_topics = set(topics)
    result = ExtractResult()
    for stmt in raw:
        try:
            schemas.validate(stmt, "statement")
        except ValidationError as e:
            # The model occasionally emits one malformed statement (empty quote,
            # confidence -1, missing field) among good ones. Drop just that
            # statement rather than discarding the whole source. SECURITY: this
            # keeps the candidate/topic path-injection defense intact — schema
            # validation still gates every KEPT statement, so a traversal value
            # (e.g. "../../ledger") is schema-invalid and dropped here, never
            # reaching propose.write_stance's path builder.
            print(f"drop schema-invalid statement: {e.message}", file=sys.stderr)
            result.dropped += 1
            continue

        if stmt["candidate"] not in known:
            result.dropped += 1
            continue
        # topic is untrusted model output that ends up in a file path
        # (propose.write_stance); only known registry topics may pass.
        if stmt["topic"] not in known_topics:
            result.dropped += 1
            continue
        if not quote_in_transcript(stmt["quote"], transcript):
            result.dropped += 1
            continue

        (result.housing if stmt["is_housing"] else result.other).append(stmt)

    return result
