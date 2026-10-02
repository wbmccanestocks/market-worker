#!/usr/bin/env python3
"""Monitor open market-worker positions for sell/hold state changes."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
from pathlib import Path

import market_signal_confirm as confirm


ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get("MARKET_WORKER_DATA_DIR", str(ROOT.parent / "handoffs" / "market-worker")))
OUT_DIR = DATA_ROOT / "position-monitor"
STATE_FILE = DATA_ROOT / "market-position-monitor-state.json"
TZ = confirm.TZ
OPENCLAW_CLI = os.environ.get("OPENCLAW_CLI", "/home/pi/.npm-global/bin/openclaw")
TELEGRAM_TARGET = os.environ.get("MARKET_POSITION_TELEGRAM_TARGET", confirm.TELEGRAM_TARGET)
TELEGRAM_ACCOUNT = os.environ.get("MARKET_TELEGRAM_ACCOUNT", confirm.TELEGRAM_ACCOUNT)
NOTIFY_TELEGRAM = os.environ.get("MARKET_POSITION_NOTIFY_TELEGRAM", "1").lower() not in {"0", "false", "no"}


def now_ct() -> dt.datetime:
    return dt.datetime.now(TZ)


def read_state() -> dict:
    if not STATE_FILE.exists():
        return {"schema": "openclaw.market_worker.position_monitor.v1", "positions": {}}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema": "openclaw.market_worker.position_monitor.v1", "positions": {}}
    data.setdefault("schema", "openclaw.market_worker.position_monitor.v1")
    data.setdefault("positions", {})
    return data


def write_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(STATE_FILE)


def current_window(now: dt.datetime) -> str:
    tod = now.time()
    if tod < dt.time(9, 35):
        return "opening"
    if tod < dt.time(11, 30):
        return "one-hour"
    if tod < dt.time(14, 30):
        return "midday"
    if tod < dt.time(15, 15):
        return "late-day"
    return "after-close"


def fetch_quotes(tickers: list[str], prefer_extended: bool = False) -> dict[str, confirm.Quote]:
    quote_tickers = sorted(set(tickers) | {ctx for ticker in tickers for ctx in confirm.CONTEXT[confirm.TICKER_SECTOR[ticker]]})
    return confirm.fetch_primary_quotes(quote_tickers, prefer_extended=prefer_extended)


def monitor_positions(window: str) -> tuple[list[dict], list[str]]:
    open_positions = confirm.read_positions()
    skipped = []
    selected = sorted(open_positions)
    if not selected:
        return [], skipped

    quotes = fetch_quotes(selected, prefer_extended=window == "after-close")
    packet_hits = confirm.packet_mentions()
    rows: list[dict] = []
    for ticker in selected:
        headlines = confirm.fetch_rss_headlines(ticker)
        score, evidence, blockers = confirm.score_sell_position(
            ticker,
            quotes[ticker],
            quotes,
            packet_hits,
            headlines,
            open_positions[ticker],
            window,
        )
        threshold = confirm.sell_thresholds(window)["score"]
        if score >= threshold and evidence:
            action = "SELL_SIGNAL"
        elif score >= max(2, threshold - 1) and evidence:
            action = "HOLD_WARNING"
        else:
            action = "HOLD"
        entry = confirm.position_entry_price(open_positions[ticker])
        price = quotes[ticker].price
        pnl_pct = ((price - entry) / entry * 100) if price is not None and entry else None
        rows.append(
            {
                "ticker": ticker,
                "action": action,
                "score": score,
                "threshold": threshold,
                "price": price,
                "day_change_pct": quotes[ticker].day_change_pct,
                "entry_price": entry,
                "position_change_pct": pnl_pct,
                "evidence": evidence,
                "blockers": blockers,
                "headlines": headlines[:3],
                "quote_error": quotes[ticker].error,
            }
        )
    rows.sort(key=lambda row: ({"SELL_SIGNAL": 0, "HOLD_WARNING": 1, "HOLD": 2}[row["action"]], -row["score"], row["ticker"]))
    return rows, skipped


def state_changes(rows: list[dict], state: dict) -> list[dict]:
    previous = state.setdefault("positions", {})
    changes = []
    for row in rows:
        prior_action = previous.get(row["ticker"], {}).get("action")
        if row["action"] == "SELL_SIGNAL" and row["action"] != prior_action:
            changes.append(row)
        previous[row["ticker"]] = {
            "action": row["action"],
            "score": row["score"],
            "price": row["price"],
            "updated_at": now_ct().isoformat(),
        }
    open_tickers = {row["ticker"] for row in rows}
    for ticker in list(previous):
        if ticker not in open_tickers:
            previous.pop(ticker, None)
    return changes


def write_report(rows: list[dict], skipped: list[str], window: str, changes: list[dict], force: bool) -> Path | None:
    if not rows and not skipped and not force:
        return None
    if not changes and not force:
        return None
    OUT_DIR.mkdir(exist_ok=True)
    now = now_ct()
    out = OUT_DIR / f"{now.strftime('%Y-%m-%dT%H%M')}-position-monitor-{window}.md"
    lines = [
        "source: market-position-monitor",
        "status: proposed",
        "target: market-scout",
        f"window: {window}",
        f"generated_at: {now.isoformat()}",
        "policy: research alert only; no orders executed",
        "",
        "## changes",
    ]
    if changes:
        lines.extend(f"- {row['action']}: {row['ticker']} score {row['score']}/{row['threshold']}" for row in changes)
    else:
        lines.append("- no alerting state changes")
    if skipped:
        lines.extend(["", "## skipped", *[f"- {ticker}: ticker not mapped to a monitor sector" for ticker in skipped]])
    lines.extend(["", "## positions"])
    for row in rows:
        lines.extend(
            [
                f"### {row['ticker']}",
                f"- action: {row['action']}",
                f"- score: {row['score']}/{row['threshold']}",
                f"- quote: price {row['price'] if row['price'] is not None else 'n/a'}; day {confirm.pct(row['day_change_pct'])}; position {confirm.pct(row['position_change_pct'])}",
            ]
        )
        if row["quote_error"]:
            lines.append(f"- quote_error: {row['quote_error']}")
        if row["evidence"]:
            lines.append("- evidence:")
            lines.extend(f"  - {item}" for item in row["evidence"])
        if row["blockers"]:
            lines.append("- blockers:")
            lines.extend(f"  - {item}" for item in row["blockers"])
        if row["headlines"]:
            lines.append("- headlines:")
            lines.extend(f"  - {item}" for item in row["headlines"])
        lines.append("")
    out.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return out


def notify(changes: list[dict], report: Path | None, dry_run: bool, window: str) -> str:
    if not changes:
        return "no alerting state changes"
    if not NOTIFY_TELEGRAM:
        return "telegram notification disabled"
    summary = "; ".join(
        f"{row['action']} {row['ticker']} score {row['score']}/{row['threshold']} price {row['price'] if row['price'] is not None else 'n/a'}"
        for row in changes[:5]
    )
    label = "AFTER_HOURS" if window == "after-close" else window
    message = f"Market position monitor ({label})\n{summary}"
    if report:
        message += f"\n{report}"
    cmd = [
        OPENCLAW_CLI,
        "message",
        "send",
        "--channel",
        "telegram",
        "--account",
        TELEGRAM_ACCOUNT,
        "--target",
        TELEGRAM_TARGET,
        "--message",
        message,
    ]
    if dry_run:
        cmd.append("--dry-run")
    result = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=30, check=False)
    output = (result.stdout + result.stderr).strip()
    if result.returncode == 0:
        return "telegram notification ok" + (f": {output[:300]}" if output else "")
    return f"telegram notification failed ({result.returncode}): {output[:500]}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--window", choices=["opening", "one-hour", "midday", "late-day", "after-close"])
    parser.add_argument("--write-all", action="store_true", help="write a report even when no alerting state changed")
    parser.add_argument("--dry-run", action="store_true", help="use OpenClaw dry-run notification mode")
    parser.add_argument("--no-notify", action="store_true", help="skip Telegram notification")
    args = parser.parse_args()

    window = args.window or current_window(now_ct())
    rows, skipped = monitor_positions(window)
    state = read_state()
    changes = state_changes(rows, state)
    write_state(state)
    report = write_report(rows, skipped, window, changes, args.write_all)
    print(f"checked {len(rows)} open positions; changes={len(changes)}; skipped={len(skipped)}; window={window}")
    if report:
        print(report)
    if args.no_notify:
        print("telegram notification skipped by --no-notify")
    else:
        print(notify(changes, report, args.dry_run, window))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
