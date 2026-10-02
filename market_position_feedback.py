#!/usr/bin/env python3
"""Record user trade feedback for market-worker confirmations."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get("MARKET_WORKER_DATA_DIR", str(ROOT.parent / "handoffs" / "market-worker")))
POSITIONS_FILE = DATA_ROOT / "market-position-feedback.json"
TZ = ZoneInfo("America/Chicago")


def now_ct() -> str:
    return dt.datetime.now(TZ).isoformat()


def read_state() -> dict:
    if not POSITIONS_FILE.exists():
        return {"schema": "openclaw.market_worker.position_feedback.v1", "positions": {}, "events": []}
    try:
        data = json.loads(POSITIONS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        backup = POSITIONS_FILE.with_suffix(f".bad-{dt.datetime.now(TZ).strftime('%Y%m%dT%H%M%S')}.json")
        POSITIONS_FILE.replace(backup)
        return {"schema": "openclaw.market_worker.position_feedback.v1", "positions": {}, "events": []}
    data.setdefault("schema", "openclaw.market_worker.position_feedback.v1")
    data.setdefault("positions", {})
    data.setdefault("events", [])
    return data


def write_state(state: dict) -> None:
    tmp = POSITIONS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(POSITIONS_FILE)


def parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    cleaned = value.strip().replace("$", "").replace(",", "")
    if not cleaned:
        return None
    return float(cleaned)


def record_buy(args: argparse.Namespace) -> int:
    state = read_state()
    ticker = args.ticker.upper()
    event = {
        "type": "buy",
        "ticker": ticker,
        "recorded_at": now_ct(),
        "shares": parse_float(args.shares),
        "price": parse_float(args.price),
        "broker": args.broker,
        "note": args.note or "",
    }
    position = {
        "status": "open",
        "ticker": ticker,
        "shares": event["shares"],
        "avg_price": event["price"],
        "broker": args.broker,
        "bought_at": event["recorded_at"],
        "note": event["note"],
    }
    state["positions"][ticker] = position
    state["events"].append(event)
    write_state(state)
    print(f"recorded open position: {ticker}")
    return 0


def record_receive(args: argparse.Namespace) -> int:
    state = read_state()
    ticker = args.ticker.upper()
    event = {
        "type": "receive",
        "reason": args.reason,
        "ticker": ticker,
        "recorded_at": now_ct(),
        "shares": parse_float(args.shares),
        "price": parse_float(args.price),
        "broker": args.broker,
        "note": args.note or "",
    }
    position = {
        "status": "open",
        "ticker": ticker,
        "shares": event["shares"],
        "avg_price": event["price"],
        "broker": args.broker,
        "acquired_at": event["recorded_at"],
        "acquisition_type": args.reason,
        "note": event["note"],
    }
    state["positions"][ticker] = position
    state["events"].append(event)
    write_state(state)
    print(f"recorded open position received: {ticker}")
    return 0


def record_close(args: argparse.Namespace) -> int:
    state = read_state()
    ticker = args.ticker.upper()
    event = {
        "type": "close",
        "ticker": ticker,
        "recorded_at": now_ct(),
        "price": parse_float(args.price),
        "broker": args.broker,
        "note": args.note or "",
    }
    position = state["positions"].setdefault(ticker, {"ticker": ticker})
    position.update(
        {
            "status": "closed",
            "closed_at": event["recorded_at"],
            "close_price": event["price"],
            "broker": position.get("broker") or args.broker,
            "close_note": event["note"],
        }
    )
    state["events"].append(event)
    write_state(state)
    print(f"recorded closed position: {ticker}")
    return 0


def status(_: argparse.Namespace) -> int:
    import market_signal_confirm as confirm

    print(
        json.dumps(
            {
                "manual_positions_file": str(POSITIONS_FILE),
                "robinhood_positions_file": str(confirm.ROBINHOOD_POSITIONS_FILE),
                "schwab_positions_file": str(confirm.SCHWAB_POSITIONS_FILE),
                "open_positions": confirm.read_positions(),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    buy = sub.add_parser("buy", help="record that you bought a ticker")
    buy.add_argument("ticker")
    buy.add_argument("--shares")
    buy.add_argument("--price")
    buy.add_argument("--broker", default="schwab")
    buy.add_argument("--note")
    buy.set_defaults(func=record_buy)

    receive = sub.add_parser("receive", help="record shares received outside a normal buy")
    receive.add_argument("ticker")
    receive.add_argument("--shares", required=True)
    receive.add_argument("--price")
    receive.add_argument("--reason", default="dividend")
    receive.add_argument("--broker", default="schwab")
    receive.add_argument("--note")
    receive.set_defaults(func=record_receive)

    close = sub.add_parser("close", help="record that you exited a ticker")
    close.add_argument("ticker")
    close.add_argument("--price")
    close.add_argument("--broker", default="schwab")
    close.add_argument("--note")
    close.set_defaults(func=record_close)

    stat = sub.add_parser("status", help="show current open positions")
    stat.set_defaults(func=status)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
