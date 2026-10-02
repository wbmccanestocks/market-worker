#!/usr/bin/env python3
"""Tmpfs-backed dynamic confirmation queue for market-scout signals."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get("MARKET_WORKER_DATA_DIR", str(ROOT.parent / "handoffs" / "market-worker")))
CODE_ROOT = ROOT
TZ = ZoneInfo("America/Chicago")
QUEUE_DIR = Path(os.environ.get("MARKET_SIGNAL_QUEUE_DIR", "/dev/shm/openclaw-market"))
QUEUE_FILE = QUEUE_DIR / "market-signal-queue.json"
LOCK_DIR = QUEUE_DIR / ".poll.lock"
PACKET_GLOB = "*-market-packet-*.md"
CONFIRMATION_GLOB = "confirmations/*-market-confirm-*.md"
SCHEDULED_CONFIRMATION_GRACE = dt.timedelta(minutes=3)
WINDOW_TIMES = {
    "opening": dt.time(8, 35),
    "one-hour": dt.time(9, 35),
    "midday": dt.time(11, 30),
    "late-day": dt.time(14, 30),
    "friday-closeout": dt.time(14, 50),
    "after-close": dt.time(15, 15),
}
WATCHLIST = {
    "NVDA", "MSFT", "AAPL", "AMD", "AVGO", "GOOGL", "GOOG", "META", "TSLA", "TSLL", "TSLS", "TSLZ", "SPCX", "LOFF", "LOFD",
    "AMZN", "LOW", "F", "FB", "QQQ", "SPY", "IWM", "XLK", "SMH", "XOM",
    "CVX", "COP", "OXY", "SLB", "LNG", "ENPH", "FSLR", "XLE", "XOP", "USO",
}
CATALYST_WORDS = {
    "earnings", "guidance", "revenue", "profit", "forecast", "analyst",
    "upgrade", "downgrade", "rating", "price target", "oil", "gas", "opec",
    "inventory", "chips", "ai", "semiconductor", "solar", "energy",
    "data center", "surges", "jumps", "rallies", "premarket", "pre-bell",
}
WINDOW_PATTERNS = [
    ("opening", re.compile(r"\b(pre[- ]?market|pre[- ]?bell|before (?:the )?open|opening|after (?:the )?open)\b", re.I)),
    ("one-hour", re.compile(r"\b(one[- ]?hour|first hour|hour after (?:the )?open|after open)\b", re.I)),
    ("midday", re.compile(r"\b(midday|mid[- ]?day|noon|lunch)\b", re.I)),
    ("late-day", re.compile(r"\b(late[- ]?day|afternoon|power hour|into close|near close)\b", re.I)),
    ("after-close", re.compile(r"\b(after[- ]?close|after hours|post[- ]?market)\b", re.I)),
]


def ensure_queue_dir() -> None:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)


def now_ct() -> dt.datetime:
    return dt.datetime.now(TZ)


def read_queue() -> dict:
    ensure_queue_dir()
    if not QUEUE_FILE.exists():
        return {"version": 1, "jobs": []}
    try:
        return json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        backup = QUEUE_FILE.with_suffix(f".bad-{now_ct().strftime('%Y%m%dT%H%M%S')}.json")
        shutil.move(str(QUEUE_FILE), str(backup))
        return {"version": 1, "jobs": []}


def write_queue(queue: dict) -> None:
    ensure_queue_dir()
    tmp = QUEUE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(queue, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(QUEUE_FILE)


def normalize_queue(queue: dict) -> bool:
    changed = False
    seen_active: set[str] = set()
    for job in queue.get("jobs", []):
        if not job.get("due_at") or not job.get("window"):
            continue
        try:
            due_at = dt.datetime.fromisoformat(job["due_at"])
        except (TypeError, ValueError):
            continue
        window = job["window"]
        if job.get("status") == "pending" and window in WINDOW_TIMES:
            exact_due = dt.datetime.combine(due_at.date(), WINDOW_TIMES[window], TZ)
            if due_at == exact_due:
                due_at = exact_due + SCHEDULED_CONFIRMATION_GRACE
                job["due_at"] = due_at.isoformat()
                changed = True
        dedupe_key = job_dedupe_key(due_at, window, job.get("tickers", []))
        if job.get("dedupe_key") != dedupe_key:
            job["dedupe_key"] = dedupe_key
            changed = True
        if job.get("status") != "pending":
            continue
        if dedupe_key in seen_active:
            job["status"] = "skipped"
            job["completed_at"] = now_ct().isoformat()
            job["last_output"] = "skipped: duplicate pending same-day same-window ticker confirmation"
            job["exit_code"] = 0
            changed = True
        else:
            seen_active.add(dedupe_key)
    return changed


def latest_packets(limit: int = 1) -> list[Path]:
    return sorted(DATA_ROOT.glob(PACKET_GLOB), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]


def packet_json_path(packet: Path) -> Path:
    return packet.with_suffix(".json")


def tickers_in(text: str) -> list[str]:
    upper = text.upper()
    return sorted(ticker for ticker in WATCHLIST if re.search(rf"\b{re.escape(ticker)}\b", upper))


def windows_for(text: str) -> list[str]:
    windows = [window for window, pattern in WINDOW_PATTERNS if pattern.search(text)]
    if "opening" in windows and "one-hour" not in windows:
        windows.append("one-hour")
    return sorted(set(windows), key=lambda w: list(WINDOW_TIMES).index(w))


def due_at_for(window: str, base: dt.datetime) -> dt.datetime | None:
    due = dt.datetime.combine(base.date(), WINDOW_TIMES[window], TZ) + SCHEDULED_CONFIRMATION_GRACE
    if window == "friday-closeout" and base.weekday() != 4:
        return None
    if due <= base:
        return None
    return due


def canonical_tickers(tickers: list[str]) -> tuple[str, ...]:
    return tuple(sorted(set(tickers)))


def job_dedupe_key(due_at: dt.datetime | str, window: str, tickers: list[str]) -> str:
    if isinstance(due_at, str):
        due_date = dt.datetime.fromisoformat(due_at).date().isoformat()
    else:
        due_date = due_at.date().isoformat()
    return f"{due_date}|{window}|{','.join(canonical_tickers(tickers))}"


def same_day_window_report_exists(window: str, tickers: list[str], when: dt.datetime) -> bool:
    day_prefix = when.strftime("%Y-%m-%d")
    wanted = set(canonical_tickers(tickers))
    for report in DATA_ROOT.glob(CONFIRMATION_GLOB):
        if not report.name.startswith(day_prefix) or f"-market-confirm-{window}" not in report.name:
            continue
        try:
            text = report.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        checked = set(re.findall(r"^###\s+([A-Z][A-Z0-9.]*)\s*$", text, flags=re.M))
        if wanted and wanted.issubset(checked):
            return True
    return False


def candidate_snippets(packet_text: str) -> list[str]:
    lines = []
    for line in packet_text.splitlines():
        if not re.search(r"\b(?:snippet|fact):", line, flags=re.I):
            continue
        compact = re.sub(r"\s+", " ", line).strip()
        if tickers_in(compact) and any(word in compact.lower() for word in CATALYST_WORDS):
            lines.append(compact[:650])
    return lines


def candidate_snippets_from_json(packet: Path) -> list[str] | None:
    json_path = packet_json_path(packet)
    if not json_path.exists():
        return None
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    lines: list[str] = []
    for item in data.get("source_starting_points", []):
        source = item.get("final_url") or item.get("url") or ""
        for snippet in item.get("snippets", []):
            compact = re.sub(r"\s+", " ", str(snippet)).strip()
            if tickers_in(compact) and any(word in compact.lower() for word in CATALYST_WORDS):
                lines.append(f"snippet: {compact[:650]} source: {source}")

    extracted = data.get("extracted_facts", {})
    if isinstance(extracted, dict):
        for fact in extracted.get("fallback_facts", []):
            text = fact.get("fact", "")
            compact = re.sub(r"\s+", " ", str(text)).strip()
            if tickers_in(compact) and any(word in compact.lower() for word in CATALYST_WORDS):
                lines.append(f"fact: {compact[:650]} source: {fact.get('source', '')}")
        extraction_text = str(extracted.get("extraction_text") or extracted.get("ollama_text") or "")
        for line in extraction_text.splitlines():
            compact = re.sub(r"\s+", " ", line).strip()
            if tickers_in(compact) and any(word in compact.lower() for word in CATALYST_WORDS):
                lines.append(f"fact: {compact[:650]}")

    return lines


def job_id(source_packet: Path, window: str, tickers: list[str], thesis: str) -> str:
    digest = hashlib.sha1(
        f"{source_packet.name}|{window}|{','.join(tickers)}|{thesis[:220]}".encode("utf-8")
    ).hexdigest()[:12]
    return f"{window}-{','.join(tickers)}-{digest}".replace(",", "-")


def enqueue_from_latest(limit: int = 1) -> int:
    queue = read_queue()
    if normalize_queue(queue):
        write_queue(queue)
    existing = {job.get("id") for job in queue.get("jobs", [])}
    existing_dedupe = {
        job.get("dedupe_key") or job_dedupe_key(job.get("due_at"), job.get("window", ""), job.get("tickers", []))
        for job in queue.get("jobs", [])
        if job.get("due_at") and job.get("window")
    }
    created = 0
    skipped = 0
    base = now_ct()
    for packet in latest_packets(limit):
        snippets = candidate_snippets_from_json(packet)
        if snippets is None:
            text = packet.read_text(encoding="utf-8", errors="replace")
            snippets = candidate_snippets(text)
        for snippet in snippets:
            tickers = tickers_in(snippet)
            windows = windows_for(snippet)
            if not windows:
                continue
            for window in windows:
                due_at = due_at_for(window, base)
                if due_at is None:
                    continue
                dedupe_key = job_dedupe_key(due_at, window, tickers)
                if dedupe_key in existing_dedupe:
                    skipped += 1
                    continue
                if same_day_window_report_exists(window, tickers, due_at):
                    skipped += 1
                    continue
                jid = job_id(packet, window, tickers, snippet)
                if jid in existing:
                    skipped += 1
                    continue
                queue.setdefault("jobs", []).append(
                    {
                        "id": jid,
                        "dedupe_key": dedupe_key,
                        "status": "pending",
                        "created_at": base.isoformat(),
                        "due_at": due_at.isoformat(),
                        "window": window,
                        "tickers": tickers,
                        "thesis": snippet,
                        "source_packet": packet.name,
                        "attempts": 0,
                    }
                )
                existing.add(jid)
                existing_dedupe.add(dedupe_key)
                created += 1
    write_queue(queue)
    print(f"enqueued {created} jobs; skipped {skipped} duplicates; queue={QUEUE_FILE}")
    return 0


def acquire_lock() -> bool:
    ensure_queue_dir()
    try:
        LOCK_DIR.mkdir()
        return True
    except FileExistsError:
        return False


def release_lock() -> None:
    try:
        LOCK_DIR.rmdir()
    except OSError:
        pass


def run_job(job: dict) -> tuple[int, str]:
    refresh_output = "Robinhood position refresh disabled before dynamic confirmation\n"
    try:
        refresh = subprocess.run(
            [sys.executable, str(CODE_ROOT / "market_schwab.py"), "sync"],
            cwd=CODE_ROOT,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        refresh_output += refresh.stdout + refresh.stderr
    except Exception as exc:  # noqa: BLE001 - stale snapshot is safer than blocking confirmation.
        refresh_output += f"Schwab position refresh failed before dynamic confirmation: {type(exc).__name__}: {exc}\n"
    cmd = [
        sys.executable,
        str(CODE_ROOT / "market_signal_confirm.py"),
        "--window",
        job["window"],
        "--tickers",
        ",".join(job["tickers"]),
        "--job-id",
        job["id"],
        "--job-thesis",
        job["thesis"],
        "--source-packet",
        job["source_packet"],
    ]
    result = subprocess.run(cmd, cwd=CODE_ROOT, text=True, capture_output=True, timeout=180, check=False)
    output = (refresh_output + result.stdout + result.stderr).strip()
    return result.returncode, output[-2000:]


def poll(max_jobs: int = 3) -> int:
    if not acquire_lock():
        print("poll already running")
        return 0
    try:
        queue = read_queue()
        if normalize_queue(queue):
            write_queue(queue)
        base = now_ct()
        ran = 0
        for job in queue.get("jobs", []):
            if ran >= max_jobs or job.get("status") != "pending":
                continue
            due_at = dt.datetime.fromisoformat(job["due_at"])
            if due_at > base:
                continue
            if same_day_window_report_exists(job["window"], job.get("tickers", []), due_at):
                job["status"] = "skipped"
                job["completed_at"] = now_ct().isoformat()
                job["last_output"] = "skipped: same-day same-window confirmation report already checked these tickers"
                job["exit_code"] = 0
                write_queue(queue)
                continue
            job["status"] = "running"
            job["started_at"] = base.isoformat()
            job["attempts"] = int(job.get("attempts", 0)) + 1
            write_queue(queue)
            code, output = run_job(job)
            finished = now_ct()
            job["completed_at"] = finished.isoformat()
            job["last_output"] = output
            job["status"] = "done" if code == 0 else "error"
            job["exit_code"] = code
            ran += 1
        write_queue(queue)
        print(f"ran {ran} due jobs; queue={QUEUE_FILE}")
        return 0
    finally:
        release_lock()


def status() -> int:
    queue = read_queue()
    if normalize_queue(queue):
        write_queue(queue)
    jobs = queue.get("jobs", [])
    counts: dict[str, int] = {}
    for job in jobs:
        counts[job.get("status", "unknown")] = counts.get(job.get("status", "unknown"), 0) + 1
    print(json.dumps({"queue_file": str(QUEUE_FILE), "jobs": len(jobs), "counts": counts}, indent=2, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    enqueue = sub.add_parser("enqueue-from-latest")
    enqueue.add_argument("--limit", type=int, default=1)
    poll_parser = sub.add_parser("poll")
    poll_parser.add_argument("--max-jobs", type=int, default=3)
    sub.add_parser("status")
    args = parser.parse_args()
    if args.command == "enqueue-from-latest":
        return enqueue_from_latest(args.limit)
    if args.command == "poll":
        return poll(args.max_jobs)
    if args.command == "status":
        return status()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
