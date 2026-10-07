#!/usr/bin/env python3
"""Advisory review for protective sell orders on open positions.

This script is read-only. It reviews current holdings, live quotes, recent
sell-rule evidence, then writes
a recommendation report. It never places, cancels, or modifies orders.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import subprocess
from pathlib import Path

import market_history_analysis as history_analysis
import market_intraday_models
import market_signal_confirm as confirm
import market_schwab


ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get("MARKET_WORKER_DATA_DIR", str(ROOT.parent / "handoffs" / "market-worker")))
OUT_DIR = DATA_ROOT / "protective-order-review"
SELL_TICKET_DIR = Path(os.environ.get("MARKET_SELL_TICKET_DIR", str(DATA_ROOT / "sell-tickets")))
STATE_FILE = DATA_ROOT / "market-protective-order-review-state.json"
TZ = confirm.TZ
OPENCLAW_CLI = os.environ.get("OPENCLAW_CLI", "/home/pi/.npm-global/bin/openclaw")
TELEGRAM_TARGET = os.environ.get("MARKET_PROTECTIVE_TELEGRAM_TARGET", confirm.TELEGRAM_TARGET)
TELEGRAM_ACCOUNT = os.environ.get("MARKET_TELEGRAM_ACCOUNT", confirm.TELEGRAM_ACCOUNT)
NOTIFY_TELEGRAM = os.environ.get("MARKET_PROTECTIVE_NOTIFY_TELEGRAM", "1").lower() not in {"0", "false", "no"}
NOTIFY_DRY_RUN = os.environ.get("MARKET_PROTECTIVE_NOTIFY_DRY_RUN", "0").lower() in {"1", "true", "yes"}
FRACTIONAL_PRICE_ACTIONS = {
    "manual_fractional_exit_review",
    "manual_fractional_protective_price",
}
NON_REPORTING_ACTIONS = {"leave_existing_order"}
TRAILING_OFFSET_MISMATCH_RATIO = 0.10


def now_ct() -> dt.datetime:
    return dt.datetime.now(TZ)


def number(value: object) -> float | None:
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def money(value: float | None) -> str:
    return "n/a" if value is None else f"${value:.2f}"


def pct_value(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{max(1, math.floor(value + 0.5))}%"


def account_label(account: dict) -> str:
    if account.get("nickname"):
        return str(account["nickname"])[:80]
    label = str(account.get("brokerage_account_type") or "brokerage")
    if account.get("is_default"):
        label += " default"
    elif account.get("agentic_allowed"):
        label += " agentic"
    return label[:80]


def open_orders_by_side() -> tuple[dict[str, list[dict]], list[dict], list[str]]:
    """Read the latest local read-only Schwab order snapshot."""
    snapshot = market_schwab.read_json_file(market_schwab.OPEN_ORDERS_FILE)
    if not snapshot:
        return {}, [], ["Schwab open-order snapshot is missing"]
    if snapshot.get("status") != "ok":
        return {}, [], [f"Schwab open-order snapshot unavailable: {snapshot.get('error') or 'unknown error'}"]
    sell_orders: dict[str, list[dict]] = {}
    buy_orders: list[dict] = []
    for order in snapshot.get("orders") or []:
        if not isinstance(order, dict):
            continue
        ticker = str(order.get("symbol") or "").upper().strip()
        if not ticker:
            continue
        if order.get("side") == "sell":
            sell_orders.setdefault(ticker, []).append(order)
        elif order.get("side") == "buy":
            buy_orders.append(order)
    return sell_orders, buy_orders, []


def open_sell_orders_by_symbol() -> tuple[dict[str, list[dict]], list[str]]:
    """Return no broker order snapshot until read-only Schwab orders are added."""
    sell_orders, _buy_orders, errors = open_orders_by_side()
    return sell_orders, errors


def cushion_pct(history: history_analysis.HistoryFeature | None, ticker: str) -> float:
    """Percent cushion below current price before a suggested protective stop."""
    if history and history.volatility_20d_pct is not None:
        base = history.volatility_20d_pct * 0.75
    else:
        base = 3.0
    sector = confirm.TICKER_SECTOR.get(ticker, "")
    floor = 1.5 if sector in {"market"} else 2.0
    return min(max(base, floor), 8.0)


def trailing_pct_for_stop(price: float | None, stop: float | None) -> float | None:
    if price is None or stop is None or price <= 0 or stop <= 0 or stop >= price:
        return None
    return (price - stop) / price * 100


def trailing_stop_limit_order_eligible(position: dict | None) -> tuple[bool, str]:
    """Return whether the position should be reviewed as a trailing stop-limit order."""
    source = position
    if not source:
        return False, "no position snapshot"
    brokers = {
        str(item).lower()
        for item in source.get("brokers", [])
        if isinstance(item, str) and item.strip()
    } | {str(source.get("broker") or "unknown").lower()}
    shares = number(source.get("shares"))
    if shares is None or shares <= 0:
        return False, "share count unavailable"
    broker_text = ",".join(sorted(brokers - {"unknown"})) or "position"
    return True, f"{broker_text} trailing stop-limit percent review: {shares:g} shares"


def suggested_intraday_stop(
    entry: float | None,
    price: float | None,
    plan: dict,
) -> tuple[float | None, float | None, float | None, list[str]]:
    """Adapt a five-minute structure stop into the protective review format."""
    stop = number(plan.get("stop"))
    if price is None or price <= 0 or stop is None or stop <= 0 or stop >= price:
        return None, None, None, [f"intraday protection unavailable: {plan.get('reason', 'invalid plan')}"]
    if entry is not None and entry > 0 and price >= entry:
        stop = max(stop, entry)
    trailing_pct = trailing_pct_for_stop(price, stop)
    return stop, stop * 0.995, trailing_pct, [
        "intraday-origin position: 5-minute structure/ATR protection",
        str(plan.get("reason") or "recent swing low with ATR buffer"),
    ]


def suggested_stop(entry: float | None, price: float | None, gain_pct: float | None, history: history_analysis.HistoryFeature | None, ticker: str) -> tuple[float | None, float | None, float | None, list[str]]:
    if entry is None or price is None or price <= 0:
        return None, None, None, ["missing entry or live price"]

    reasons: list[str] = []
    cushion = cushion_pct(history, ticker)
    volatility_floor = price * (1 - cushion / 100)

    if gain_pct is None:
        profit_floor = entry * 0.97
        reasons.append("entry fallback: cap loss near 3%")
    elif gain_pct < 1.0:
        profit_floor = entry * 0.97
        reasons.append("small/no gain: cap loss near 3%")
    elif gain_pct < 3.0:
        profit_floor = entry * (1 + (gain_pct * 0.25) / 100)
        reasons.append("modest gain: protect about a quarter of the gain")
    elif gain_pct < 6.0:
        profit_floor = entry * (1 + (gain_pct * 0.40) / 100)
        reasons.append("gain above 3%: protect about 40% of the gain")
    elif gain_pct < 10.0:
        profit_floor = entry * (1 + (gain_pct * 0.55) / 100)
        reasons.append("gain above 6%: protect over half the gain")
    else:
        profit_floor = entry * (1 + (gain_pct * 0.70) / 100)
        reasons.append("large gain: protect most of the gained cushion")

    stop = max(profit_floor, volatility_floor)
    stop = min(stop, price * 0.995)
    limit = stop * 0.995
    trailing_pct = trailing_pct_for_stop(price, stop)
    reasons.append(f"volatility cushion: {cushion:.1f}% below current price")
    return stop, limit, trailing_pct, reasons


def strongest_stop(orders: list[dict]) -> tuple[float | None, dict | None]:
    best_stop = None
    best_order = None
    for order in orders:
        stop = number(order.get("stop_price"))
        price = number(order.get("price"))
        candidate = stop if stop is not None else price
        if candidate is None:
            continue
        if best_stop is None or candidate > best_stop:
            best_stop = candidate
            best_order = order
    return best_stop, best_order


def trailing_orders(orders: list[dict]) -> list[dict]:
    return [
        order for order in orders
        if str(order.get("type") or "").lower().startswith("trailing_stop")
    ]


def trailing_order_text(orders: list[dict]) -> str:
    parts: list[str] = []
    for order in trailing_orders(orders):
        offset = number(order.get("trailing_pct"))
        if offset is not None:
            parts.append(f"{offset:g}%")
            continue
        offset = number(order.get("trailing_offset"))
        if offset is not None:
            unit = "" if order.get("trailing_offset_type") == "percent" else " value"
            parts.append(f"{offset:g}{unit}")
            continue
        parts.append("offset unavailable")
    return ", ".join(parts) if parts else "none"


def existing_trailing_pct(orders: list[dict]) -> float | None:
    """Return the first broker-reported percentage trailing offset."""
    for order in trailing_orders(orders):
        offset = number(order.get("trailing_pct"))
        if offset is not None and offset > 0:
            return offset
    return None


def trailing_offset_mismatch(current: float | None, suggested: float | None) -> bool:
    """Flag a material relative difference between existing and suggested trails."""
    if current is None or suggested is None or suggested <= 0:
        return False
    return abs(current - suggested) / suggested > TRAILING_OFFSET_MISMATCH_RATIO


def classify_order_review(
    ticker: str,
    current_stop: float | None,
    suggested: float | None,
    sell_score: int,
    sell_threshold: int,
    evidence: list[str],
    orders: list[dict],
    order_eligible: bool,
    existing_trailing_orders: list[dict] | None = None,
    suggested_trailing_pct: float | None = None,
    existing_trailing_pct_value: float | None = None,
) -> str:
    if not order_eligible:
        if sell_score >= sell_threshold and evidence:
            return "manual_fractional_exit_review"
        if suggested is not None:
            return "manual_fractional_protective_price"
        return "insufficient_data"
    if sell_score >= sell_threshold and evidence:
        return "manual_exit_review"
    if trailing_offset_mismatch(existing_trailing_pct_value, suggested_trailing_pct):
        return "adjust_trailing_stop"
    if existing_trailing_orders:
        return "leave_existing_order"
    if suggested is None:
        return "insufficient_data"
    if not orders:
        return "consider_initial_protective_stop"
    if current_stop is None:
        return "consider_add_protective_stop"
    if suggested > current_stop * 1.01:
        return "consider_raise_stop"
    return "leave_existing_order"


def read_state() -> dict:
    if not STATE_FILE.exists():
        return {"schema": "openclaw.market_worker.protective_order_review.v1", "positions": {}}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema": "openclaw.market_worker.protective_order_review.v1", "positions": {}}
    data.setdefault("schema", "openclaw.market_worker.protective_order_review.v1")
    data.setdefault("positions", {})
    return data


def write_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(STATE_FILE)


def state_changes(rows: list[dict], state: dict) -> list[dict]:
    previous = state.setdefault("positions", {})
    changes: list[dict] = []
    for row in rows:
        ticker = row["ticker"]
        prior = previous.get(ticker, {})
        prior_stop = number(prior.get("suggested_stop"))
        current_stop = row.get("suggested_stop")
        action_changed = row["action"] != prior.get("action")
        stop_raised = (
            current_stop is not None
            and prior_stop is not None
            and current_stop > prior_stop * 1.01
            and row["action"] in {
                "consider_raise_stop",
                "consider_initial_protective_stop",
                "manual_exit_review",
                *FRACTIONAL_PRICE_ACTIONS,
            }
        )
        first_actionable = not prior and row["action"] != "leave_existing_order"
        if row["action"] not in NON_REPORTING_ACTIONS and (action_changed or stop_raised or first_actionable):
            changes.append(row)
        previous[ticker] = {
            "action": row["action"],
            "suggested_stop": round(current_stop, 2) if current_stop is not None else None,
            "suggested_stop_limit": round(row["suggested_stop_limit"], 2) if row.get("suggested_stop_limit") is not None else None,
            "suggested_trailing_pct": round(row["suggested_trailing_pct"], 1) if row.get("suggested_trailing_pct") is not None else None,
            "existing_sell_floor": round(row["current_stop"], 2) if row.get("current_stop") is not None else None,
            "order_eligible": bool(row.get("order_eligible")),
            "order_eligibility": row.get("order_eligibility"),
            "strategy": row.get("strategy", "swing_position"),
            "price": round(row["price"], 2) if row.get("price") is not None else None,
            "updated_at": now_ct().isoformat(),
        }
    open_tickers = {row["ticker"] for row in rows}
    for ticker in list(previous):
        if ticker not in open_tickers:
            previous.pop(ticker, None)
    return changes


def build_rows(window: str) -> tuple[list[dict], list[str], list[dict]]:
    orders_by_symbol, open_buy_orders, order_errors = open_orders_by_side()
    open_positions = confirm.read_positions()
    if not open_positions:
        return [], order_errors, open_buy_orders
    intraday_origins = confirm.intraday_buy_origins()
    intraday_config = None
    if any(ticker in intraday_origins for ticker in open_positions):
        try:
            intraday_config = market_schwab.load_env()
        except Exception as exc:  # noqa: BLE001
            order_errors.append(f"intraday protection config unavailable: {type(exc).__name__}")

    for ticker in open_positions:
        confirm.TICKER_SECTOR.setdefault(ticker, "consumer")
    selected = sorted(open_positions)
    quote_tickers = sorted(set(selected) | {ctx for ticker in selected for ctx in confirm.CONTEXT[confirm.TICKER_SECTOR[ticker]]})
    quotes = confirm.fetch_primary_quotes(quote_tickers, prefer_extended=window == "after-close")
    packet_hits = confirm.packet_mentions(selected)
    histories = history_analysis.features_for_symbols(selected)

    rows: list[dict] = []
    for ticker in selected:
        quote = quotes[ticker]
        position = open_positions[ticker]
        entry = confirm.position_entry_price(position)
        price = quote.price
        gain_pct = ((price - entry) / entry * 100) if price is not None and entry else None
        headlines = confirm.fetch_rss_headlines(ticker)
        sell_score, evidence, blockers = confirm.score_sell_position(
            ticker,
            quote,
            quotes,
            packet_hits,
            headlines,
            position,
            window,
            histories.get(ticker),
        )
        threshold = int(confirm.sell_thresholds(window)["score"])
        strategy = "swing_position"
        stop, limit, trailing_pct, stop_reasons = suggested_stop(entry, price, gain_pct, histories.get(ticker), ticker)
        if ticker in intraday_origins and intraday_config is not None:
            try:
                payload = market_schwab.fetch_intraday_history(intraday_config, ticker, days=1, frequency=5)
                candles = payload.get("candles") if isinstance(payload, dict) else []
                plan = market_intraday_models.protection_plan(ticker, candles if isinstance(candles, list) else [])
                if plan.get("status") == "ok":
                    stop, limit, trailing_pct, stop_reasons = suggested_intraday_stop(entry, price, plan)
                    strategy = "intraday"
                else:
                    stop_reasons = [f"intraday protection unavailable; using swing/position fallback: {plan.get('reason', 'unknown reason')}", *stop_reasons]
            except Exception as exc:  # noqa: BLE001
                stop_reasons = [f"intraday protection unavailable; using swing/position fallback: {type(exc).__name__}", *stop_reasons]
        orders = orders_by_symbol.get(ticker, [])
        current_stop, strongest = strongest_stop(orders)
        existing_trailing_orders = trailing_orders(orders)
        existing_trailing_pct_value = existing_trailing_pct(existing_trailing_orders)
        order_eligible, order_eligibility = trailing_stop_limit_order_eligible(position)
        action = classify_order_review(
            ticker, current_stop, stop, sell_score, threshold, evidence, orders,
            order_eligible, existing_trailing_orders,
            trailing_pct, existing_trailing_pct_value,
        )
        rows.append(
            {
                "ticker": ticker,
                "action": action,
                "shares": position.get("shares"),
                "accounts": position.get("accounts") or position.get("brokers") or [position.get("broker", "unknown")],
                "entry": entry,
                "price": price,
                "gain_pct": gain_pct,
                "day_change_pct": quote.day_change_pct,
                "suggested_stop": stop,
                "suggested_stop_limit": limit,
                "suggested_trailing_pct": trailing_pct,
                "existing_trailing_pct": existing_trailing_pct_value,
                "current_stop": current_stop,
                "order_eligible": order_eligible,
                "order_eligibility": order_eligibility,
                "strongest_order": strongest,
                "open_sell_orders": orders,
                "existing_trailing_orders": existing_trailing_orders,
                "existing_trailing_text": trailing_order_text(existing_trailing_orders),
                "sell_score": sell_score,
                "sell_threshold": threshold,
                "sell_evidence": evidence,
                "sell_blockers": blockers,
                "stop_reasons": stop_reasons,
                "strategy": strategy,
                "intraday_origin": ticker in intraday_origins,
                "quote_error": quote.error,
            }
        )
    priority = {
        "manual_exit_review": 0,
        "adjust_trailing_stop": 1,
        "consider_raise_stop": 2,
        "consider_initial_protective_stop": 3,
        "consider_add_protective_stop": 4,
        "manual_fractional_exit_review": 5,
        "manual_fractional_protective_price": 6,
        "leave_existing_order": 7,
        "insufficient_data": 8,
    }
    rows.sort(key=lambda row: (priority.get(row["action"], 9), row["ticker"]))
    return rows, order_errors, open_buy_orders


def summary_line(row: dict, prefix: str = "") -> str:
    return f"- {prefix}{action_instruction(row)}"


def sell_ticket_path(row: dict, report: Path | None = None) -> Path | None:
    """Write a review-only market-sell ticket for profitable exit signals."""
    price = number(row.get("price"))
    entry = number(row.get("entry"))
    shares = number(row.get("shares"))
    if price is None or entry is None or shares is None or price <= entry or shares <= 0:
        return None
    SELL_TICKET_DIR.mkdir(parents=True, exist_ok=True)
    stem = report.stem if report else dt.datetime.now(TZ).strftime("%Y-%m-%dT%H%M")
    path = SELL_TICKET_DIR / f"{stem}-{row['ticker']}-sell-ticket.json"
    payload = {
        "schema": "openclaw.market_worker.sell_ticket.v1",
        "status": "ready_for_review",
        "ticker": row["ticker"],
        "side": "SELL",
        "order_type": "MARKET",
        "time_in_force": "GFD",
        "quantity": shares,
        "current_price": price,
        "cost_basis": entry,
        "reason": "SELL/EXIT recommendation with current price above cost basis",
        "confirmation_required": True,
        "execution_submitted": False,
        "source_action": row.get("action"),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def action_instruction(row: dict) -> str:
    """Return the plain-language action a user should take."""
    ticker = row["ticker"]
    action = row.get("action")
    current = money(row.get("current_stop"))
    suggested = money(row.get("suggested_stop"))
    existing = trailing_offset_decimal(row.get("existing_trailing_pct"))
    recommended = trailing_offset_decimal(row.get("suggested_trailing_pct"))
    if action in {"manual_exit_review", "manual_fractional_exit_review"}:
        price = number(row.get("price"))
        entry = number(row.get("entry"))
        shares = number(row.get("shares"))
        if price is not None and entry is not None and shares is not None and price > entry:
            return f"{ticker}: SELL TICKET READY — sell {shares:g} shares at market; current {money(price)} > cost basis {money(entry)}; confirm before submitting"
        return f"{ticker}: SELL/EXIT — sell evidence is active; review the position now"
    if action == "adjust_trailing_stop":
        return f"{ticker}: CHANGE trailing stop from {existing} to {recommended}"
    if action == "consider_raise_stop":
        return f"{ticker}: RAISE protective stop from {current} to {suggested}"
    if action in {"consider_initial_protective_stop", "manual_fractional_protective_price"}:
        return f"{ticker}: SET protective stop at {suggested} (trailing offset {recommended})"
    if action == "consider_add_protective_stop":
        return f"{ticker}: ADD protective stop at {suggested} (trailing offset {recommended})"
    if action == "leave_existing_order":
        protection = row.get("existing_trailing_text") or (
            f"stop {current}" if current != "n/a" else "existing order"
        )
        return f"{ticker}: KEEP existing protection ({protection})"
    return f"{ticker}: NO ACTION — protection recommendation unavailable"


def stop_gap_text(price: float | None, stop: float | None) -> str:
    gap = trailing_pct_for_stop(price, stop)
    if gap is None:
        return "n/a"
    return f"{gap:.1f}% below current"


def limit_offset_pct(stop: float | None, limit: float | None) -> float | None:
    if stop is None or limit is None or stop <= 0 or limit <= 0 or limit >= stop:
        return None
    return (stop - limit) / stop * 100


def pct_decimal(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}%"



def trailing_offset_decimal(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}%"


def trailing_stop_limit_text(row: dict) -> str:
    return (
        f"TrailingStopLimit trail {pct_value(row.get('suggested_trailing_pct'))} "
        f"limit_offset {pct_decimal(limit_offset_pct(row.get('suggested_stop'), row.get('suggested_stop_limit')))} "
        f"(stop_ref {money(row.get('suggested_stop'))}, limit_ref {money(row.get('suggested_stop_limit'))})"
    )


def display_action(action: str) -> str:
    if action == "consider_raise_stop":
        return "consider_raise_stop_floor"
    if action == "manual_fractional_exit_review":
        return "manual_exit_review"
    if action == "manual_fractional_protective_price":
        return "consider_initial_protective_stop"
    return action


def order_type_text(order: dict) -> str:
    if order.get("trigger") == "stop" and order.get("type") == "limit":
        return "stop_limit"
    if order.get("trigger") == "stop":
        return "stop_market"
    return str(order.get("type") or "unknown")


def buy_order_line(order: dict) -> str:
    price = order.get("price") or order.get("limit_price") or "n/a"
    stop = order.get("stop_price") or "n/a"
    quantity = order.get("quantity") or order.get("cumulative_quantity") or "n/a"
    notional = order.get("dollar_amount") or order.get("notional") or "n/a"
    return (
        f"- {str(order.get('symbol') or '').upper()}: {order_type_text(order)}; "
        f"state {order.get('state')}; qty {quantity}; notional {notional}; "
        f"limit {price}; stop {stop}; account {order.get('account_label')}; id {order.get('id')}"
    )


def write_report(rows: list[dict], changes: list[dict], order_errors: list[str], open_buy_orders: list[dict], window: str, force: bool) -> tuple[Path | None, str]:
    actionable = [row for row in rows if row["action"] not in NON_REPORTING_ACTIONS]
    if not rows and not open_buy_orders and not force:
        return None, "no open positions"
    if not changes and not order_errors and not open_buy_orders and not force:
        return None, "no protective order changes suggested"

    OUT_DIR.mkdir(exist_ok=True)
    now = now_ct()
    out = OUT_DIR / f"{now.strftime('%Y-%m-%dT%H%M')}-protective-order-review-{window}.md"
    lines = [
        "source: market-protective-order-review",
        "status: proposed",
        "target: market-scout",
        f"window: {window}",
        f"generated_at: {now.isoformat()}",
        "policy: advisory only; no orders placed, cancelled, or modified",
        "",
        "## summary",
    ]
    if changes:
        for row in changes:
            lines.append(summary_line(row))
    elif actionable:
        lines.append("- no changed recommendation state; open advisory rows included because write-all was requested")
        lines.extend(summary_line(row, prefix="unchanged ") for row in actionable)
    else:
        lines.append("- no protective order changes suggested")
    if open_buy_orders:
        lines.append(f"- outstanding_buy_orders: {len(open_buy_orders)} open BUY order(s) not cancelled")
    if order_errors:
        lines.extend(["", "## order_read_errors", *[f"- {error}" for error in order_errors]])

    lines.extend(["", "## outstanding_buy_orders"])
    if open_buy_orders:
        lines.extend(buy_order_line(order) for order in open_buy_orders)
    else:
        lines.append("- none")

    lines.extend(["", "## positions"])
    for row in actionable:
        lines.extend(
            [
                f"### {row['ticker']}",
                f"- what_to_do: {action_instruction(row)}",
                f"- position: shares {row['shares']}; accounts {', '.join(str(a) for a in row['accounts'])}",
                f"- quote: price {money(row['price'])}; day {confirm.pct(row['day_change_pct'])}; position {confirm.pct(row['gain_pct'])}",
                f"- order_eligibility: {row['order_eligibility']}",
                f"- strategy: {row.get('strategy', 'swing_position')}",
                f"- existing_sell_floor: {money(row['current_stop'])} ({stop_gap_text(row['price'], row['current_stop'])})",
                f"- existing_trailing_stop: {row.get('existing_trailing_text') or 'none'}",
                f"- existing_trailing_pct: {pct_decimal(row.get('existing_trailing_pct'))}",
                f"- sell_rule_score: {row['sell_score']}/{row['sell_threshold']}",
            ]
        )
        lines.append(
            f"- suggested_stop_floor: {money(row['suggested_stop'])} "
            f"({stop_gap_text(row['price'], row['suggested_stop'])})"
        )
        lines.append(f"- suggested_trailing_stop_limit_sell: {row['ticker']} {trailing_stop_limit_text(row)}")
        if row["quote_error"]:
            lines.append(f"- quote_error: {row['quote_error']}")
        if row["stop_reasons"]:
            lines.append("- stop_logic:")
            lines.extend(f"  - {reason}" for reason in row["stop_reasons"])
        if row["sell_evidence"]:
            lines.append("- sell_evidence:")
            lines.extend(f"  - {item}" for item in row["sell_evidence"])
        if row["sell_blockers"]:
            lines.append("- sell_blockers:")
            lines.extend(f"  - {item}" for item in row["sell_blockers"])
        if row["open_sell_orders"]:
            lines.append("- open_sell_orders:")
            for order in row["open_sell_orders"]:
                order_type = "stop_limit" if order.get("trigger") == "stop" and order.get("type") == "limit" else (
                    "stop_market" if order.get("trigger") == "stop" else str(order.get("type") or "unknown")
                )
                lines.append(
                    f"  - {order_type}; state {order.get('state')}; qty {order.get('quantity')}; "
                    f"stop {order.get('stop_price') or 'n/a'}; limit {order.get('price') or 'n/a'}; "
                    f"account {order.get('account_label')}; id {order.get('id')}"
                )
        lines.append("")
    out.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    summary = "; ".join(f"{row['ticker']} {row['action']}" for row in changes) if changes else "no protective order changes suggested"
    if open_buy_orders:
        summary += f"; outstanding BUY orders={len(open_buy_orders)}"
    return out, summary


def notification_rows(rows: list[dict], changes: list[dict], force: bool) -> list[dict]:
    if changes:
        return changes
    if force:
        return [row for row in rows if row["action"] not in NON_REPORTING_ACTIONS]
    return []


def notification_line(row: dict) -> str:
    return action_instruction(row)


def notification_summary(rows: list[dict], changes: list[dict], force: bool) -> str:
    selected = notification_rows(rows, changes, force)
    if selected:
        return "\n".join(notification_line(row) for row in selected[:8])
    if force:
        return "No changed protective-stop recommendations; opening report written for review."
    return "No protective-stop notification changes."


def notify_telegram(rows: list[dict], changes: list[dict], report: Path | None, window: str, force: bool) -> str:
    # --write-all (and the pre-market opening run) may deliberately create a
    # report with unchanged advisory rows.  That report is useful on disk, but
    # it must not create a Telegram "no changes" notification.
    if not changes:
        return "telegram notification skipped: no protective-stop notification changes"
    if not report:
        return "telegram notification skipped: no report"
    for row in changes:
        if row.get("action") in {"manual_exit_review", "manual_fractional_exit_review"}:
            sell_ticket_path(row, report)
    if not NOTIFY_TELEGRAM:
        return "telegram notification disabled"
    if not TELEGRAM_TARGET:
        return "telegram notification skipped: MARKET_PROTECTIVE_TELEGRAM_TARGET is empty"

    label = "AFTER_HOURS" if window == "after-close" else window
    message = (
        f"Protective stop review ({label})\n"
        f"{notification_summary(rows, changes, force)}\n"
        f"{report}"
    )
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
    if NOTIFY_DRY_RUN:
        cmd.append("--dry-run")

    try:
        result = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=30, check=False)
    except Exception as exc:  # noqa: BLE001 - notification must not block report generation.
        return f"telegram notification failed: {type(exc).__name__}: {exc}"

    output = (result.stdout + result.stderr).strip()
    if result.returncode == 0:
        return "telegram notification ok" + (f": {output[:300]}" if output else "")
    return f"telegram notification failed ({result.returncode}): {output[:500]}"


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--window", choices=["opening", "one-hour", "midday", "late-day", "after-close"])
    parser.add_argument("--write-all", action="store_true", help="write even when no protective order change is suggested")
    parser.add_argument("--no-notify", action="store_true", help="skip Telegram notification")
    args = parser.parse_args()

    now = now_ct()
    window = args.window or current_window(now)
    rows, order_errors, open_buy_orders = build_rows(window)
    state = read_state()
    changes = state_changes(rows, state)
    write_state(state)
    force = args.write_all or (window == "opening" and now.time() < dt.time(8, 30))
    report, summary = write_report(rows, changes, order_errors, open_buy_orders, window, force)
    print(f"reviewed {len(rows)} open positions; changes={len(changes)}; open_buy_orders={len(open_buy_orders)}; {summary}; window={window}")
    if report:
        print(report)
        if args.no_notify:
            print("telegram notification skipped by --no-notify")
        else:
            print(notify_telegram(rows, changes, report, window, force))
    if order_errors:
        print(f"order read errors: {len(order_errors)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
