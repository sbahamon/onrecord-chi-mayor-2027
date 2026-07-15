"""Command-line entrypoints wired to the real dependencies.

Subcommands (run as ``python -m pipeline <cmd>``):

  ingest-url   Manual intake of one URL -> reviewable files + PR body.
  discover     Poll feeds, triage, ingest+extract new items -> files + PR body.
  backfill     Process a URL list -> files + one PR body per candidate.
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

from pipeline import backfill as backfill_mod
from pipeline import bluesky, config, discover, ingest as ingest_mod, run
from pipeline.llm import OpenRouterLLM


_BACKFILL_DIVIDER = "\n\n---\n\n"


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
        "outlet": args.outlet or ingest_mod.domain_of(args.url),
        "media_type": args.type,
        "title": args.title,  # None -> ingest fills from the page for articles
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
    data_dir = Path(args.data_dir)
    cfg = config.load_config(data_dir)
    llm = OpenRouterLLM()
    ledger = discover.Ledger(data_dir / "ledger.json")
    candidates = config.candidate_slugs(data_dir, active_only=True)
    topics = config.topic_slugs(data_dir)

    disc_cfg = cfg.get("discovery", {})
    # Bound cost + PR size: cap fresh items per run, and per feed so one noisy
    # feed can't starve later high-signal feeds (podcasts, Bluesky).
    max_items = args.max_items or disc_cfg.get("max_items_per_run", 25)
    max_items_per_feed = disc_cfg.get("max_items_per_feed")
    # Google News item links are news.google.com redirects a plain fetch can't
    # read; gate them off until a headless-fetch path (#30) can resolve them.
    google_news_enabled = disc_cfg.get("google_news_enabled", True)

    # Same browser-UA fetcher ingest/review use, so a site that 403s a non-browser
    # agent behaves identically for feed and article fetches.
    fetch = ingest_mod._default_fetcher
    triage_model = cfg["models"]["triage"]
    extractor_model = cfg["models"]["extractor"]

    def item_fetcher(feed):
        if feed["type"] == "bluesky":
            # Bluesky is a JSON API, not RSS; the client returns the post text on
            # each item, which ingest uses directly (no fetch/transcribe).
            return bluesky.fetch_author_feed(feed["url"])
        # The feed declares its media type; route ingestion on it instead of
        # forcing "article" (which sent youtube/podcast items down the text path).
        media_type = discover.media_type_for_feed(feed)
        return discover.parse_feed(
            fetch(feed["url"]), source_id=feed["id"],
            prefer_enclosure=(media_type == "podcast"),
        )

    def process_fn(feed, item):
        media_type = discover.media_type_for_feed(feed)
        # A candidate's own Bluesky feed is first-person with no name in the text,
        # so scope extraction to that candidate; the extractor would otherwise
        # mis-attribute the post. Other feeds keep the full candidate set.
        feed_candidates = [feed["candidate"]] if feed.get("candidate") else candidates
        source = {
            "url": item["url"], "outlet": feed["name"], "media_type": media_type,
            "title": item["title"], "published_date": _today(),
            "text": item.get("text"),  # set for Bluesky; None (ignored) otherwise
        }
        # process_source retries the extraction internally (a lone bad field no
        # longer loses the item); run_discovery skips only on a hard failure.
        return run.process_source(
            source, data_dir=data_dir, llm=llm, extractor_model=extractor_model,
            today=_today(), candidates=feed_candidates, topics=topics,
        )

    def triage_fn(title):
        return discover.triage(title, llm=llm, model=triage_model)

    feeds = discover.active_media_feeds(
        config.discovery_feeds(data_dir), google_news_enabled=google_news_enabled
    )
    result = discover.run_discovery(
        feeds, ledger=ledger, item_fetcher=item_fetcher, triage_fn=triage_fn,
        process_fn=process_fn, max_items=max_items,
        max_items_per_feed=max_items_per_feed,
    )

    _write(args.pr_body_out,
           "\n\n---\n\n".join(result.bodies) if result.bodies
           else "No new housing statements found.")
    print(f"processed {result.housing_hits} item(s) with housing content")
    return 0


def cmd_backfill(args) -> int:
    data_dir = Path(args.data_dir)
    cfg = config.load_config(data_dir)
    llm = OpenRouterLLM()
    topics = config.topic_slugs(data_dir)

    raw = json.loads(Path(args.input).read_text())
    rows = raw["rows"] if isinstance(raw, dict) else raw
    if args.only:
        rows = [r for r in rows if r["candidate_slug"] == args.only]

    # Per-candidate PRs each add their own URL; the ledger is seeded once by a
    # dedicated run (without --skip-ledger) to keep those PRs conflict-free.
    ledger = None if args.skip_ledger else discover.Ledger(data_dir / "ledger.json")

    buckets = backfill_mod.run_backfill(
        rows, data_dir=data_dir, llm=llm,
        extractor_model=cfg["models"]["extractor"], today=_today(),
        topics=topics, ledger=ledger,
    )

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    total_housing = 0
    for b in buckets:
        body_path = out_dir / f"{b.candidate_slug}.md"
        body_path.write_text(b.pr_body or f"No housing statements found for {b.candidate_slug}.")
        total_housing += b.housing_count
        manifest.append({
            "candidate": b.candidate_slug,
            "branch": f"backfill/{b.candidate_slug}",
            "body_path": str(body_path),
            "housing_count": b.housing_count,
            "paths": [str(p) for p in b.paths],
        })

    # Combined body (for a single --only run this is just that candidate's body).
    _write(args.pr_body_out, _BACKFILL_DIVIDER.join(b.pr_body for b in buckets if b.pr_body)
           or "No housing statements found.")
    if args.manifest_out:
        _write(args.manifest_out, json.dumps(manifest, indent=2))
    print(f"candidates={len(buckets)} housing={total_housing}")

    # Surface rows that never succeeded (even after retries) loudly, and exit
    # non-zero so the workflow marks the job failed instead of opening an empty PR.
    errors = [(b.candidate_slug, url, msg) for b in buckets for (url, msg) in b.errors]
    for slug, url, msg in errors:
        print(f"ERROR backfill {slug} {url}: {msg}", file=sys.stderr)
    return 1 if errors else 0


def cmd_review(args) -> int:
    from pipeline import review

    data_dir = Path(args.data_dir)
    cfg = config.load_config(data_dir)
    llm = OpenRouterLLM()
    model = cfg["models"]["reviewer"]

    verdicts = []
    for ev_path in args.evidence:
        evidence = json.loads(Path(ev_path).read_text())
        # Re-ingest the source to rebuild the transcript (not stored in-repo).
        verdicts.extend(review.review_evidence(
            evidence, llm=llm, model=model, ingest_fn=ingest_mod.ingest
        ))

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
    dc.add_argument("--max-items", type=int, default=None,
                    help="cap fresh items ingested this run (default: config value)")
    dc.set_defaults(func=cmd_discover)

    bf = sub.add_parser("backfill", help="process a URL list into one PR per candidate")
    bf.add_argument("--input", required=True, help="JSON rows: [{candidate_slug, url, type?, outlet?, date?}]")
    bf.add_argument("--only", help="process only this candidate slug (workflow matrix)")
    bf.add_argument("--out-dir", default=".", help="dir for per-candidate <slug>.md PR bodies")
    bf.add_argument("--pr-body-out", default="pr_body.md")
    bf.add_argument("--manifest-out", help="write per-candidate {branch, body_path, ...} JSON")
    bf.add_argument("--skip-ledger", action="store_true",
                    help="do not touch data/ledger.json (matrix jobs; seed it once separately)")
    bf.set_defaults(func=cmd_backfill)

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
