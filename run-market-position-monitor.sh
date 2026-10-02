#!/usr/bin/env bash
set -euo pipefail

cd /home/pi/.openclaw/workspace/market-worker
umask 022
export MARKET_CONFIRM_POSITION_BROKERS="${MARKET_CONFIRM_POSITION_BROKERS:-schwab}"
python3 ./market_schwab.py sync || true
python3 ./market_position_monitor.py "$@"
