#!/usr/bin/env bash
set -euo pipefail

cd /home/pi/.openclaw/workspace/market-worker
umask 022
exec 9>/dev/shm/openclaw-market-pullback-shadow.lock
flock -n 9 || {
  echo "market pullback shadow already running; skipped overlapping run"
  exit 0
}

# Reads only the local completed-bar cache.  This job has no Telegram or broker
# delivery path and never places orders.
timeout --kill-after=30s 5m python3 ./market_pullback_shadow.py
