#!/usr/bin/env bash
set -euo pipefail

cd /home/pi/.openclaw/workspace/market-worker
umask 022
export MARKET_SIGNAL_QUEUE_DIR="${MARKET_SIGNAL_QUEUE_DIR:-/dev/shm/openclaw-market}"
export MARKET_CONFIRM_POSITION_BROKERS="${MARKET_CONFIRM_POSITION_BROKERS:-schwab}"
mkdir -p "$MARKET_SIGNAL_QUEUE_DIR"
python3 ./market_signal_queue.py poll --max-jobs "${MARKET_SIGNAL_QUEUE_MAX_JOBS:-3}"
