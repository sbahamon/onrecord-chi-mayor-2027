"""Command-line entrypoints wired to the real dependencies.

Subcommands (run as ``python -m pipeline <cmd>``):

  ingest-url   Manual intake of one URL -> reviewable files + PR body.
  discover     Poll feeds, triage, ingest+extract new items -> files + PR body.
  review       Verify the evidence files changed in a PR -> comment + label.

Each command writes any PR/comment text to a file so the GitHub Actions layer
(bash + gh) can post it without embedding untrusted content in shell.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

from pipeline import config, discover, run
from pipeline.llm import OpenRouterLLM


def _today() -> str:
    return datetime.date.today().isoformat()


def _write(path: str, text: str) -> None:
    Path(path).write_text(text)
    print(f"wrote {path}")


def cmd_ingest_url(args) -> int:
    data_dir = Path(args.data_dir)
    llm = OpenRouterLLM()
    cfg = config.load_config(data_dir)
    source = {
        "url": args.url,
        "outlet": args.outlet or args.url,
        "media_type": args.type,
        "title": args.title or args.url,
        "published_date": args.date or _today(),
    }
    result = run.process_source(
        source,
        data_dir=data_dir,
        llm=llm,
        extractor_model=cfg["models"]["extractor"],
        today=_today(),
        candidates=config.candidate_slugs(data_dir, active_only=True),
        topics=config.topic_slugs(data_dir),
    )
    _write(args.pr_body_out, result.pr_body)
    print(f"housing={result.housing_count} other={result.other_count}")
    return 0


def cmd_discover(args) -> int:
    import requests

    data_dir = Path(args.data_dir)
    cfg = config.load_config(data_dir)
    llm = OpenRouterLLM()
    ledger = discover.Ledger(data_dir / "ledger.json")
    candidates = config.candidate_slugs(data_dir, active_only=True)
    topics = config.topic_slugs(data_dir)

    def fetch(url):
        r = requests.get(url, timeout=30, headers={"User-Agent": "housing-tracker/0.1"})
        r.raise_for_status()
        return r.text

    bodies, processed = [], 0
    for feed in config.load_sources(data_dir):
        if not feed.get("enabled", True) or feed["type"] not in {"rss", "google-news", "youtube"}:
            continue
        try:
            items = discover.parse_feed(fetch(feed["url"]), source_id=feed["id"])
        except Exception as e:  # noqa: BLE001
            print(f"skip feed {feed['id']}: {e}", file=sys.stderr)
            continue
        for item in ledger.filter_new(items):
            ledger.mark(item["url"])
            if not discover.triage(item["title"], llm=llm, model=cfg["models"]["triage"]):
                continue
            source = {
                "url": item["url"], "outlet": feed["name"], "media_type": "article",
                "title": item["title"], "published_date": _today(),
            }
            try:
                result = run.process_source(
                    source, data_dir=data_dir, llm=llm,
                    extractor_model=cfg["models"]["extractor"], today=_today(),
                    candidates=candidates, topics=topics,
                )
            except Exception as e:  # noqa: BLE001
                print(f"skip item {item['url']}: {e}", file=sys.stderr)
                continue
            if result.housing_count:
                bodies.append(result.pr_body)
                processed += 1

    ledger.save()
    _write(args.pr_body_out, "\n\n---\n\n".join(bodies) if bodies else "No new housing statements found.")
    print(f"processed {processed} item(s) with housing content")
    return 0


def cmd_review(args) -> int:
    from pipeline import review

    data_dir = Path(args.data_dir)
    cfg = config.load_config(data_dir)
    llm = OpenRouterLLM()
    model = cfg["models"]["reviewer"]

    verdicts = []
    for ev_path in args.evidence:
        evidence = json.loads(Path(ev_path).read_text())
        tpath = run.transcript_path_for(data_dir, evidence)
        transcript = tpath.read_text() if tpath.exists() else ""
        for stmt in evidence["statements"]:
            verdicts.append(review.verify_statement(stmt, transcript, llm=llm, model=model))

    comment = review.render_review_comment(verdicts)
    label = review.decide_label(verdicts)
    auto_merge = review.should_auto_merge(verdicts, cfg)
    _write(args.comment_out, comment)
    print(f"label={label}")
    print(f"auto_merge={'true' if auto_merge else 'false'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pipeline")
    p.add_argument("--data-dir", default="data")
    sub = p.add_subparsers(dest="cmd", required=True)

    iu = sub.add_parser("ingest-url", help="manual intake of one URL")
    iu.add_argument("--url", required=True)
    iu.add_argument("--type", default="article",
                    choices=["article", "website", "youtube", "podcast", "social", "manual"])
    iu.add_argument("--outlet")
    iu.add_argument("--title")
    iu.add_argument("--date")
    iu.add_argument("--pr-body-out", default="pr_body.md")
    iu.set_defaults(func=cmd_ingest_url)

    dc = sub.add_parser("discover", help="poll feeds and process new items")
    dc.add_argument("--pr-body-out", default="pr_body.md")
    dc.set_defaults(func=cmd_discover)

    rv = sub.add_parser("review", help="verify evidence files changed in a PR")
    rv.add_argument("evidence", nargs="+", help="paths to evidence JSON files")
    rv.add_argument("--comment-out", default="review_comment.md")
    rv.set_defaults(func=cmd_review)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
