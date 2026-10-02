#!/usr/bin/env bash
set -euo pipefail

cd /home/pi/.openclaw/workspace/market-worker
umask 022
exec 9>/dev/shm/openclaw-market-worker.lock
flock -n 9 || {
  echo "market-worker already running; skipped overlapping run"
  exit 0
}
export OLLAMA_ENDPOINT="${OLLAMA_ENDPOINT:-http://brian-pc.local:11434/api/generate}"
export OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5:64k}"
# Luna cheaply triages raw feeds; Terra performs evidence extraction only on
# retained corporate tech/energy items. Enable after host-managed Codex auth is
# available; Ollama remains the fail-open default in the meantime.
export MARKET_WORKER_CODEX="${MARKET_WORKER_CODEX:-0}"
export MARKET_WORKER_LUNA_MODEL="${MARKET_WORKER_LUNA_MODEL:-gpt-5.6-luna}"
export MARKET_WORKER_TERRA_MODEL="${MARKET_WORKER_TERRA_MODEL:-gpt-5.6-terra}"
export MARKET_WORKER_LUNA_FILTER="${MARKET_WORKER_LUNA_FILTER:-1}"
python3 ./market_schwab.py sync || true
python3 ./market_worker_cron.py
python3 ./market_signal_queue.py enqueue-from-latest --limit 1
