#!/usr/bin/env python3
"""Refresh read-only Robinhood research data for market-worker.

The snapshot intentionally avoids account numbers and never calls write/trade
tools. It gives the packet generator and confirmer a single local source for
Robinhood scanners, earnings, fundamentals, technical indicators, options
context, and aggregate realized PnL.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import subprocess
import urllib.parse
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get("MARKET_WORKER_DATA_DIR", str(ROOT.parent / "handoffs" / "market-worker")))
OUT_FILE = DATA_ROOT / "market-robinhood-research.json"
RH_CLIENT = Path(
    os.environ.get(
        "MARKET_WORKER_ROBINHOOD_CLIENT",
        str(ROOT.parent / "skills" / "robinhood-agentic" / "rh-client.mjs"),
    )
)
TIMEOUT = int(os.environ.get("MARKET_WORKER_ROBINHOOD_RESEARCH_TIMEOUT", "45"))
MAX_SYMBOLS = int(os.environ.get("MARKET_WORKER_ROBINHOOD_RESEARCH_SYMBOLS", "80"))
MAX_SCANS = int(os.environ.get("MARKET_WORKER_ROBINHOOD_SCANS", "8"))
MAX_SCAN_RESULTS = int(os.environ.get("MARKET_WORKER_ROBINHOOD_SCAN_RESULTS", "40"))
MAX_EARNINGS_CALENDAR = int(os.environ.get("MARKET_WORKER_ROBINHOOD_EARNINGS_CALENDAR", "300"))
TECHNICAL_TYPES = [
    item.strip()
    for item in os.environ.get("MARKET_WORKER_ROBINHOOD_TECHNICALS", "rsi,macd,atr").split(",")
    if item.strip()
]
TZ = ZoneInfo("America/Chicago")


def now_ct() -> dt.datetime:
    return dt.datetime.now(TZ)


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def call(tool_name: str, args: dict) -> dict:
    completed = subprocess.run(
        ["node", str(RH_CLIENT), "call", tool_name, json.dumps(args)],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
        raise RuntimeError(f"{tool_name} failed: {detail[:400]}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{tool_name} returned invalid JSON: {completed.stdout[:200]}") from exc


def number(value: object) -> float | None:
    try:
        parsed = float(str(value).replace("$", "").replace(",", ""))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def parse_cursor(next_url: object) -> str | None:
    text = str(next_url or "")
    if not text:
        return None
    return urllib.parse.parse_qs(urllib.parse.urlparse(text).query).get("cursor", [None])[0]


def symbol_set(symbols: list[str]) -> list[str]:
    return sorted(
        {
            symbol.upper().strip()
            for symbol in symbols
            if re.fullmatch(r"[A-Z0-9.-]{1,15}", symbol.upper().strip())
        }
    )[:MAX_SYMBOLS]


def chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def collect_scans(gaps: list[str], tools: set[str]) -> dict:
    try:
        response = call("get_scans", {})
        tools.add("get_scans")
    except Exception as exc:  # noqa: BLE001
        gaps.append(f"get_scans unavailable: {type(exc).__name__}: {exc}")
        return {"scans": [], "candidates": {}}

    scans = (response.get("data") or {}).get("scans") or []
    out_scans = []
    candidates: dict[str, dict] = {}
    for scan in scans[:MAX_SCANS]:
        if not isinstance(scan, dict):
            continue
        scan_id = str(scan.get("scan_id") or scan.get("id") or "")
        title = str(scan.get("scan_title") or scan.get("title") or "Untitled scan")[:120]
        if not scan_id:
            continue
        try:
            run = call("run_scan", {"scan_id": scan_id})
            tools.add("run_scan")
            result = (run.get("data") or {}).get("result") or {}
            rows = result.get("results") or []
        except Exception as exc:  # noqa: BLE001
            gaps.append(f"run_scan {title} unavailable: {type(exc).__name__}: {exc}")
            rows = []
        selected_rows = []
        for row in rows[:MAX_SCAN_RESULTS]:
            if not isinstance(row, dict):
                continue
            ticker = str(row.get("ticker") or "").upper().strip()
            if not re.fullmatch(r"[A-Z0-9.-]{1,15}", ticker):
                continue
            columns = row.get("columns") if isinstance(row.get("columns"), dict) else {}
            selected_rows.append({"ticker": ticker, "columns": dict(list(columns.items())[:8])})
            entry = candidates.setdefault(ticker, {"scan_titles": [], "columns": {}})
            entry["scan_titles"].append(title)
            if not entry["columns"]:
                entry["columns"] = dict(list(columns.items())[:8])
        out_scans.append(
            {
                "id": scan_id,
                "title": title,
                "total_items": scan.get("total_items"),
                "sorted_by": scan.get("sorted_by"),
                "cortex_managed": bool(scan.get("cortex_managed")),
                "results": selected_rows,
            }
        )
    return {"scans": out_scans, "candidates": candidates}


def collect_earnings(symbols: list[str], gaps: list[str], tools: set[str]) -> dict:
    out = {"calendar": [], "by_symbol": {}}
    today = now_ct().date().isoformat()
    try:
        response = call("get_earnings_calendar", {"start_date": today, "days": 14, "filter": "high_market_cap"})
        tools.add("get_earnings_calendar")
        out["calendar"] = ((response.get("data") or {}).get("results") or [])[:MAX_EARNINGS_CALENDAR]
    except Exception as exc:  # noqa: BLE001
        gaps.append(f"get_earnings_calendar unavailable: {type(exc).__name__}: {exc}")

    for symbol in symbols:
        try:
            response = call("get_earnings_results", {"symbol": symbol})
            tools.add("get_earnings_results")
            out["by_symbol"][symbol] = (response.get("data") or {}).get("results") or []
        except Exception as exc:  # noqa: BLE001
            gaps.append(f"{symbol} earnings unavailable: {type(exc).__name__}: {exc}")
    return out


def collect_fundamentals(symbols: list[str], gaps: list[str], tools: set[str]) -> dict:
    out: dict[str, dict] = {}
    for chunk in chunks(symbols, 10):
        try:
            response = call("get_equity_fundamentals", {"symbols": chunk, "bounds": "regular"})
            tools.add("get_equity_fundamentals")
            for row in (response.get("data") or {}).get("results") or []:
                if isinstance(row, dict):
                    symbol = str(row.get("symbol") or "").upper()
                    if symbol:
                        out[symbol] = row
        except Exception as exc:  # noqa: BLE001
            gaps.append(f"fundamentals {','.join(chunk)} unavailable: {type(exc).__name__}: {exc}")
    return out


def collect_financials(symbols: list[str], gaps: list[str], tools: set[str]) -> dict:
    out: dict[str, list[dict]] = {}
    for chunk in chunks(symbols, 20):
        try:
            response = call("get_financials", {"symbols": chunk, "period": "quarterly", "limit": 4})
            tools.add("get_financials")
            for row in (response.get("data") or {}).get("results") or []:
                if isinstance(row, dict):
                    symbol = str(row.get("symbol") or "").upper()
                    if symbol:
                        out.setdefault(symbol, []).append(row)
        except Exception as exc:  # noqa: BLE001
            gaps.append(f"financials {','.join(chunk)} unavailable: {type(exc).__name__}: {exc}")
    return out


def latest_indicator_value(payload: dict) -> object:
    indicators = (payload.get("data") or {}).get("indicators") or []
    if not indicators:
        return None
    series = indicators[0].get("series") if isinstance(indicators[0], dict) else None
    if not series:
        return None
    latest = series[-1]
    if not isinstance(latest, dict):
        return None
    if "value" in latest:
        return latest.get("value")
    return {key: value for key, value in latest.items() if key != "begins_at"}


def collect_technicals(symbols: list[str], gaps: list[str], tools: set[str]) -> dict:
    out: dict[str, dict] = {}
    start = (now_utc() - dt.timedelta(days=180)).isoformat().replace("+00:00", "Z")
    for symbol in symbols:
        row = {}
        for indicator in TECHNICAL_TYPES:
            args = {
                "symbol": symbol,
                "type": indicator,
                "interval": "day",
                "start_time": start,
                "bounds": "regular",
                "adjustment_type": "split",
                "output": "latest",
            }
            if indicator in {"rsi", "atr"}:
                args["period"] = 14
            try:
                response = call("get_equity_technical_indicators", args)
                tools.add("get_equity_technical_indicators")
                row[indicator] = latest_indicator_value(response)
            except Exception as exc:  # noqa: BLE001
                gaps.append(f"{symbol} {indicator} unavailable: {type(exc).__name__}: {exc}")
        if row:
            out[symbol] = row
    return out


def active_accounts(gaps: list[str], tools: set[str]) -> list[dict]:
    try:
        response = call("get_accounts", {})
        tools.add("get_accounts")
    except Exception as exc:  # noqa: BLE001
        gaps.append(f"get_accounts unavailable for account research: {type(exc).__name__}: {exc}")
        return []
    return [
        account
        for account in ((response.get("data") or {}).get("accounts") or [])
        if isinstance(account, dict)
        and account.get("state") == "active"
        and not account.get("deactivated")
        and not account.get("permanently_deactivated")
    ]


def summarize_tax_lots(lots: list[dict]) -> dict:
    quantity = 0.0
    short_term = 0.0
    long_term = 0.0
    for lot in lots:
        if not isinstance(lot, dict):
            continue
        qty = number(lot.get("quantity_available") or lot.get("quantity") or lot.get("remaining_quantity")) or 0.0
        quantity += qty
        term = str(lot.get("term") or lot.get("holding_period") or lot.get("tax_lot_term") or "").lower()
        if "long" in term:
            long_term += qty
        elif "short" in term:
            short_term += qty
    return {
        "lot_count": len(lots),
        "quantity": quantity,
        "short_term_quantity": short_term or None,
        "long_term_quantity": long_term or None,
    }


def account_equity_positions(account_number: str, gaps: list[str], tools: set[str]) -> list[dict]:
    positions: list[dict] = []
    cursor = None
    while True:
        args = {"account_number": account_number}
        if cursor:
            args["cursor"] = cursor
        try:
            response = call("get_equity_positions", args)
            tools.add("get_equity_positions")
        except Exception as exc:  # noqa: BLE001
            gaps.append(f"equity positions unavailable for tax-lot context: {type(exc).__name__}: {exc}")
            return positions
        data = response.get("data") or {}
        positions.extend(row for row in (data.get("positions") or []) if isinstance(row, dict))
        cursor = parse_cursor(data.get("next"))
        if not cursor:
            return positions


def collect_account_research(gaps: list[str], tools: set[str]) -> dict:
    accounts = active_accounts(gaps, tools)
    out = {"accounts_checked": len(accounts), "option_positions": [], "option_watchlist": [], "realized_pnl": [], "equity_tax_lots": {}}
    try:
        response = call("get_option_watchlist", {})
        tools.add("get_option_watchlist")
        out["option_watchlist"] = (response.get("data") or {}).get("items") or (response.get("data") or {}).get("results") or []
    except Exception as exc:  # noqa: BLE001
        gaps.append(f"option watchlist unavailable: {type(exc).__name__}: {exc}")

    for account in accounts:
        label = "agentic" if account.get("agentic_allowed") else str(account.get("brokerage_account_type") or "brokerage")
        account_number = str(account.get("account_number") or "")
        if not account_number:
            continue
        try:
            response = call("get_option_positions", {"account_number": account_number, "nonzero": True})
            tools.add("get_option_positions")
            positions = (response.get("data") or {}).get("positions") or []
            out["option_positions"].append({"account_label": label, "count": len(positions), "positions": positions[:20]})
        except Exception as exc:  # noqa: BLE001
            gaps.append(f"{label} option positions unavailable: {type(exc).__name__}: {exc}")
        try:
            response = call("get_realized_pnl", {"account_number": account_number, "span": "month", "asset_classes": ["equity", "option"]})
            tools.add("get_realized_pnl")
            out["realized_pnl"].append({"account_label": label, "month": response.get("data")})
        except Exception as exc:  # noqa: BLE001
            gaps.append(f"{label} realized PnL unavailable: {type(exc).__name__}: {exc}")
        for position in account_equity_positions(account_number, gaps, tools)[:20]:
            symbol = str(position.get("symbol") or "").upper().strip()
            shares = number(position.get("quantity"))
            if not symbol or shares is None or shares <= 0:
                continue
            try:
                response = call("get_equity_tax_lots", {"account_number": account_number, "symbol": symbol})
                tools.add("get_equity_tax_lots")
                lots = (response.get("data") or {}).get("lots") or (response.get("data") or {}).get("tax_lots") or []
                summary = summarize_tax_lots(lots if isinstance(lots, list) else [])
                prior = out["equity_tax_lots"].setdefault(symbol, {"accounts": [], "lot_count": 0, "quantity": 0.0})
                prior["accounts"].append(label)
                prior["lot_count"] += summary["lot_count"]
                prior["quantity"] += summary["quantity"]
                if summary.get("short_term_quantity") is not None:
                    prior["short_term_quantity"] = (prior.get("short_term_quantity") or 0.0) + summary["short_term_quantity"]
                if summary.get("long_term_quantity") is not None:
                    prior["long_term_quantity"] = (prior.get("long_term_quantity") or 0.0) + summary["long_term_quantity"]
            except Exception as exc:  # noqa: BLE001
                gaps.append(f"{label} {symbol} tax lots unavailable: {type(exc).__name__}: {exc}")
    return out


def collect(symbols: list[str]) -> dict:
    normalized = symbol_set(symbols)
    gaps: list[str] = []
    tools: set[str] = set()
    scans = collect_scans(gaps, tools)
    scan_symbols = sorted(scans.get("candidates") or {})
    research_symbols = symbol_set([*normalized, *scan_symbols])
    snapshot = {
        "schema": "openclaw.market_worker.robinhood_research.v1",
        "source": "robinhood-agentic",
        "generated_at": now_ct().isoformat(),
        "requested_symbols": normalized,
        "research_symbols": research_symbols,
        "tools_used": [],
        "scanners": scans,
        "earnings": collect_earnings(research_symbols, gaps, tools),
        "fundamentals": collect_fundamentals(research_symbols, gaps, tools),
        "financials": collect_financials(research_symbols, gaps, tools),
        "technicals": collect_technicals(research_symbols, gaps, tools),
        "account_research": collect_account_research(gaps, tools),
        "data_gaps": gaps[:80],
    }
    snapshot["tools_used"] = sorted(tools)
    return snapshot


def write_snapshot(snapshot: dict) -> None:
    tmp = OUT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(OUT_FILE)
    os.chmod(OUT_FILE, 0o600)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", help="comma-separated symbols to refresh")
    args = parser.parse_args()
    if not RH_CLIENT.exists():
        raise SystemExit(f"missing Robinhood client: {RH_CLIENT}")
    symbols = [part.strip().upper() for part in (args.symbols or "").split(",") if part.strip()]
    try:
        snapshot = collect(symbols)
    except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
        print(f"Robinhood research refresh failed; preserving prior snapshot: {type(exc).__name__}: {exc}")
        return 1
    write_snapshot(snapshot)
    print(
        f"refreshed Robinhood research for {len(snapshot['research_symbols'])} symbols; "
        f"{len((snapshot.get('scanners') or {}).get('candidates') or {})} scanner candidates"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
