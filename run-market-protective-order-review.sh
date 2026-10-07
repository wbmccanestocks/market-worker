#!/usr/bin/env bash
set -euo pipefail

cd /home/pi/.openclaw/workspace/market-worker
umask 022
exec 9>/dev/shm/openclaw-market-protective-order-review.lock
flock -n 9 || {
  echo "market protective order review already running; skipped overlapping run"
  exit 0
}

export MARKET_CONFIRM_POSITION_BROKERS="${MARKET_CONFIRM_POSITION_BROKERS:-schwab}"
# Automatic sells are enabled only for profitable, eligible SELL/EXIT rows. Set
# MARKET_SCHWAB_DISABLE_AUTOMATIC_SELLS=1 for an emergency stop.
export MARKET_SCHWAB_ENABLE_ORDER_SUBMISSION="${MARKET_SCHWAB_ENABLE_ORDER_SUBMISSION:-1}"
export MARKET_SCHWAB_ENABLE_AUTOMATIC_SELLS="${MARKET_SCHWAB_ENABLE_AUTOMATIC_SELLS:-1}"
python3 ./market_schwab.py sync || true
python3 ./market_protective_order_review.py "$@"
