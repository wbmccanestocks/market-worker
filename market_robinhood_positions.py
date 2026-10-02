#!/usr/bin/env python3
"""Refresh read-only Robinhood equity holdings for market-worker."""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import subprocess
import urllib.parse
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get("MARKET_WORKER_DATA_DIR", str(ROOT.parent / "handoffs" / "market-worker")))
OUT_FILE = DATA_ROOT / "market-robinhood-positions.json"
RH_CLIENT = Path(
    os.environ.get(
        "MARKET_WORKER_ROBINHOOD_CLIENT",
        str(ROOT.parent / "skills" / "robinhood-agentic" / "rh-client.mjs"),
    )
)
TIMEOUT = int(os.environ.get("MARKET_WORKER_ROBINHOOD_TIMEOUT", "30"))
TZ = ZoneInfo("America/Chicago")


def call(tool_name: str, args: dict) -> dict:
    result = subprocess.run(
        ["node", str(RH_CLIENT), "call", tool_name, json.dumps(args)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise RuntimeError(f"{tool_name} failed: {detail[:300]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{tool_name} returned invalid JSON: {result.stdout[:200]}") from exc


def number(value: object) -> float | None:
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def account_label(account: dict) -> str:
    if account.get("nickname"):
        return str(account["nickname"])[:80]
    label = str(account.get("brokerage_account_type") or "brokerage")
    if account.get("is_default"):
        label += " default"
    elif account.get("agentic_allowed"):
        label += " agentic"
    return label[:80]


def account_positions(account: dict) -> list[dict]:
    positions: list[dict] = []
    cursor = None
    while True:
        args = {"account_number": account["account_number"]}
        if cursor:
            args["cursor"] = cursor
        response = call("get_equity_positions", args)
        data = response.get("data") or {}
        positions.extend(row for row in (data.get("positions") or []) if isinstance(row, dict))
        next_url = str(data.get("next") or "")
        cursor = urllib.parse.parse_qs(urllib.parse.urlparse(next_url).query).get("cursor", [None])[0]
        if not cursor:
            return positions


def account_portfolio(account: dict) -> dict | None:
    try:
        response = call("get_portfolio", {"account_number": account["account_number"]})
    except RuntimeError:
        return None
    data = response.get("data")
    return data if isinstance(data, dict) else None


def portfolio_buying_power(portfolio: dict | None) -> float | None:
    if not isinstance(portfolio, dict):
        return None
    buying_power = portfolio.get("buying_power")
    if isinstance(buying_power, dict):
        parsed = number(buying_power.get("buying_power"))
        if parsed is not None:
            return parsed
    return number(portfolio.get("buying_power")) or number(portfolio.get("cash"))


def merge_position(existing: dict | None, row: dict, label: str) -> dict | None:
    ticker = str(row.get("symbol") or "").upper().strip()
    shares = number(row.get("quantity"))
    if not ticker or shares is None or shares == 0:
        return existing
    avg_price = number(row.get("average_buy_price"))
    if existing is None:
        return {
            "status": "open",
            "ticker": ticker,
            "shares": shares,
            "avg_price": avg_price,
            "position_type": str(row.get("type") or "long"),
            "broker": "robinhood",
            "accounts": [label],
        }

    prior_shares = number(existing.get("shares")) or 0.0
    prior_price = number(existing.get("avg_price"))
    total_shares = prior_shares + shares
    if prior_price is not None and avg_price is not None and total_shares != 0:
        existing["avg_price"] = ((prior_shares * prior_price) + (shares * avg_price)) / total_shares
    elif prior_price is None:
        existing["avg_price"] = avg_price
    existing["shares"] = total_shares
    existing["accounts"] = sorted(set(existing.get("accounts", [])) | {label})
    return existing


def refresh() -> dict:
    accounts_response = call("get_accounts", {})
    accounts = [
        account
        for account in ((accounts_response.get("data") or {}).get("accounts") or [])
        if isinstance(account, dict)
        and account.get("state") == "active"
        and not account.get("deactivated")
        and not account.get("permanently_deactivated")
    ]

    positions: dict[str, dict] = {}
    labels = []
    account_snapshots = []
    for account in accounts:
        label = account_label(account)
        labels.append(label)
        portfolio = account_portfolio(account)
        if portfolio is not None:
            account_snapshots.append(
                {
                    "label": label,
                    "agentic_allowed": bool(account.get("agentic_allowed")),
                    "cash": number(portfolio.get("cash")),
                    "buying_power": portfolio_buying_power(portfolio),
                    "currency": str(portfolio.get("currency") or "USD"),
                }
            )
        for row in account_positions(account):
            ticker = str(row.get("symbol") or "").upper().strip()
            merged = merge_position(positions.get(ticker), row, label)
            if merged:
                positions[ticker] = merged

    agentic_snapshots = [row for row in account_snapshots if row.get("agentic_allowed")]
    spending_power_values = [
        row["buying_power"]
        for row in agentic_snapshots or account_snapshots
        if isinstance(row.get("buying_power"), (int, float))
    ]
    spending_power = min(spending_power_values) if spending_power_values else None

    return {
        "schema": "openclaw.market_worker.robinhood_positions.v1",
        "source": "robinhood-agentic",
        "read_only_tools_used": ["get_accounts", "get_equity_positions", "get_portfolio"],
        "refreshed_at": dt.datetime.now(TZ).isoformat(),
        "accounts_checked": sorted(set(labels)),
        "accounts": account_snapshots,
        "spending_power": spending_power,
        "spending_power_currency": "USD",
        "positions": positions,
    }


def main() -> int:
    if not RH_CLIENT.exists():
        raise SystemExit(f"missing Robinhood client: {RH_CLIENT}")
    try:
        state = refresh()
    except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
        print(f"Robinhood position refresh failed; preserving prior snapshot: {type(exc).__name__}: {exc}")
        return 1

    tmp = OUT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(OUT_FILE)
    os.chmod(OUT_FILE, 0o600)
    print(f"refreshed {len(state['positions'])} Robinhood equity positions across {len(state['accounts_checked'])} accounts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
