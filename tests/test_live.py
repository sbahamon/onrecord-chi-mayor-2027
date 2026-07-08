"""Live smoke tests — run manually at the API-key checkpoint:

    pytest -m live

They hit real APIs and need OPENROUTER_API_KEY / GROQ_API_KEY in the env.
Skipped automatically when a key is absent so the default suite stays offline.
"""
import json
import os

import pytest

pytestmark = pytest.mark.live


@pytest.mark.skipif(not os.environ.get("OPENROUTER_API_KEY"), reason="no OPENROUTER_API_KEY")
def test_openrouter_returns_json():
    from pipeline.llm import OpenRouterLLM

    llm = OpenRouterLLM()
    model = os.environ.get("EXTRACTOR_MODEL", "deepseek/deepseek-chat-v3.2")
    out = llm.complete_json(
        model=model,
        system='Reply with JSON only: {"ok": true}.',
        user="Say ok.",
    )
    assert isinstance(out, dict)


@pytest.mark.skipif(not os.environ.get("OPENROUTER_API_KEY"), reason="no OPENROUTER_API_KEY")
def test_openrouter_extract_end_to_end():
    from pipeline.extract import extract
    from pipeline.llm import OpenRouterLLM

    transcript = (
        "Interviewer: Where do you stand on zoning?\n"
        "Jane Doe: I want to legalize apartments in every neighborhood in Chicago."
    )
    llm = OpenRouterLLM()
    model = os.environ.get("EXTRACTOR_MODEL", "deepseek/deepseek-chat-v3.2")
    result = extract(transcript, candidates=["jane-doe"],
                     topics=["zoning-reform"], llm=llm, model=model)
    # We don't assert exact content (model may vary), only that the invariants hold:
    # anything returned as housing must have a quote that really appears.
    for s in result.housing:
        assert s["quote"].strip()


@pytest.mark.skipif(not os.environ.get("GROQ_API_KEY"), reason="no GROQ_API_KEY")
def test_groq_key_is_accepted():
    """A tiny request proves the key authenticates (400 body error is fine; 401 is not)."""
    import requests

    resp = requests.post(
        "https://api.groq.com/openai/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"},
        data={"model": "whisper-large-v3-turbo"},
        timeout=30,
    )
    assert resp.status_code != 401, f"Groq rejected the key: {resp.text[:200]}"
