"""Walk the data/ tree and map each content file to the schema it must satisfy.

Location determines schema:

  data/registry/candidates.json -> candidates
  data/registry/sources.json    -> sources
  data/registry/topics.json     -> topics
  data/registry/config.json     -> config
  data/media-hits/**/*.json     -> evidence
  data/stances/**/*.json        -> stance

Operational files (ledger.json) and the unreviewed non-housing captures under
data/positions/other/ are intentionally skipped — they are not published.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator, Tuple

_REGISTRY = {
    "candidates": "candidates",
    "sources": "sources",
    "topics": "topics",
    "config": "config",
}


def iter_data_files(data_dir: Path) -> Iterator[Tuple[Path, str]]:
    """Yield (path, schema_name) for every schema-checked file under data_dir."""
    data_dir = Path(data_dir)

    registry = data_dir / "registry"
    for stem, schema in _REGISTRY.items():
        path = registry / f"{stem}.json"
        if path.exists():
            yield path, schema

    for path in sorted((data_dir / "media-hits").rglob("*.json")):
        yield path, "evidence"

    for path in sorted((data_dir / "stances").rglob("*.json")):
        yield path, "stance"
