"""Load registries and pipeline config from the data/ tree."""
from __future__ import annotations

import json
from pathlib import Path

EXCLUDED_STATUSES = {"example", "withdrawn"}


def _read(data_dir, name):
    return json.loads((Path(data_dir) / "registry" / f"{name}.json").read_text())


def load_config(data_dir) -> dict:
    return _read(data_dir, "config")


def load_candidates(data_dir) -> list[dict]:
    return _read(data_dir, "candidates")["candidates"]


def load_topics(data_dir) -> list[dict]:
    return _read(data_dir, "topics")["topics"]


def load_sources(data_dir) -> list[dict]:
    return _read(data_dir, "sources")["feeds"]


def candidate_slugs(data_dir, *, active_only: bool = False) -> list[str]:
    return [
        c["slug"]
        for c in load_candidates(data_dir)
        if not (active_only and c["status"] in EXCLUDED_STATUSES)
    ]


def topic_slugs(data_dir) -> list[str]:
    return [t["slug"] for t in load_topics(data_dir)]
