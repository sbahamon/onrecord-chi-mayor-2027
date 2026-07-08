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
    result = ExtractResult()
    for stmt in raw:
        try:
            schemas.validate(stmt, "statement")
        except ValidationError as e:
            raise ExtractionError(f"statement failed schema: {e.message}") from e

        if stmt["candidate"] not in known:
            result.dropped += 1
            continue
        if not quote_in_transcript(stmt["quote"], transcript):
            result.dropped += 1
            continue

        (result.housing if stmt["is_housing"] else result.other).append(stmt)

    return result
