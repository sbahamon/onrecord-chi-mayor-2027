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


class FakeResponse:
    """Minimal stand-in for a requests.Response."""

    def __init__(self, status_code, text='{"error": {"message": "boom"}}'):
        self.status_code = status_code
        self.text = text

    def json(self):
        import json as _json
        return _json.loads(self.text)


def test_http_failure_surfaces_the_response_body():
    """A bare `400 Bad Request` is undiagnosable: OpenRouter puts the actual
    reason (bad slug, unsupported response_format, context overflow) only in the
    body, and raise_for_status() throws it away. This cost a live debugging
    round-trip on the first backfill review run.
    """
    from pipeline.llm import _check_status, HTTPStatusError

    with pytest.raises(HTTPStatusError) as exc:
        _check_status(FakeResponse(400, '{"error": {"message": "no endpoints found"}}'))
    assert exc.value.status == 400
    assert "no endpoints found" in str(exc.value)


def test_a_permanent_client_error_is_not_retried():
    """400/401/404 fail identically every time; three attempts just hide the
    reason behind a retry count and triple the latency before the same failure.
    """
    from pipeline.llm import HTTPStatusError

    attempts = {"n": 0}

    def rejecting(**kw):
        attempts["n"] += 1
        raise HTTPStatusError(400, '{"error": {"message": "no endpoints found"}}')

    llm = OpenRouterLLM(api_key="x", post=rejecting, max_retries=3)
    with pytest.raises(LLMError) as exc:
        llm.complete_json(model="m", system="s", user="u")
    assert attempts["n"] == 1, "a permanent client error must not be retried"
    assert "no endpoints found" in str(exc.value), "the reason must reach the log"


def test_rate_limits_and_server_errors_are_still_retried():
    """429/5xx are the transient cases the retry loop exists for."""
    from pipeline.llm import HTTPStatusError

    for status in (429, 500, 503):
        attempts = {"n": 0}

        def flaky(**kw):
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise HTTPStatusError(status, "slow down")
            return chat_response('{"ok": true}')

        llm = OpenRouterLLM(api_key="x", post=flaky, max_retries=3)
        assert llm.complete_json(model="m", system="s", user="u") == {"ok": True}
        assert attempts["n"] == 2, f"{status} should have been retried"
