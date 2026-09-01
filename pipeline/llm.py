"""Minimal OpenRouter chat client that returns parsed JSON.

OpenRouter is OpenAI-compatible, so any model id works over one endpoint. The
HTTP POST is injectable (``post=``) so the parsing/retry logic is unit-testable
without a key or network. In production, ``post`` defaults to a requests call.
"""
from __future__ import annotations

import json
import os
import re
import time

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class LLMError(RuntimeError):
    pass


class HTTPStatusError(Exception):
    """A non-2xx response, carrying the body OpenRouter put the reason in."""

    def __init__(self, status: int, body: str = ""):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {body[:_BODY_CHARS]}" if body else f"HTTP {status}")


# 4xx codes that are worth another attempt; every other 4xx fails identically
# on retry (bad model slug, unsupported response_format, rejected key).
_RETRYABLE_STATUSES = {408, 409, 425, 429}
_BODY_CHARS = 500


def _check_status(resp) -> None:
    """raise_for_status() reports only the status line; the reason is in the body."""
    if resp.status_code >= 400:
        raise HTTPStatusError(resp.status_code, (getattr(resp, "text", "") or "").strip())


def _real_post(*, url, headers, json_body, timeout):
    import requests

    resp = requests.post(url, headers=headers, json=json_body, timeout=timeout)
    _check_status(resp)
    return resp.json()


def _extract_json(content: str) -> dict:
    text = content.strip()
    if text.startswith("```"):
        text = _FENCE.sub("", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMError(f"model did not return valid JSON: {content[:200]!r}") from e


class OpenRouterLLM:
    def __init__(self, api_key=None, *, post=None, max_retries=3, timeout=120,
                 retry_sleep=0.0):
        self.api_key = api_key if api_key is not None else os.environ.get("OPENROUTER_API_KEY", "")
        if not self.api_key:
            raise LLMError("OPENROUTER_API_KEY is not set")
        self._post = post or (lambda **kw: _real_post(
            url=ENDPOINT, headers=self._headers(),
            json_body=kw["json_body"], timeout=timeout))
        self.max_retries = max_retries
        self.retry_sleep = retry_sleep

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-Title": "Chicago Housing Tracker",
        }

    def complete_json(self, *, model, system, user):
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
        }
        last = None
        for attempt in range(self.max_retries):
            try:
                data = self._post(json_body=body)
                content = data["choices"][0]["message"]["content"]
                return _extract_json(content)
            except LLMError:
                raise
            except Exception as e:  # network / transient
                last = e
                status = getattr(e, "status", None)
                if status is not None and 400 <= status < 500 and status not in _RETRYABLE_STATUSES:
                    # Permanent: retrying only hides the reason behind a count.
                    raise LLMError(f"request rejected: {e}") from e
                if attempt < self.max_retries - 1 and self.retry_sleep:
                    time.sleep(self.retry_sleep * (attempt + 1))
        raise LLMError(f"request failed after {self.max_retries} attempts: {last}")
