#!/usr/bin/env python3
"""Enrich queued RSS/email sources with linked article text for market-worker."""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import market_rss_listener as handoff
import market_worker_cron as worker


ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get("MARKET_WORKER_DATA_DIR", str(ROOT.parent / "handoffs" / "market-worker")))
RSS_HANDOFF = Path(os.environ.get("MARKET_WORKER_RSS_HANDOFF", str(DATA_ROOT / "market-rss-handoff.jsonl")))
ARTICLE_HANDOFF = Path(os.environ.get("MARKET_WORKER_ARTICLE_HANDOFF", str(DATA_ROOT / "market-article-handoff.jsonl")))


def main() -> int:
    """Claim raw inputs, enrich bounded article links, then publish a new handoff."""
    now = dt.datetime.now(worker.TZ)
    raw_records, raw_claim = handoff.claim_handoff(RSS_HANDOFF)
    liquidity_rows = worker.read_liquidity_cache().get("rows") or {}
    valid_symbols = set(liquidity_rows) | worker.WATCHLIST
    rss_sources = worker.rss_sources_from_records(raw_records, valid_symbols)
    email_sources, email_gaps, email_state = worker.new_email_sources(defer_commit=True)
    sources = rss_sources + email_sources
    gaps = list(email_gaps)
    worker.enrich_linked_articles(sources, gaps, now)

    # The output is appended before acknowledging the raw RSS claim, so an
    # interrupted run retries raw RSS rather than losing it. Downstream packet
    # processing has the same claim/ack behavior.
    written = handoff.append_handoff(sources, ARTICLE_HANDOFF)
    if email_state is not None:
        worker.save_email_corpus_state(email_state)
    handoff.acknowledge_handoff(raw_claim)
    print(f"article enrichment: {written} records queued at {ARTICLE_HANDOFF}")
    for gap in gaps:
        print(f"article enrichment: {gap}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
