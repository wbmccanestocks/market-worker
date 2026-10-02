#!/usr/bin/env bash
set -euo pipefail

cd /home/pi/.openclaw/workspace/market-worker
umask 022
export MARKET_SIGNAL_QUEUE_DIR="${MARKET_SIGNAL_QUEUE_DIR:-/dev/shm/openclaw-market}"
mkdir -p "$MARKET_SIGNAL_QUEUE_DIR"
python3 ./market_signal_queue.py enqueue-from-latest --limit "${MARKET_SIGNAL_QUEUE_PACKET_LIMIT:-1}"
