#!/usr/bin/env python3
"""Lightweight RSS/Atom collector that writes normalized records to a handoff."""
from __future__ import annotations

import os
from pathlib import Path

import market_rss_listener as rss


ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get("MARKET_WORKER_DATA_DIR", str(ROOT.parent / "handoffs" / "market-worker")))
CONFIG = Path(os.environ.get("MARKET_WORKER_RSS_CONFIG", str(DATA_ROOT / "market-rss-feeds.json")))
STATE = Path(os.environ.get("MARKET_WORKER_RSS_STATE", str(DATA_ROOT / "market-rss-listener-state.json")))
HANDOFF = Path(os.environ.get("MARKET_WORKER_RSS_HANDOFF", str(DATA_ROOT / "market-rss-handoff.jsonl")))
BACKFILL = os.environ.get("MARKET_WORKER_RSS_BACKFILL", "0").lower() in {"1", "true", "yes"}


def main() -> int:
    records, gaps = rss.poll(config_path=CONFIG, state_path=STATE, backfill=BACKFILL)
    written = rss.append_handoff(records, HANDOFF)
    print(f"rss collector: {written} new records queued at {HANDOFF}")
    for gap in gaps:
        print(f"rss collector: {gap}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
