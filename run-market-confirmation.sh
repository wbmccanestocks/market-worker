#!/usr/bin/env bash
set -euo pipefail

window="${1:-auto}"
cd /home/pi/.openclaw/workspace/market-worker
umask 022
exec 9>/dev/shm/openclaw-market-confirmation.lock
flock -n 9 || {
  echo "market confirmation already running; skipped overlapping run"
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

# Schwab is the active brokerage.  A caller can still deliberately override
# this for a manual diagnostic, but scheduled monitoring is Schwab-first.
export MARKET_CONFIRM_POSITION_BROKERS="${MARKET_CONFIRM_POSITION_BROKERS:-schwab}"
# Schwab's current stock-buying-power value is the funding authority for this
# account. It includes sale proceeds that Schwab makes available for another
# purchase, so do not substitute a separate settled-cash rule.
export MARKET_CONFIRM_SCHWAB_BALANCE_GATE="${MARKET_CONFIRM_SCHWAB_BALANCE_GATE:-1}"
# Momentum-chasing aggressive BUYs are paper-only while the pullback shadow
# report establishes an out-of-sample record.
export MARKET_CONFIRM_AGGRESSIVE_BUY_ENABLED="${MARKET_CONFIRM_AGGRESSIVE_BUY_ENABLED:-0}"
python3 ./market_schwab.py sync || true
timeout --kill-after=30s 20m python3 ./market_signal_confirm.py --window "$window"
