"""OpenRouter client: the JSON-extraction and retry logic is unit-tested with a
fake HTTP transport (no key, no network). The live path is exercised separately
under the 'live' marker at the API-key checkpoint.
"""
import pytest

from pipeline.llm import OpenRouterLLM, LLMError


def chat_response(content):
    """Shape of an OpenAI-compatible chat completion."""
    return {"choices": [{"message": {"content": content}}]}


def test_complete_json_parses_plain_json_content():
    llm = OpenRouterLLM(api_key="x", post=lambda **kw: chat_response('{"statements": []}'))
    assert llm.complete_json(model="m", system="s", user="u") == {"statements": []}


def test_complete_json_strips_markdown_code_fences():
    fenced = "```json\n{\"a\": 1}\n```"
    llm = OpenRouterLLM(api_key="x", post=lambda **kw: chat_response(fenced))
    assert llm.complete_json(model="m", system="s", user="u") == {"a": 1}


def test_complete_json_retries_then_succeeds():
    attempts = {"n": 0}

    def flaky(**kw):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise ConnectionError("boom")
        return chat_response('{"ok": true}')

    llm = OpenRouterLLM(api_key="x", post=flaky, max_retries=3)
    assert llm.complete_json(model="m", system="s", user="u") == {"ok": True}
    assert attempts["n"] == 2


def test_complete_json_raises_on_unparseable_content():
    llm = OpenRouterLLM(api_key="x", post=lambda **kw: chat_response("not json at all"))
    with pytest.raises(LLMError):
        llm.complete_json(model="m", system="s", user="u")


def test_missing_api_key_raises_before_any_call():
    with pytest.raises(LLMError):
        OpenRouterLLM(api_key="")
