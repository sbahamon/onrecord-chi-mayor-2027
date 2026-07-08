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


def _real_post(*, url, headers, json_body, timeout):
    import requests

    resp = requests.post(url, headers=headers, json=json_body, timeout=timeout)
    resp.raise_for_status()
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
                if attempt < self.max_retries - 1 and self.retry_sleep:
                    time.sleep(self.retry_sleep * (attempt + 1))
        raise LLMError(f"request failed after {self.max_retries} attempts: {last}")
