#!/usr/bin/env bash
set -euo pipefail

cd /home/pi/.openclaw/workspace/market-worker
umask 022
exec 9>/dev/shm/openclaw-market-schwab-keepalive.lock
flock -n 9 || {
  echo "market Schwab keepalive already running; skipped overlapping run"
  exit 0
}

python3 ./market_schwab.py keepalive
