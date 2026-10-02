#!/usr/bin/env bash
set -euo pipefail

cd /home/pi/.openclaw/workspace/market-worker
umask 022
exec 9>/dev/shm/openclaw-market-rss-collector.lock
flock -n 9 || {
  echo "market RSS collector already running; skipped overlapping run"
  exit 0
}
python3 ./market_rss_collect.py
