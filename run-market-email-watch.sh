#!/usr/bin/env bash
set -euo pipefail

cd /home/pi/.openclaw/workspace/market-worker
umask 022

GWS="${GWS:-/home/pi/.npm-global/bin/gws}"
PROJECT="${GWS_PROJECT:-openclaw-integration-496522}"
HANDOFF_DIR="${MARKET_HANDOFF_DIR:-/home/pi/.openclaw/workspace/handoffs/market-worker}"
OUT_DIR="${MARKET_EMAIL_OUT_DIR:-$HANDOFF_DIR/email-inbox}"
POLL_INTERVAL="${MARKET_EMAIL_POLL_INTERVAL:-5}"
PROCESS_INTERVAL="${MARKET_EMAIL_PROCESS_INTERVAL:-5}"
LOG_DIR="${MARKET_EMAIL_LOG_DIR:-$HANDOFF_DIR/email-logs}"

mkdir -p "$OUT_DIR" "$LOG_DIR"

"$GWS" gmail +watch \
  --project "$PROJECT" \
  --label-ids INBOX \
  --poll-interval "$POLL_INTERVAL" \
  --msg-format full \
  --output-dir "$OUT_DIR" \
  >"$LOG_DIR/gws-watch.ndjson" \
  2>"$LOG_DIR/gws-watch.err" &
watch_pid=$!

cleanup() {
  kill "$watch_pid" 2>/dev/null || true
  wait "$watch_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

while kill -0 "$watch_pid" 2>/dev/null; do
  python3 ./market_email_processor.py || true
  sleep "$PROCESS_INTERVAL"
done

wait "$watch_pid"
