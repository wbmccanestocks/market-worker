#!/usr/bin/env bash
set -euo pipefail

window="${1:-auto}"
cd /home/pi/.openclaw/workspace/market-worker
umask 022
exec 9>/dev/shm/openclaw-market-schwab-aggressive-confirmation.lock
flock -n 9 || {
  echo "market Schwab aggressive confirmation already running; skipped overlapping run"
  exit 0
}

if [[ "$window" == "auto" ]]; then
  hhmm="$(TZ=America/Chicago date +%H%M)"
  if (( 10#$hhmm < 940 )); then
    window="opening"
  elif (( 10#$hhmm < 1140 )); then
    window="one-hour"
  elif (( 10#$hhmm < 1440 )); then
    window="midday"
  elif (( 10#$hhmm < 1510 )); then
    window="late-day"
  else
    window="after-close"
  fi
fi

python3 ./market_schwab.py sync || true

export MARKET_CONFIRM_PROFILE="${MARKET_CONFIRM_PROFILE:-schwab-aggressive}"
export MARKET_CONFIRM_OUT_DIR="${MARKET_CONFIRM_OUT_DIR:-/home/pi/.openclaw/workspace/handoffs/market-worker/confirmations-schwab-aggressive}"
export MARKET_CONFIRM_RECOMMENDATION_STATE_FILE="${MARKET_CONFIRM_RECOMMENDATION_STATE_FILE:-/home/pi/.openclaw/workspace/handoffs/market-worker/market-recommendation-state-schwab-aggressive.json}"
export MARKET_CONFIRM_POSITION_BROKERS="${MARKET_CONFIRM_POSITION_BROKERS:-schwab}"
export MARKET_CONFIRM_SCHWAB_BALANCE_GATE="${MARKET_CONFIRM_SCHWAB_BALANCE_GATE:-1}"
export MARKET_CONFIRM_AGGRESSIVE_BUY_ENABLED="${MARKET_CONFIRM_AGGRESSIVE_BUY_ENABLED:-0}"
export MARKET_CONFIRM_CASH_ONLY_NO_MARGIN="${MARKET_CONFIRM_CASH_ONLY_NO_MARGIN:-0}"
export MARKET_CONFIRM_WHOLE_SHARE_PROTECTIVE_STOPS_ONLY="${MARKET_CONFIRM_WHOLE_SHARE_PROTECTIVE_STOPS_ONLY:-1}"
export MARKET_CONFIRM_MAX_WHOLE_SHARE_TRADE_VALUE="${MARKET_CONFIRM_MAX_WHOLE_SHARE_TRADE_VALUE:-250}"
export MARKET_CONFIRM_MIN_BUY_PRICE="${MARKET_CONFIRM_MIN_BUY_PRICE:-0.50}"
export MARKET_CONFIRM_MIN_BUY_AVG_DOLLAR_VOLUME="${MARKET_CONFIRM_MIN_BUY_AVG_DOLLAR_VOLUME:-250000}"
export MARKET_CONFIRM_MIN_BUY_AVG_VOLUME="${MARKET_CONFIRM_MIN_BUY_AVG_VOLUME:-500000}"
export MARKET_CONFIRM_MIN_BUY_CURRENT_VOLUME="${MARKET_CONFIRM_MIN_BUY_CURRENT_VOLUME:-25000}"
export MARKET_CONFIRM_MIN_OPENING_BUY_CURRENT_VOLUME="${MARKET_CONFIRM_MIN_OPENING_BUY_CURRENT_VOLUME:-15000}"
export MARKET_CONFIRM_AGGRESSIVE_BUY_MIN_CHANGE="${MARKET_CONFIRM_AGGRESSIVE_BUY_MIN_CHANGE:-1.5}"
export MARKET_CONFIRM_AGGRESSIVE_BUY_MAX_CHANGE="${MARKET_CONFIRM_AGGRESSIVE_BUY_MAX_CHANGE:-12.0}"
export MARKET_CONFIRM_AGGRESSIVE_BUY_MIN_VOLUME_RATIO="${MARKET_CONFIRM_AGGRESSIVE_BUY_MIN_VOLUME_RATIO:-0.45}"
export MARKET_CONFIRM_NEWS_CANDIDATE_LIMIT="${MARKET_CONFIRM_NEWS_CANDIDATE_LIMIT:-80}"

timeout --kill-after=30s 20m python3 ./market_signal_confirm.py --window "$window"
