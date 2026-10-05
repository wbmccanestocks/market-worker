#!/usr/bin/env python3
"""Market-scout signal confirmation runner.

Reads the latest market-worker packets, checks current Yahoo Finance quote/chart
data for the configured watchlist, and writes explicit buy / sell / hold /
don't-buy research calls for each candidate at a scheduled confirmation window.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import datetime as dt
import gzip
import html
import io
import json
import math
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import market_history_analysis as history_analysis
import market_intraday_models
import market_schwab


def env_bool(name: str, default: bool) -> bool:
    fallback = "1" if default else "0"
    return os.environ.get(name, fallback).lower() not in {"0", "false", "no"}


ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get("MARKET_WORKER_DATA_DIR", str(ROOT.parent / "handoffs" / "market-worker")))
CONFIRMATION_PROFILE = os.environ.get("MARKET_CONFIRM_PROFILE", "market").strip() or "market"
OUT_DIR = Path(os.environ.get("MARKET_CONFIRM_OUT_DIR", str(DATA_ROOT / "confirmations")))
POSITIONS_FILE = DATA_ROOT / "market-position-feedback.json"
ROBINHOOD_POSITIONS_FILE = DATA_ROOT / "market-robinhood-positions.json"
SCHWAB_POSITIONS_FILE = DATA_ROOT / "market-schwab-positions.json"
SCHWAB_BALANCES_FILE = DATA_ROOT / "market-schwab-balances.json"
RECOMMENDATION_STATE_FILE = Path(
    os.environ.get("MARKET_CONFIRM_RECOMMENDATION_STATE_FILE", str(DATA_ROOT / "market-recommendation-state.json"))
)
LIQUIDITY_CACHE_FILE = DATA_ROOT / "market-liquidity-cache.json"
IDEA_SCREEN_FILE = DATA_ROOT / "market-idea-screen.json"
CATALYST_CALENDAR_FILE = DATA_ROOT / "market-catalyst-calendar.json"
THESIS_TRACKER_FILE = DATA_ROOT / "market-thesis-tracker.json"
ROBINHOOD_RESEARCH_FILE = DATA_ROOT / "market-robinhood-research.json"
BROKER_ELIGIBILITY_FILE = DATA_ROOT / "market-broker-eligibility.json"
TZ = ZoneInfo("America/Chicago")
ET = ZoneInfo("America/New_York")
OPENCLAW_CLI = os.environ.get("OPENCLAW_CLI", "/home/pi/.npm-global/bin/openclaw")
ROBINHOOD_CLIENT = Path(
    os.environ.get(
        "MARKET_CONFIRM_ROBINHOOD_CLIENT",
        str(ROOT.parent / "skills" / "robinhood-agentic" / "rh-client.mjs"),
    )
)
ROBINHOOD_QUOTE_TIMEOUT = int(os.environ.get("MARKET_CONFIRM_ROBINHOOD_QUOTE_TIMEOUT", "30"))
TELEGRAM_TARGET = os.environ.get("MARKET_CONFIRM_TELEGRAM_TARGET", "8920769890")
TELEGRAM_ACCOUNT = os.environ.get("MARKET_TELEGRAM_ACCOUNT", "market_bot")
NOTIFY_TELEGRAM = env_bool("MARKET_CONFIRM_NOTIFY_TELEGRAM", True)
NOTIFY_DRY_RUN = env_bool("MARKET_CONFIRM_NOTIFY_DRY_RUN", False)
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 OpenClawMarketSignal/1.0"
)

WATCHLIST = {
    "tech": ["NVDA", "MSFT", "AAPL", "AMD", "AVGO", "GOOG", "GOOGL", "META", "TSLA", "TSLL", "TSLS", "TSLZ", "SPCX", "LOFF", "LOFD", "QQQ", "XLK", "SMH", "TQQQ", "SOXL", "IONQ", "NVDL", "NVD", "SQQQ", "SOXS"],
    "consumer": ["AMZN", "AMZU", "AMZD", "LOW", "F", "FB", "RIVN", "LCID", "WBD", "SNAP", "OPEN", "TALK", "TZA"],
    "energy": ["XOM", "CVX", "COP", "OXY", "SLB", "LNG", "ENPH", "FSLR", "XLE", "XOP", "USO"],
    "financial": ["SOFI"],
    "healthcare": ["XLV", "XBI", "IBB", "PFE", "LABU", "LABD"],
    "international": ["YINN"],
    "crypto": ["BITX", "SBIT"],
}
CONTEXT = {
    "tech": ["QQQ", "XLK", "SMH"],
    "consumer": ["SPY", "QQQ", "IWM"],
    "energy": ["XLE", "XOP", "USO"],
    "financial": ["XLF", "KRE", "SPY"],
    "healthcare": ["XLV", "XBI", "IBB"],
    "international": ["FXI", "KWEB", "EEM"],
    # BITX and SBIT target bitcoin exposure.  IBIT/BITO are liquid, quoted
    # bitcoin ETP proxies used only for context; they are not BUY candidates.
    "crypto": ["IBIT", "BITO"],
}
TICKER_SECTOR = {ticker: sector for sector, tickers in WATCHLIST.items() for ticker in tickers}
ALL_TICKERS = sorted(TICKER_SECTOR)
NEWS_CANDIDATE_LIMIT = int(os.environ.get("MARKET_CONFIRM_NEWS_CANDIDATE_LIMIT", "40"))
# Feed-discovered names are an intentionally temporary candidate universe.  A
# stock stays eligible for consideration only while it has relevant Technology
# or Energy coverage within this window; this is not a watchlist expansion.
FEED_CANDIDATE_HORIZON_DAYS = int(os.environ.get("MARKET_CONFIRM_FEED_CANDIDATE_HORIZON_DAYS", "7"))
FEED_CANDIDATE_PACKET_LIMIT = int(os.environ.get("MARKET_CONFIRM_FEED_CANDIDATE_PACKET_LIMIT", "240"))
AMBIGUOUS_TICKERS = {"ALL", "ARE", "BE", "BY", "CAN", "FOR", "HAS", "IT", "NOW", "ON", "OR", "SO", "TE"}
MIN_BUY_PRICE = float(os.environ.get("MARKET_CONFIRM_MIN_BUY_PRICE", "2.0"))
MIN_BUY_AVG_DOLLAR_VOLUME = float(os.environ.get("MARKET_CONFIRM_MIN_BUY_AVG_DOLLAR_VOLUME", "1000000"))
# Dollar turnover by itself is insufficient: a high-priced thin name can still
# be difficult to enter or exit. Require a minimum 10-day average share volume.
MIN_BUY_AVG_VOLUME = int(os.environ.get("MARKET_CONFIRM_MIN_BUY_AVG_VOLUME", "500000"))
MIN_BUY_CURRENT_VOLUME = int(os.environ.get("MARKET_CONFIRM_MIN_BUY_CURRENT_VOLUME", "100000"))
MIN_OPENING_BUY_CURRENT_VOLUME = int(os.environ.get("MARKET_CONFIRM_MIN_OPENING_BUY_CURRENT_VOLUME", "50000"))
INTRADAY_PAPER_ENABLED = env_bool("MARKET_CONFIRM_INTRADAY_PAPER_ENABLED", True)
INTRADAY_PAPER_MAX_TICKERS = int(os.environ.get("MARKET_CONFIRM_INTRADAY_PAPER_MAX_TICKERS", "60"))
INTRADAY_PAPER_WORKERS = int(os.environ.get("MARKET_CONFIRM_INTRADAY_PAPER_WORKERS", "6"))
LOW_SPENDING_POWER_THRESHOLD = float(os.environ.get("MARKET_CONFIRM_LOW_SPENDING_POWER_THRESHOLD", "25"))
MAX_WHOLE_SHARE_TRADE_VALUE = float(os.environ.get("MARKET_CONFIRM_MAX_WHOLE_SHARE_TRADE_VALUE", "125"))
WHOLE_SHARE_PROTECTIVE_STOPS_ONLY = env_bool("MARKET_CONFIRM_WHOLE_SHARE_PROTECTIVE_STOPS_ONLY", True)
CASH_ONLY_NO_MARGIN = env_bool("MARKET_CONFIRM_CASH_ONLY_NO_MARGIN", True)
ROBINHOOD_SPENDING_POWER_GATE_ENABLED = False
SCHWAB_BALANCE_GATE_ENABLED = env_bool("MARKET_CONFIRM_SCHWAB_BALANCE_GATE", False)
ROBINHOOD_RESEARCH_ENABLED = False
ROBINHOOD_QUOTES_ENABLED = False
SCHWAB_QUOTES_ENABLED = env_bool("MARKET_CONFIRM_SCHWAB_QUOTES", True)
# The momentum-chasing aggressive lane tested poorly in the saved-signal audit.
# Keep its scoring visible only as a suppressed WATCH until a future paper study
# provides enough out-of-sample evidence to revisit it.
AGGRESSIVE_BUY_ENABLED = env_bool("MARKET_CONFIRM_AGGRESSIVE_BUY_ENABLED", False)
AGGRESSIVE_BUY_MIN_CHANGE = float(os.environ.get("MARKET_CONFIRM_AGGRESSIVE_BUY_MIN_CHANGE", "2.5"))
AGGRESSIVE_BUY_MAX_CHANGE = float(os.environ.get("MARKET_CONFIRM_AGGRESSIVE_BUY_MAX_CHANGE", "8.0"))
AGGRESSIVE_BUY_MIN_VOLUME_RATIO = float(os.environ.get("MARKET_CONFIRM_AGGRESSIVE_BUY_MIN_VOLUME_RATIO", "0.8"))
IDEA_SCREEN_REFRESH_DAYS = int(os.environ.get("MARKET_CONFIRM_IDEA_SCREEN_REFRESH_DAYS", "7"))
IDEA_SCREEN_CANDIDATE_LIMIT = int(os.environ.get("MARKET_CONFIRM_IDEA_SCREEN_CANDIDATE_LIMIT", "20"))
CATALYST_CALENDAR_MAX_AGE_DAYS = int(os.environ.get("MARKET_CONFIRM_CATALYST_CALENDAR_MAX_AGE_DAYS", "14"))
CATALYST_CALENDAR_SYMBOL_LIMIT = int(os.environ.get("MARKET_CONFIRM_CATALYST_CALENDAR_SYMBOL_LIMIT", "25"))
ROBINHOOD_RESEARCH_MAX_AGE_HOURS = float(os.environ.get("MARKET_CONFIRM_ROBINHOOD_RESEARCH_MAX_AGE_HOURS", "3"))
ROBINHOOD_RESEARCH_CANDIDATE_LIMIT = int(os.environ.get("MARKET_CONFIRM_ROBINHOOD_RESEARCH_CANDIDATE_LIMIT", "40"))
ROBINHOOD_ORDER_REVIEW_ENABLED = False
POSITION_BROKERS = {
    part.strip().lower()
    for part in os.environ.get("MARKET_CONFIRM_POSITION_BROKERS", "manual,schwab").split(",")
    if part.strip()
}
BULL_LEVERAGED_ETFS = {"TQQQ", "SOXL", "TSLL", "AMZU", "BITX", "LABU", "NVDL", "YINN", "LOFF"}
INVERSE_LEVERAGED_ETFS = {"SQQQ", "SOXS", "TSLS", "TSLZ", "NVD", "AMZD", "SBIT", "LABD", "TZA", "LOFD"}
LEVERAGED_ETFS = BULL_LEVERAGED_ETFS | INVERSE_LEVERAGED_ETFS
KNOWN_ETFS = set().union(*CONTEXT.values()) | {"QQQ", "XLK", "SMH", "USO", "XLE", "XOP", "XLV", "XBI", "IBB"} | LEVERAGED_ETFS
DEFAULT_BUY_EXCLUDED_SYMBOLS = {
    "SNY": "not supported on Robinhood; Schwab warns the stock may be subject to foreign tax",
}

# These are entry vetoes: the candidate is unsafe to size or cannot be
# responsibly evaluated. Other pullback/reclaim checks are scored
# confirmations, so one late or unavailable soft signal does not erase an
# otherwise well-supported overnight setup.
PULLBACK_HARD_BLOCKER_FRAGMENTS = (
    "quote fetch failed",
    "missing price",
    "negative headline",
    "upcoming earnings",
    "binary event",
    "price below durable-trade floor",
    "average dollar volume too low",
    "average share volume too low",
    "current participation too thin",
    "current participation unavailable",
)

# Every entry model shares these non-negotiable overnight-hold protections.
ENTRY_MODEL_HARD_BLOCKER_FRAGMENTS = PULLBACK_HARD_BLOCKER_FRAGMENTS
ENTRY_MODEL_NAMES = {
    "evidence_revision": "Evidence-backed revision momentum",
    "catalyst_trend": "Catalyst + relative-strength continuation",
    "post_earnings": "Post-earnings drift",
    "sector_breakout": "Sector-leader breakout",
    "news_dislocation": "News-dislocation stabilization",
    "pullback_reclaim": "Pullback/reclaim",
}
# Pullback/reclaim is context only.  It cannot independently originate a BUY:
# a falling price near an average is not evidence of a durable recovery.
BUY_ELIGIBLE_ENTRY_MODELS = frozenset(ENTRY_MODEL_NAMES) - {"pullback_reclaim"}

CATALYST_WORDS = {
    "earnings", "guidance", "revenue", "profit", "forecast", "analyst",
    "upgrade", "downgrade", "rating", "price target", "chips", "ai",
    "semiconductor", "cloud", "data center", "cybersecurity", "oil", "gas",
    "opec", "inventory", "supply", "geopolitical", "filing", "merger",
    "acquisition", "regulatory",
}
NEGATIVE_WORDS = {
    "downgrade", "miss", "cuts", "cut", "falls", "slumps", "slides",
    "warning", "probe", "lawsuit", "halt", "fraud", "weak", "loss",
}
POSITIVE_WORDS = {
    "upgrade", "beats", "beat", "raises", "raised", "surges", "jumps",
    "rallies", "strong", "record", "outperform", "buy", "growth",
}
BROKER_BULLISH_WORDS = {
    "upgrade", "upgraded", "overweight", "outperform", "buy rating",
    "raised price target", "raises price target", "price target raised",
    "initiated at buy", "initiated with buy",
}
EARNINGS_VALIDATION_WORDS = {
    "earnings beat", "beat estimates", "beats estimates", "raises guidance",
    "raised guidance", "reaffirmed guidance", "guidance raised",
}
CORPORATE_VALIDATION_WORDS = {
    "contract award", "wins contract", "approved", "approval", "launches",
    "partnership", "acquisition", "production increase",
}
# News-dislocation is intentionally narrow: it is a rebound setup after a
# sentiment/positioning shock, never a way to average into fundamental damage.
# These terms are evaluated only against normalized RSS/email/news-packet
# evidence; quote and reversal checks remain deterministic.
DISLOCATION_TRANSIENT_WORDS = {
    "analyst", "downgrade", "rating", "price target", "valuation", "sentiment",
    "profit taking", "technical", "overreaction", "headline", "tariff",
    "trade war", "macro", "sector rotation", "short seller",
}
FUNDAMENTAL_DAMAGE_WORDS = {
    "fraud", "accounting irregular", "accounting investigation", "restatement",
    "bankruptcy", "insolvency", "liquidity crisis", "going concern",
    "delisting", "sec charges", "criminal investigation", "data breach",
    "guidance cut", "withdraws guidance", "dividend suspension",
}
EVENT_RISK_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"\bearnings announcement\b",
        r"\bearnings call\b",
        r"\breport(?:s|ing)? (?:after|before) (?:the )?(?:close|open)\b",
        r"\bQ[1-4]\s+\d{4}\s+earnings\b",
        r"\bAMC\b",
        r"\bBMO\b",
    ]
]
CATALYST_EVENT_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"\bearnings\b",
        r"\bguidance\b",
        r"\banalyst day\b",
        r"\binvestor day\b",
        r"\bconference\b",
        r"\bFDA\b",
        r"\bPDUFA\b",
        r"\bapproval\b",
        r"\bproduct launch\b",
        r"\bmerger\b",
        r"\bacquisition\b",
    ]
]
CONFIRMATION_ORDER = ["opening", "one-hour", "midday", "late-day", "friday-closeout", "after-close"]
CONFIRMATION_TIMES = {
    "opening": dt.time(8, 40),
    "one-hour": dt.time(9, 40),
    "midday": dt.time(11, 40),
    "late-day": dt.time(14, 40),
    "friday-closeout": dt.time(14, 50),
    "after-close": dt.time(15, 10),
}


@dataclass
class Quote:
    ticker: str
    price: float | None
    prev_close: float | None
    day_change_pct: float | None
    open_price: float | None
    volume: int | None
    avg_volume_10d: float | None
    volume_ratio: float | None
    last_timestamp: int | None
    error: str | None = None


@dataclass
class Intraday:
    ticker: str
    price: float | None
    session_open: float | None
    return_15m_pct: float | None
    return_30m_pct: float | None
    from_open_pct: float | None
    error: str | None = None


@dataclass
class TradePlan:
    max_entry: float
    stop: float
    stop_limit: float
    cushion_pct: float
    limit_gap_pct: float
    trailing_buy_offset: float
    trailing_buy_offset_pct: float
    notes: list[str]


def open_positions_from_file(path: Path, default_broker: str) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    positions = data.get("positions", {})
    if not isinstance(positions, dict):
        return {}
    return {
        str(ticker).upper(): {**details, "broker": details.get("broker") or default_broker}
        for ticker, details in positions.items()
        if isinstance(details, dict) and details.get("status") == "open"
    }


def position_file_generated_at(path: Path) -> dt.datetime | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return parsed_datetime(data.get("generated_at"))


def manual_closed_position_times(path: Path) -> dict[str, dt.datetime]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    positions = data.get("positions", {}) if isinstance(data, dict) else {}
    if not isinstance(positions, dict):
        return {}
    closed: dict[str, dt.datetime] = {}
    for ticker, details in positions.items():
        if not isinstance(details, dict) or details.get("status") != "closed":
            continue
        closed_at = parsed_datetime(details.get("closed_at"))
        if closed_at:
            closed[str(ticker).upper()] = closed_at
    return closed


def merge_position(existing: dict | None, incoming: dict) -> dict:
    if existing is None:
        result = dict(incoming)
        result["brokers"] = sorted(set(incoming.get("brokers", [])) | {str(incoming.get("broker") or "unknown")})
        return result

    result = dict(existing)
    old_shares = existing.get("shares")
    new_shares = incoming.get("shares")
    old_price = position_entry_price(existing)
    new_price = position_entry_price(incoming)
    if isinstance(old_shares, (int, float)) and isinstance(new_shares, (int, float)):
        total = float(old_shares) + float(new_shares)
        result["shares"] = total
        if total and old_price is not None and new_price is not None:
            result["avg_price"] = ((float(old_shares) * old_price) + (float(new_shares) * new_price)) / total
        elif old_price is None and new_price is not None:
            result["avg_price"] = new_price

    result["brokers"] = sorted(
        set(existing.get("brokers", []))
        | set(incoming.get("brokers", []))
        | {str(existing.get("broker") or "unknown"), str(incoming.get("broker") or "unknown")}
    )
    result["broker"] = "+".join(result["brokers"])
    return result


def read_positions(position_brokers: set[str] | None = None) -> dict[str, dict]:
    position_brokers = POSITION_BROKERS if position_brokers is None else position_brokers
    merged: dict[str, dict] = {}
    live_schwab_snapshot = SCHWAB_POSITIONS_FILE.exists()
    manual_closed = manual_closed_position_times(POSITIONS_FILE)
    for path, broker in ((POSITIONS_FILE, "manual"), (ROBINHOOD_POSITIONS_FILE, "robinhood"), (SCHWAB_POSITIONS_FILE, "schwab")):
        generated_at = position_file_generated_at(path)
        for ticker, details in open_positions_from_file(path, broker).items():
            closed_at = manual_closed.get(ticker)
            if path != POSITIONS_FILE and closed_at and generated_at and generated_at <= closed_at:
                continue
            detail_broker = str(details.get("broker") or broker).lower()
            detail_brokers = {
                str(item).lower()
                for item in details.get("brokers", [])
                if isinstance(item, str) and item.strip()
            } | {detail_broker}
            if position_brokers and "all" not in position_brokers and not (position_brokers & detail_brokers):
                continue
            if path == POSITIONS_FILE and live_schwab_snapshot and details.get("broker") == "schwab":
                continue
            merged[ticker] = merge_position(merged.get(ticker), details)
    for ticker in merged:
        if ticker not in TICKER_SECTOR:
            TICKER_SECTOR[ticker] = "consumer"
    return merged


def read_all_broker_positions() -> dict[str, dict]:
    return read_positions({"schwab"})


def read_json_object(path: Path, default: dict | None = None) -> dict:
    if not path.exists():
        return dict(default or {})
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(default or {})
    return data if isinstance(data, dict) else dict(default or {})


def refresh_robinhood_research_snapshot(symbols: list[str], now: dt.datetime) -> tuple[dict, list[str]]:
    if not ROBINHOOD_RESEARCH_ENABLED:
        return {}, ["Robinhood research disabled for this confirmation profile"]
    current = read_json_object(ROBINHOOD_RESEARCH_FILE)
    if not ROBINHOOD_ORDER_REVIEW_ENABLED and current and not stale_timestamp(current.get("generated_at"), now, 1):
        return current, []
    generated_at = parsed_datetime(current.get("generated_at")) if current else None
    max_age = dt.timedelta(hours=ROBINHOOD_RESEARCH_MAX_AGE_HOURS)
    requested = {
        str(symbol).upper()
        for symbol in (current.get("requested_symbols") or [])
        if isinstance(symbol, str)
    }
    needed = {symbol.upper() for symbol in symbols if symbol.upper() not in KNOWN_ETFS}
    if generated_at and now - generated_at <= max_age:
        missing = sorted(needed - requested)
        if missing:
            return current, [f"Robinhood research snapshot fresh; skipped slow refresh for missing symbols: {', '.join(missing[:12])}"]
        return current, []
    try:
        snapshot = market_robinhood_research.collect(sorted(needed))
        market_robinhood_research.write_snapshot(snapshot)
        return snapshot, []
    except Exception as exc:  # noqa: BLE001 - confirmation can continue with stale/no research.
        if current:
            return current, [f"Robinhood research refresh failed; using prior snapshot: {type(exc).__name__}: {exc}"]
        return {}, [f"Robinhood research unavailable: {type(exc).__name__}: {exc}"]


def robinhood_research_symbols(now: dt.datetime | None = None) -> set[str]:
    if not ROBINHOOD_RESEARCH_ENABLED:
        return set()
    now = now or dt.datetime.now(TZ)
    data = read_json_object(ROBINHOOD_RESEARCH_FILE)
    if not data or stale_timestamp(data.get("generated_at"), now, 1):
        return set()
    candidates = ((data.get("scanners") or {}).get("candidates") or {})
    symbols = [
        str(ticker).upper()
        for ticker in candidates
        if re.fullmatch(r"[A-Z0-9.-]{1,15}", str(ticker).upper())
    ]
    upcoming = []
    for ticker, rows in ((data.get("earnings") or {}).get("by_symbol") or {}).items():
        if isinstance(rows, list) and rows:
            upcoming.append(str(ticker).upper())
    return set(sorted(symbols + upcoming)[:ROBINHOOD_RESEARCH_CANDIDATE_LIMIT])


def latest_earnings_event(rows: object, now: dt.datetime) -> dict | None:
    if not isinstance(rows, list):
        return None
    parsed_rows = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        report = row.get("report") if isinstance(row.get("report"), dict) else {}
        event_date = parsed_datetime(report.get("date"))
        if event_date:
            parsed_rows.append((abs((event_date.date() - now.date()).days), row))
    if not parsed_rows:
        return None
    parsed_rows.sort(key=lambda item: item[0])
    return parsed_rows[0][1]


def research_adjustment_for_ticker(ticker: str, snapshot: dict, now: dt.datetime) -> tuple[int, list[str], list[str]]:
    if not snapshot:
        return 0, [], []
    score = 0
    evidence: list[str] = []
    blockers: list[str] = []

    scanner = ((snapshot.get("scanners") or {}).get("candidates") or {}).get(ticker)
    if isinstance(scanner, dict):
        titles = [str(title) for title in (scanner.get("scan_titles") or [])[:3]]
        if titles:
            score += 1
            evidence.append(f"Robinhood scanner match: {', '.join(titles)}")

    fundamentals = (snapshot.get("fundamentals") or {}).get(ticker)
    if isinstance(fundamentals, dict):
        market_cap = market_number(fundamentals.get("market_cap"))
        avg_volume = market_number(fundamentals.get("average_volume") or fundamentals.get("average_volume_30_days"))
        sector = str(fundamentals.get("sector") or "").strip()
        industry = str(fundamentals.get("industry") or "").strip()
        if market_cap is not None:
            evidence.append(f"Robinhood fundamentals: market cap {money(market_cap)}")
            if market_cap < 50_000_000 and ticker not in explicit_watchlist_symbols():
                blockers.append("Robinhood fundamentals show microcap market cap below $50M")
        if avg_volume is not None and avg_volume >= 100_000:
            evidence.append(f"Robinhood fundamentals: avg volume {num(avg_volume)}")
        elif avg_volume is not None and ticker not in explicit_watchlist_symbols():
            blockers.append(f"Robinhood fundamentals show thin average volume: {num(avg_volume)}")
        if sector or industry:
            evidence.append(f"Robinhood classification: {sector or 'unknown sector'} / {industry or 'unknown industry'}")

    technicals = (snapshot.get("technicals") or {}).get(ticker)
    if isinstance(technicals, dict):
        rsi = market_number(technicals.get("rsi"))
        atr = market_number(technicals.get("atr"))
        macd = technicals.get("macd")
        if rsi is not None:
            if 45 <= rsi <= 68:
                score += 1
                evidence.append(f"Robinhood RSI constructive: {rsi:.1f}")
            elif rsi > 75:
                blockers.append(f"Robinhood RSI overbought: {rsi:.1f}")
            elif rsi < 35:
                blockers.append(f"Robinhood RSI weak: {rsi:.1f}")
        if atr is not None and atr > 0:
            evidence.append(f"Robinhood ATR14: {atr:.2f}")
        if isinstance(macd, dict):
            macd_value = market_number(macd.get("macd") or macd.get("value"))
            signal = market_number(macd.get("signal"))
            if macd_value is not None and signal is not None:
                if macd_value >= signal:
                    score += 1
                    evidence.append("Robinhood MACD is above signal")
                else:
                    blockers.append("Robinhood MACD is below signal")

    earnings_rows = ((snapshot.get("earnings") or {}).get("by_symbol") or {}).get(ticker)
    event = latest_earnings_event(earnings_rows, now)
    if isinstance(event, dict):
        report = event.get("report") if isinstance(event.get("report"), dict) else {}
        event_date = parsed_datetime(report.get("date"))
        eps = event.get("eps") if isinstance(event.get("eps"), dict) else {}
        estimate = market_number(eps.get("estimate"))
        actual = market_number(eps.get("actual"))
        if event_date:
            days = (event_date.date() - now.date()).days
            timing = str(report.get("timing") or "").upper()
            if 0 <= days <= 3 and actual is None:
                blockers.append(f"Robinhood earnings risk: reports {event_date.date().isoformat()} {timing}".rstrip())
            elif -2 <= days <= 1 and actual is not None and estimate is not None:
                surprise = actual - estimate
                if surprise >= 0:
                    score += 1
                    evidence.append(f"Robinhood earnings result: EPS {actual:.2f} vs {estimate:.2f}")
                else:
                    blockers.append(f"Robinhood earnings miss: EPS {actual:.2f} vs {estimate:.2f}")

    account_research = snapshot.get("account_research") or {}
    tax_lots = (account_research.get("equity_tax_lots") or {}).get(ticker) if isinstance(account_research, dict) else None
    if isinstance(tax_lots, dict):
        lot_count = market_number(tax_lots.get("lot_count"))
        short_qty = market_number(tax_lots.get("short_term_quantity"))
        long_qty = market_number(tax_lots.get("long_term_quantity"))
        parts = [f"{int(lot_count)} lot(s)" if lot_count is not None else "tax lots available"]
        if short_qty:
            parts.append(f"short-term qty {short_qty:g}")
        if long_qty:
            parts.append(f"long-term qty {long_qty:g}")
        evidence.append(f"Robinhood tax-lot context: {', '.join(parts)}")
    option_watchlist = account_research.get("option_watchlist") if isinstance(account_research, dict) else []
    if isinstance(option_watchlist, list):
        option_hits = [
            item for item in option_watchlist if isinstance(item, dict) and str(item.get("chain_symbol") or item.get("underlying_symbol") or "").upper() == ticker
        ]
        if option_hits:
            evidence.append(f"Robinhood option watchlist interest: {len(option_hits)} contract(s)")

    return score, evidence, blockers


def robinhood_confirm_call(tool_name: str, args: dict, timeout: int = 30) -> dict:
    completed = subprocess.run(
        ["node", str(ROBINHOOD_CLIENT), "call", tool_name, json.dumps(args)],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
        raise RuntimeError(f"{tool_name} failed: {detail[:400]}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{tool_name} returned invalid JSON: {completed.stdout[:200]}") from exc


def agentic_account_number() -> tuple[str | None, str]:
    if not ROBINHOOD_CLIENT.exists():
        return None, f"missing Robinhood client: {ROBINHOOD_CLIENT}"
    try:
        response = robinhood_confirm_call("get_accounts", {})
    except Exception as exc:  # noqa: BLE001
        return None, f"Robinhood account lookup failed: {type(exc).__name__}: {exc}"
    active = [
        account
        for account in ((response.get("data") or {}).get("accounts") or [])
        if isinstance(account, dict)
        and account.get("state") == "active"
        and not account.get("deactivated")
        and not account.get("permanently_deactivated")
    ]
    agentic = [account for account in active if account.get("agentic_allowed")]
    if not agentic:
        return None, "no active Robinhood Agentic account available for order review"
    if len(agentic) > 1:
        return None, "multiple Robinhood Agentic accounts available; order review skipped to avoid defaulting"
    account_number = str(agentic[0].get("account_number") or "")
    return (account_number or None), ("ok" if account_number else "Agentic account number missing")


def flatten_review_messages(value: object, limit: int = 8) -> list[str]:
    messages: list[str] = []

    def visit(item: object, path: str = "") -> None:
        if len(messages) >= limit:
            return
        if isinstance(item, dict):
            for key, nested in item.items():
                key_text = str(key).lower()
                if key_text in {"account_number", "id", "order_id", "ref_id"}:
                    continue
                if key_text in {"alert", "alerts", "warning", "warnings", "error", "errors", "reject", "rejections", "message", "detail", "reason"}:
                    visit(nested, key_text)
                elif isinstance(nested, (dict, list)):
                    visit(nested, key_text)
        elif isinstance(item, list):
            for nested in item:
                visit(nested, path)
        elif isinstance(item, str) and item.strip():
            messages.append(re.sub(r"\s+", " ", item.strip())[:220])

    visit(value)
    return list(dict.fromkeys(messages))[:limit]


def robinhood_position_shares(ticker: str) -> float | None:
    positions = open_positions_from_file(ROBINHOOD_POSITIONS_FILE, "robinhood")
    position = positions.get(ticker)
    return market_number(position.get("shares")) if isinstance(position, dict) else None


def review_equity_order_rows(
    rows: list[tuple[str, int, str, str, list[str], list[str]]],
    trade_plans: dict[str, TradePlan],
) -> tuple[list[tuple[str, int, str, str, list[str], list[str]]], list[str]]:
    if not ROBINHOOD_ORDER_REVIEW_ENABLED:
        return rows, ["Robinhood equity order review disabled"]
    actionable = {"predictive_buy", "aggressive_buy", "early_buy", "buy", "sell"}
    if not any(row[0] in actionable for row in rows):
        return rows, []
    account_number, status = agentic_account_number()
    if not account_number:
        return rows, [status]

    updated = []
    notes: list[str] = []
    for action, score, ticker, decision, evidence, blockers in rows:
        if action not in actionable:
            updated.append((action, score, ticker, decision, evidence, blockers))
            continue
        side = "sell" if action == "sell" else "buy"
        args = {
            "account_number": account_number,
            "symbol": ticker,
            "side": side,
            "time_in_force": "gfd",
            "market_hours": "regular_hours",
        }
        if side == "buy":
            plan = trade_plans.get(ticker)
            if not plan:
                updated.append((action, score, ticker, decision, evidence, blockers))
                continue
            args.update({"type": "limit", "quantity": "1", "limit_price": f"{plan.max_entry:.2f}"})
        else:
            shares = robinhood_position_shares(ticker)
            if shares is None or shares < 1:
                updated.append((action, score, ticker, decision, evidence, blockers))
                continue
            args.update({"type": "market", "quantity": str(int(math.floor(shares)))})
        try:
            response = robinhood_confirm_call("review_equity_order", args)
        except Exception as exc:  # noqa: BLE001
            message = f"Robinhood {side} order review failed: {type(exc).__name__}: {exc}"
            notes.append(f"{ticker}: {message}")
            if side == "buy":
                updated.append(("no_buy", score, ticker, f"NO BUY: {ticker} - Robinhood order review did not clear", evidence, [*blockers, message]))
            else:
                updated.append((action, score, ticker, decision, evidence, [*blockers, message]))
            continue
        messages = flatten_review_messages(response.get("data") or response)
        if messages:
            notes.append(f"{ticker}: {'; '.join(messages[:3])}")
            if side == "buy":
                updated.append(("no_buy", score, ticker, f"NO BUY: {ticker} - Robinhood order review returned alerts", evidence, [*blockers, *[f"Robinhood order review: {m}" for m in messages[:4]]]))
            else:
                updated.append((action, score, ticker, decision, [*evidence, *[f"Robinhood sell review: {m}" for m in messages[:4]]], blockers))
        else:
            updated.append((action, score + (1 if side == "buy" else 0), ticker, decision, [*evidence, f"Robinhood {side} order review cleared"], blockers))
            notes.append(f"{ticker}: Robinhood {side} order review cleared")
    return updated, notes


def same_session_key(now: dt.datetime) -> str:
    return now.astimezone(TZ).date().isoformat()


def manual_trade_symbols_for_session(now: dt.datetime) -> set[str]:
    data = read_json_object(POSITIONS_FILE, {"events": []})
    traded: set[str] = set()
    session_key = same_session_key(now)
    for event in data.get("events") or []:
        if not isinstance(event, dict) or event.get("type") not in {"buy", "close"}:
            continue
        recorded_at = parsed_datetime(event.get("recorded_at"))
        ticker = str(event.get("ticker") or "").upper()
        if ticker and recorded_at and same_session_key(recorded_at) == session_key:
            traded.add(ticker)
    return traded


def report_open_position_symbols(report_path: Path) -> set[str]:
    try:
        text = report_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    section = re.search(r"(?ms)^## open_positions\n(.*?)(?:\n## |\Z)", text)
    if not section:
        return set()
    symbols: set[str] = set()
    for line in section.group(1).splitlines():
        match = re.match(r"-\s+([A-Z][A-Z0-9.]*)\s*:", line)
        if match:
            symbols.add(match.group(1).upper())
    return symbols


def position_change_symbols_for_session(now: dt.datetime, current_open_positions: dict[str, dict]) -> set[str]:
    snapshots: list[set[str]] = []
    session_prefix = same_session_key(now)
    for path in sorted(OUT_DIR.glob(f"{session_prefix}T*-market-confirm-*.md")):
        symbols = report_open_position_symbols(path)
        if symbols:
            snapshots.append(symbols)
    snapshots.append(set(current_open_positions))

    changed: set[str] = set()
    previous: set[str] | None = None
    for symbols in snapshots:
        if previous is not None:
            changed.update(previous.symmetric_difference(symbols))
        previous = symbols
    return changed


def same_session_traded_symbols(now: dt.datetime, current_open_positions: dict[str, dict]) -> set[str]:
    return manual_trade_symbols_for_session(now) | position_change_symbols_for_session(now, current_open_positions)


def write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def parsed_datetime(value: object) -> dt.datetime | None:
    try:
        text = str(value).strip()
        if not text:
            return None
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=TZ)
        return parsed.astimezone(TZ)
    except (TypeError, ValueError):
        return None


def stale_timestamp(value: object, now: dt.datetime, days: int) -> bool:
    parsed = parsed_datetime(value)
    return parsed is None or now - parsed > dt.timedelta(days=days)


def idea_screen_symbols(now: dt.datetime | None = None) -> set[str]:
    now = now or dt.datetime.now(TZ)
    data = read_json_object(IDEA_SCREEN_FILE)
    if stale_timestamp(data.get("generated_at"), now, IDEA_SCREEN_REFRESH_DAYS * 2):
        return set()
    candidates = data.get("candidates") or {}
    if not isinstance(candidates, dict):
        return set()
    symbols = set()
    for ticker, row in candidates.items():
        if isinstance(row, dict) and market_number(row.get("score")) is not None and market_number(row.get("score")) >= 3:
            symbols.add(str(ticker).upper())
    return symbols


def catalyst_calendar_symbols(now: dt.datetime | None = None) -> set[str]:
    now = now or dt.datetime.now(TZ)
    data = read_json_object(CATALYST_CALENDAR_FILE)
    events = data.get("events") or {}
    if not isinstance(events, dict):
        return set()
    symbols: dict[str, dt.datetime] = {}
    for ticker, rows in events.items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            seen = parsed_datetime(row.get("detected_at"))
            event_date = parsed_datetime(row.get("event_date"))
            if seen and now - seen <= dt.timedelta(days=CATALYST_CALENDAR_MAX_AGE_DAYS):
                symbols[str(ticker).upper()] = max(symbols.get(str(ticker).upper(), seen), seen)
                break
            if event_date and abs((event_date.date() - now.date()).days) <= CATALYST_CALENDAR_MAX_AGE_DAYS:
                symbols[str(ticker).upper()] = max(symbols.get(str(ticker).upper(), event_date), event_date)
                break
    ranked = sorted(symbols.items(), key=lambda item: (item[1], item[0]), reverse=True)
    return {ticker for ticker, _seen in ranked[:CATALYST_CALENDAR_SYMBOL_LIMIT]}


def read_robinhood_spending_power() -> tuple[float | None, str]:
    if not ROBINHOOD_POSITIONS_FILE.exists():
        return None, "Robinhood spending power snapshot missing"
    try:
        data = json.loads(ROBINHOOD_POSITIONS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, "Robinhood spending power snapshot unreadable"
    value = market_number(data.get("spending_power"))
    if value is not None:
        currency = str(data.get("spending_power_currency") or "USD")
        return value, currency

    account_values = []
    for account in data.get("accounts") or []:
        if not isinstance(account, dict):
            continue
        if account.get("agentic_allowed") is False:
            continue
        account_value = market_number(account.get("buying_power"))
        if account_value is not None:
            account_values.append(account_value)
    if account_values:
        return min(account_values), "USD"
    return None, "Robinhood spending power unavailable in latest snapshot"


def read_schwab_buying_power() -> tuple[float | None, str]:
    if not SCHWAB_BALANCES_FILE.exists():
        return None, "Schwab balance snapshot missing"
    try:
        data = json.loads(SCHWAB_BALANCES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, "Schwab balance snapshot unreadable"
    if data.get("has_restriction"):
        return None, "Schwab balance snapshot shows account restriction/call"
    for key, label in (
        ("max_stock_buying_power", "stock buying power"),
        ("max_buying_power", "buying power"),
        ("max_available_funds", "available funds"),
        ("max_cash_available_for_trading", "cash available for trading"),
        ("max_available_funds_non_marginable_trade", "available funds for non-marginable trades"),
    ):
        value = market_number(data.get(key))
        if value is not None:
            return value, f"Schwab {label}"
    return None, "Schwab balance snapshot lacks usable buying-power fields"


def http_get(url: str, timeout: int = 18) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/json,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, identity",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(1_000_000)
        enc = (resp.headers.get("Content-Encoding") or "").lower()
        if enc == "gzip" or raw.startswith(b"\x1f\x8b"):
            raw = gzip.decompress(raw)
        elif enc == "deflate":
            raw = zlib.decompress(raw)
        return raw


def fetch_json(url: str, timeout: int = 18) -> dict:
    return json.loads(http_get(url, timeout).decode("utf-8", errors="replace"))


def latest_packets(limit: int = 3) -> list[Path]:
    packets = sorted(DATA_ROOT.glob("*-market-packet-*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return packets[:limit]


def packet_text(packet: Path) -> str:
    json_path = packet.with_suffix(".json")
    if not json_path.exists():
        return packet.read_text(encoding="utf-8", errors="replace")
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return packet.read_text(encoding="utf-8", errors="replace")

    lines = [
        f"source: {data.get('source', '')}",
        f"generated_at: {data.get('generated_at', '')}",
    ]
    for item in data.get("source_starting_points", []):
        lines.append(f"url: {item.get('final_url') or item.get('url') or ''}")
        lines.append(f"affected_tickers: {', '.join(item.get('tickers', [])) if item.get('tickers') else 'not detected'}")
        for snippet in item.get("snippets", []):
            lines.append(f"snippet: {snippet}")

    extracted = data.get("extracted_facts", {})
    if isinstance(extracted, dict):
        for fact in extracted.get("fallback_facts", []):
            lines.append(
                "fact: "
                f"affected_tickers: {', '.join(fact.get('affected_tickers', []))}; "
                f"source: {fact.get('source', '')}; "
                f"{fact.get('fact', '')}"
            )
        extraction_text = extracted.get("extraction_text") or extracted.get("ollama_text")
        if extraction_text:
            lines.append(str(extraction_text))
    return "\n".join(lines)


def ticker_mentioned(text: str, ticker: str) -> bool:
    escaped = re.escape(ticker)
    if ticker in AMBIGUOUS_TICKERS:
        return bool(
            re.search(
                rf"(?:\${escaped}\b|\b(?:NASDAQ|NYSE|AMEX|OTC)\s*:\s*{escaped}\b|\({escaped}\)|\b{escaped}\s+[+-]?\d+(?:\.\d+)?%)",
                text,
                flags=re.IGNORECASE,
            )
        )
    return bool(re.search(rf"\b{escaped}\b", text, flags=re.IGNORECASE))


def packet_mentions(tickers: list[str] | None = None) -> dict[str, list[str]]:
    text = "\n".join(packet_text(p) for p in latest_packets())
    tickers = tickers or ALL_TICKERS
    mentions: dict[str, list[str]] = {ticker: [] for ticker in tickers}
    for ticker in tickers:
        pattern = (
            rf"(?:\${re.escape(ticker)}\b|\b(?:NASDAQ|NYSE|AMEX|OTC)\s*:\s*{re.escape(ticker)}\b|\({re.escape(ticker)}\)|\b{re.escape(ticker)}\s+[+-]?\d+(?:\.\d+)?%)"
            if ticker in AMBIGUOUS_TICKERS
            else rf"\b{re.escape(ticker)}\b"
        )
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            start = max(0, match.start() - 220)
            end = min(len(text), match.end() + 360)
            snippet = re.sub(r"\s+", " ", text[start:end]).strip()
            generic_nav = (
                "ADVERTISEMENT" in snippet
                or "Stock comparison" in snippet
                or "Recently viewed" in snippet
                or "Portfolio AlphaSpace" in snippet
                or "Celebrity TV Movies" in snippet
                or re.search(r"\b[2-9]d ago\b", snippet, flags=re.IGNORECASE)
            )
            fact_text = re.split(r"\b(?:snippet|fact):", snippet, flags=re.IGNORECASE)[-1]
            grouped_ticker_only = "affected_tickers:" in snippet and not re.search(rf"\b{re.escape(ticker)}\b", fact_text, flags=re.IGNORECASE)
            if generic_nav:
                continue
            if grouped_ticker_only:
                continue
            if snippet and snippet not in mentions[ticker]:
                mentions[ticker].append(snippet[:420])
    return {ticker: snippets[:3] for ticker, snippets in mentions.items() if snippets}


def packet_news_candidates(now: dt.datetime | None = None) -> dict[str, str]:
    """Return current Tech/Energy feed candidates, expiring stale coverage.

    Discovery is intentionally broader than the fixed watchlist.  It is still
    bounded by a recent-news horizon, and later liquidity, source-verification,
    event-risk, and entry checks remain mandatory before any BUY candidate.
    """
    now = now or dt.datetime.now(TZ)
    cutoff = now - dt.timedelta(days=FEED_CANDIDATE_HORIZON_DAYS)
    category_sector = {
        "Technology": "tech",
        "Energy": "energy",
    }
    candidates: dict[str, tuple[str, dt.datetime]] = {}
    packets = sorted(DATA_ROOT.glob("*-market-packet-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for json_path in packets[:FEED_CANDIDATE_PACKET_LIMIT]:
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        generated = parsed_datetime(data.get("generated_at"))
        if generated is None or generated < cutoff:
            continue
        for item in data.get("source_starting_points", []):
            if not isinstance(item, dict):
                continue
            # Static category landing pages are refreshed every packet and
            # would otherwise keep old names alive indefinitely. Only an
            # individual RSS/Atom entry can renew this temporary universe.
            if item.get("source_type") != "rss_atom":
                continue
            sector = category_sector.get(str(item.get("category") or ""))
            if sector is None:
                continue
            for ticker in item.get("tickers", []):
                ticker = str(ticker).upper().strip()
                if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", ticker) or ticker in AMBIGUOUS_TICKERS:
                    continue
                prior = candidates.get(ticker)
                if prior is None or generated > prior[1]:
                    candidates[ticker] = (sector, generated)
    ranked = sorted(candidates.items(), key=lambda item: (item[1][1], item[0]), reverse=True)
    return {ticker: sector for ticker, (sector, _seen) in ranked[:NEWS_CANDIDATE_LIMIT]}


def source_candidates_from_packets(tickers: list[str], limit: int = 3) -> dict[str, list[dict]]:
    """Collect supporting snippets, retaining feed evidence for the same horizon."""
    wanted = set(tickers)
    candidates: dict[str, list[dict]] = {ticker: [] for ticker in tickers}
    now = dt.datetime.now(TZ)
    cutoff = now - dt.timedelta(days=FEED_CANDIDATE_HORIZON_DAYS)
    packets = sorted(DATA_ROOT.glob("*-market-packet-*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    for packet in packets[:FEED_CANDIDATE_PACKET_LIMIT]:
        json_path = packet.with_suffix(".json")
        if not json_path.exists():
            continue
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        packet_generated = parsed_datetime(data.get("generated_at"))
        if packet_generated is None or packet_generated < cutoff:
            continue
        for item in data.get("source_starting_points", []):
            source = item.get("final_url") or item.get("url") or ""
            for snippet in item.get("snippets", []):
                add_source_candidate(candidates, wanted, source, str(snippet), packet.name, data.get("generated_at") or "")
        extracted = data.get("extracted_facts", {})
        if isinstance(extracted, dict):
            for fact in extracted.get("fallback_facts", []):
                source = fact.get("source") or ""
                add_source_candidate(candidates, wanted, source, str(fact.get("fact", "")), packet.name, data.get("generated_at") or "")
    return {ticker: items[:2] for ticker, items in candidates.items() if items}


def explicit_watchlist_symbols(limit: int = 3) -> set[str]:
    symbols = set(ALL_TICKERS)
    for packet in latest_packets(limit):
        json_path = packet.with_suffix(".json")
        if not json_path.exists():
            continue
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        context = data.get("robinhood_context")
        if not isinstance(context, dict):
            continue
        for symbol in context.get("symbols") or []:
            ticker = str(symbol).upper().strip()
            if re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", ticker):
                symbols.add(ticker)
        for watchlist in context.get("lists") or []:
            if not isinstance(watchlist, dict) or watchlist.get("is_sector"):
                continue
            display_name = str(watchlist.get("display_name") or "").casefold()
            if "crypto" in display_name:
                continue
            for symbol in watchlist.get("symbols") or []:
                ticker = str(symbol).upper().strip()
                if re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", ticker):
                    symbols.add(ticker)
    return symbols


def add_source_candidate(
    candidates: dict[str, list[dict]],
    wanted: set[str],
    source: str,
    text: str,
    packet_name: str,
    packet_generated: str,
) -> None:
    compact = re.sub(r"\s+", " ", text).strip()
    if not source or not compact:
        return
    lower = compact.lower()
    if not any(word in lower for word in CATALYST_WORDS):
        return
    for ticker in wanted:
        if not ticker_mentioned(compact, ticker):
            continue
        item = {
            "ticker": ticker,
            "source": source,
            "packet": packet_name,
            "packet_generated_at": packet_generated,
            "snippet": compact[:700],
        }
        dedupe = (item["source"], item["snippet"][:140])
        existing = {(row["source"], row["snippet"][:140]) for row in candidates[ticker]}
        if dedupe not in existing:
            candidates[ticker].append(item)


def strip_html(raw: str) -> str:
    raw = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", raw)
    raw = re.sub(r"(?is)<[^>]+>", " ", raw)
    return html.unescape(re.sub(r"\s+", " ", raw)).strip()


def page_title(raw: str) -> str:
    og = re.search(r'(?is)<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)', raw)
    if og:
        return html.unescape(og.group(1).strip())
    title = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw)
    return html.unescape(re.sub(r"\s+", " ", title.group(1)).strip()) if title else ""


def canonical_url(raw: str) -> str:
    canonical = re.search(r'(?is)<link\s+rel=["\']canonical["\']\s+href=["\']([^"\']+)', raw)
    return html.unescape(canonical.group(1).strip()) if canonical else ""


def generic_source_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    # StocksToTrade's editorial list is a useful lead generator, but its own
    # watchlist copy is not independent verification of a trade catalyst.
    if parsed.netloc.lower().endswith("stockstotrade.com"):
        return True
    path = parsed.path.rstrip("/").lower()
    generic_paths = {
        "",
        "/",
        "/markets",
        "/news",
        "/topic/tech",
        "/sectors/energy",
        "/markets/news",
        "/movers",
    }
    generic_fragments = (
        "/calendar",
        "/earnings",
        "/markets/stocks/most-active",
        "/markets/stocks/gainers",
        "/markets/stocks/losers",
        "/movers",
        "/sectors/",
        "/topic/",
    )
    return (
        path in generic_paths
        or path.endswith("/markets")
        or path.endswith("/news")
        or any(fragment in path for fragment in generic_fragments)
    )


def verify_source_candidate(candidate: dict) -> dict:
    ticker = candidate["ticker"]
    url = candidate["source"]
    record = {
        **candidate,
        "fetched_at": dt.datetime.now(TZ).isoformat(),
        "ok": False,
        "title": "",
        "canonical_url": "",
        "ticker_mentioned": False,
        "catalyst_terms_found": [],
        "generic_source": generic_source_url(url),
        "verified": False,
        "error": "",
    }
    if not url.startswith(("http://", "https://")):
        record["error"] = "source is not fetchable http(s)"
        return record
    try:
        raw = http_get(url, timeout=12).decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        record["error"] = f"{type(exc).__name__}: {exc}"
        return record

    text = strip_html(raw)
    lower_text = text.lower()
    terms = sorted({word for word in CATALYST_WORDS if word in lower_text})
    record.update(
        {
            "ok": True,
            "title": page_title(raw),
            "canonical_url": canonical_url(raw),
            "ticker_mentioned": ticker_mentioned(text, ticker),
            "catalyst_terms_found": terms[:12],
        }
    )
    record["verified"] = (
        record["ok"]
        and record["ticker_mentioned"]
        and bool(record["catalyst_terms_found"])
        and not record["generic_source"]
    )
    if not record["verified"] and not record["error"]:
        record["error"] = "downloaded source did not satisfy ticker/catalyst/specific-url verification"
    return record


def verify_sources(tickers: list[str]) -> dict[str, list[dict]]:
    candidates = source_candidates_from_packets(tickers)
    verified: dict[str, list[dict]] = {}
    for ticker, items in candidates.items():
        verified[ticker] = [verify_source_candidate(item) for item in items]
    return verified


def fetch_quote(ticker: str) -> Quote:
    enc = urllib.parse.quote(ticker)
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{enc}?range=10d&interval=5m&includePrePost=true"
    try:
        data = fetch_json(url)
        result = data["chart"]["result"][0]
        meta = result.get("meta", {})
        timestamps = result.get("timestamp") or []
        quote = (result.get("indicators", {}).get("quote") or [{}])[0]
        closes = [v for v in quote.get("close", []) if isinstance(v, (int, float)) and math.isfinite(v)]
        opens = [v for v in quote.get("open", []) if isinstance(v, (int, float)) and math.isfinite(v)]
        volumes = [v for v in quote.get("volume", []) if isinstance(v, int)]
        price = float(meta.get("regularMarketPrice")) if isinstance(meta.get("regularMarketPrice"), (int, float)) else (closes[-1] if closes else None)
        prev_close = float(meta.get("previousClose")) if isinstance(meta.get("previousClose"), (int, float)) else None
        day_pct = ((price - prev_close) / prev_close * 100) if price and prev_close else None
        current_day = dt.datetime.now(ET).date()
        day_volumes: dict[dt.date, int] = {}
        current_volume = 0
        for ts, vol in zip(timestamps, quote.get("volume", []), strict=False):
            if not isinstance(vol, int):
                continue
            day = dt.datetime.fromtimestamp(ts, ET).date()
            day_volumes[day] = day_volumes.get(day, 0) + vol
            if day == current_day:
                current_volume += vol
        past_days = [v for d, v in day_volumes.items() if d != current_day and v > 0]
        avg_volume = statistics.mean(past_days[-10:]) if past_days else None
        volume_ratio = (current_volume / avg_volume) if avg_volume and current_volume else None
        return Quote(
            ticker=ticker,
            price=price,
            prev_close=prev_close,
            day_change_pct=day_pct,
            open_price=opens[-1] if opens else None,
            volume=current_volume or (volumes[-1] if volumes else None),
            avg_volume_10d=avg_volume,
            volume_ratio=volume_ratio,
            last_timestamp=timestamps[-1] if timestamps else None,
        )
    except Exception as exc:  # noqa: BLE001 - confirmation report should include the fetch failure.
        return Quote(ticker=ticker, price=None, prev_close=None, day_change_pct=None, open_price=None, volume=None, avg_volume_10d=None, volume_ratio=None, last_timestamp=None, error=f"{type(exc).__name__}: {exc}")


def market_number(value: object) -> float | None:
    try:
        cleaned = str(value).replace("$", "").replace("%", "").replace(",", "").replace("+", "").strip()
        return float(cleaned) if cleaned and cleaned not in {"N/A", "--"} else None
    except (TypeError, ValueError):
        return None


def cached_daily_volume(ticker: str) -> float | None:
    try:
        data = json.loads(LIQUIDITY_CACHE_FILE.read_text(encoding="utf-8"))
        return market_number(((data.get("rows") or {}).get(ticker) or {}).get("volume"))
    except (OSError, json.JSONDecodeError):
        return None


def fetch_nasdaq_quote(ticker: str) -> Quote:
    try:
        info = None
        errors = []
        for asset_class in ("stocks", "etf"):
            url = f"https://api.nasdaq.com/api/quote/{urllib.parse.quote(ticker)}/info?assetclass={asset_class}"
            try:
                info = fetch_json(url, timeout=12).get("data")
            except Exception as exc:  # noqa: BLE001 - try the alternate asset class.
                errors.append(f"{asset_class}: {type(exc).__name__}: {exc}")
            if isinstance(info, dict):
                break
        if not isinstance(info, dict):
            raise ValueError("; ".join(errors) or "Nasdaq response missing quote data")
        primary = info.get("primaryData") or {}
        secondary = info.get("secondaryData") or {}
        market_status = str(info.get("marketStatus") or "")
        regular = secondary if market_status != "Market Open" and market_number(secondary.get("percentageChange")) is not None else primary
        price = market_number(regular.get("lastSalePrice"))
        day_pct = market_number(regular.get("percentageChange"))
        net_change = market_number(regular.get("netChange"))
        prev_close = price - net_change if price is not None and net_change is not None else None
        volume = market_number(primary.get("volume"))
        avg_volume = cached_daily_volume(ticker)
        volume_ratio = volume / avg_volume if volume and avg_volume else None
        if price is None or day_pct is None:
            raise ValueError("Nasdaq response missing price or percentage change")
        return Quote(
            ticker=ticker,
            price=price,
            prev_close=prev_close,
            day_change_pct=day_pct,
            open_price=None,
            volume=int(volume) if volume is not None else None,
            avg_volume_10d=avg_volume,
            volume_ratio=volume_ratio,
            last_timestamp=None,
        )
    except Exception as exc:  # noqa: BLE001 - caller can fall back to Yahoo.
        return Quote(ticker=ticker, price=None, prev_close=None, day_change_pct=None, open_price=None, volume=None, avg_volume_10d=None, volume_ratio=None, last_timestamp=None, error=f"Nasdaq quote failed: {type(exc).__name__}: {exc}")


def fetch_nasdaq_quotes(tickers: list[str]) -> dict[str, Quote]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        return dict(zip(tickers, pool.map(fetch_nasdaq_quote, tickers), strict=False))


def percent_change(current: float | None, prior: float | None) -> float | None:
    if current is None or prior is None or prior == 0:
        return None
    return (current - prior) / prior * 100


def fetch_nasdaq_intraday(ticker: str) -> Intraday:
    try:
        data = None
        errors = []
        for asset_class in ("stocks", "etf"):
            url = f"https://api.nasdaq.com/api/quote/{urllib.parse.quote(ticker)}/chart?assetclass={asset_class}"
            try:
                data = fetch_json(url, timeout=12).get("data")
            except Exception as exc:  # noqa: BLE001 - try alternate asset class.
                errors.append(f"{asset_class}: {type(exc).__name__}: {exc}")
            if isinstance(data, dict) and data.get("chart"):
                break
        if not isinstance(data, dict):
            raise ValueError("; ".join(errors) or "Nasdaq response missing chart data")

        points = []
        for point in data.get("chart") or []:
            timestamp_ms = point.get("x")
            value = market_number(point.get("y"))
            if not isinstance(timestamp_ms, (int, float)) or value is None:
                continue
            points.append((dt.datetime.fromtimestamp(timestamp_ms / 1000, ET), value))
        points.sort(key=lambda item: item[0])
        if not points:
            raise ValueError("Nasdaq response contained no usable chart points")

        latest_time, latest_price = points[-1]
        regular = [point for point in points if point[0].time() >= dt.time(9, 30)]
        session_open = regular[0][1] if regular else points[0][1]

        def price_at_or_before(minutes: int) -> float | None:
            cutoff = latest_time - dt.timedelta(minutes=minutes)
            eligible = [value for timestamp, value in points if timestamp <= cutoff]
            return eligible[-1] if eligible else None

        return Intraday(
            ticker=ticker,
            price=latest_price,
            session_open=session_open,
            return_15m_pct=percent_change(latest_price, price_at_or_before(15)),
            return_30m_pct=percent_change(latest_price, price_at_or_before(30)),
            from_open_pct=percent_change(latest_price, session_open),
        )
    except Exception as exc:  # noqa: BLE001 - report should retain missing-data reason.
        return Intraday(
            ticker=ticker,
            price=None,
            session_open=None,
            return_15m_pct=None,
            return_30m_pct=None,
            from_open_pct=None,
            error=f"Nasdaq intraday failed: {type(exc).__name__}: {exc}",
        )


def fetch_nasdaq_intraday_batch(tickers: list[str]) -> dict[str, Intraday]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        return dict(zip(tickers, pool.map(fetch_nasdaq_intraday, tickers), strict=False))


def iso_timestamp(value: object) -> int | None:
    try:
        return int(dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return None


def fetch_robinhood_quotes(tickers: list[str], prefer_extended: bool = False) -> dict[str, Quote]:
    quotes: dict[str, Quote] = {}
    if not ROBINHOOD_QUOTES_ENABLED:
        return quotes
    if not ROBINHOOD_CLIENT.exists():
        return quotes
    for start in range(0, len(tickers), 20):
        chunk = tickers[start : start + 20]
        command = ["node", str(ROBINHOOD_CLIENT), "call", "get_equity_quotes", json.dumps({"symbols": chunk})]
        try:
            completed = subprocess.run(
                command,
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=ROBINHOOD_QUOTE_TIMEOUT,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}")
            payload = json.loads(completed.stdout)
            results = ((payload.get("data") or {}).get("results") or [])
            for result in results:
                row = result.get("quote") or {}
                ticker = str(row.get("symbol") or "").upper()
                if ticker not in chunk or not row.get("has_traded") or row.get("state") != "active":
                    continue
                regular_price = market_number(row.get("last_trade_price"))
                regular_ts = iso_timestamp(row.get("venue_last_trade_time"))
                extended_price = market_number(row.get("last_non_reg_trade_price"))
                extended_ts = iso_timestamp(row.get("venue_last_non_reg_trade_time"))
                use_extended = (
                    prefer_extended
                    and extended_price is not None
                    and extended_ts is not None
                    and (regular_ts is None or extended_ts > regular_ts)
                )
                price = extended_price if use_extended else regular_price
                last_ts = extended_ts if use_extended else regular_ts
                prev_close = market_number(row.get("adjusted_previous_close")) or market_number(row.get("previous_close"))
                day_pct = ((price - prev_close) / prev_close * 100) if price is not None and prev_close else None
                quotes[ticker] = Quote(
                    ticker=ticker,
                    price=price,
                    prev_close=prev_close,
                    day_change_pct=day_pct,
                    open_price=None,
                    volume=None,
                    avg_volume_10d=None,
                    volume_ratio=None,
                    last_timestamp=last_ts,
                )
        except Exception as exc:  # noqa: BLE001 - fallback quote sources remain available.
            for ticker in chunk:
                quotes.setdefault(
                    ticker,
                    Quote(ticker=ticker, price=None, prev_close=None, day_change_pct=None, open_price=None, volume=None, avg_volume_10d=None, volume_ratio=None, last_timestamp=None, error=f"Robinhood quote failed: {type(exc).__name__}: {exc}"),
                )
    return quotes


def schwab_quote_number(*values: object) -> float | None:
    for value in values:
        parsed = market_number(value)
        if parsed is not None:
            return parsed
    return None


def fetch_schwab_quotes(tickers: list[str], prefer_extended: bool = False) -> dict[str, Quote]:
    quotes: dict[str, Quote] = {}
    if not SCHWAB_QUOTES_ENABLED:
        return quotes
    try:
        config = market_schwab.load_env()
        payload = market_schwab.fetch_quotes(config, tickers)
    except Exception as exc:  # noqa: BLE001 - public quote fallbacks remain available.
        message = f"Schwab quote failed: {type(exc).__name__}: {exc}"
        return {
            ticker: Quote(ticker=ticker, price=None, prev_close=None, day_change_pct=None, open_price=None, volume=None, avg_volume_10d=None, volume_ratio=None, last_timestamp=None, error=message)
            for ticker in tickers
        }

    for ticker in tickers:
        raw = payload.get(ticker) or payload.get(ticker.upper())
        if not isinstance(raw, dict):
            continue
        quote = raw.get("quote") if isinstance(raw.get("quote"), dict) else {}
        regular = raw.get("regular") if isinstance(raw.get("regular"), dict) else {}
        fundamental = raw.get("fundamental") if isinstance(raw.get("fundamental"), dict) else {}
        extended_price = schwab_quote_number(quote.get("mark"), quote.get("lastPrice"))
        regular_price = schwab_quote_number(
            regular.get("regularMarketLastPrice"),
            quote.get("lastPrice"),
            quote.get("mark"),
        )
        price = extended_price if prefer_extended and extended_price is not None else regular_price
        prev_close = schwab_quote_number(quote.get("closePrice"), regular.get("regularMarketPreviousClose"))
        day_pct = schwab_quote_number(quote.get("netPercentChange"), regular.get("regularMarketPercentChange"))
        if day_pct is None:
            day_pct = percent_change(price, prev_close)
        volume = schwab_quote_number(quote.get("totalVolume"), regular.get("regularMarketTradeVolume"))
        avg_volume = schwab_quote_number(fundamental.get("avg10DaysVolume"), fundamental.get("avg1YearVolume"))
        volume_ratio = volume / avg_volume if volume and avg_volume else None
        last_timestamp = None
        for key in ("tradeTime", "quoteTime", "regularMarketTradeTime"):
            value = quote.get(key) if key in quote else regular.get(key)
            if isinstance(value, (int, float)):
                last_timestamp = int(value / 1000 if value > 10_000_000_000 else value)
                break
        if price is None:
            continue
        quotes[ticker] = Quote(
            ticker=ticker,
            price=price,
            prev_close=prev_close,
            day_change_pct=day_pct,
            open_price=schwab_quote_number(quote.get("openPrice"), regular.get("regularMarketOpenPrice")),
            volume=int(volume) if volume is not None else None,
            avg_volume_10d=avg_volume,
            volume_ratio=volume_ratio,
            last_timestamp=last_timestamp,
        )
    return quotes


def fetch_primary_quotes(tickers: list[str], prefer_extended: bool = False) -> dict[str, Quote]:
    schwab = fetch_schwab_quotes(tickers, prefer_extended=prefer_extended)
    nasdaq = fetch_nasdaq_quotes(tickers)
    quotes: dict[str, Quote] = {}
    for ticker in tickers:
        schwab_quote = schwab.get(ticker)
        nq_quote = nasdaq.get(ticker)
        if schwab_quote and schwab_quote.price is not None:
            if nq_quote:
                schwab_quote.volume = schwab_quote.volume or nq_quote.volume
                schwab_quote.avg_volume_10d = schwab_quote.avg_volume_10d or nq_quote.avg_volume_10d
                schwab_quote.volume_ratio = schwab_quote.volume_ratio or nq_quote.volume_ratio
            quotes[ticker] = schwab_quote
        elif nq_quote and nq_quote.price is not None:
            quotes[ticker] = nq_quote
        else:
            quotes[ticker] = fetch_quote(ticker)
    return quotes


def parse_float(value: str) -> float | None:
    try:
        if not value or value == "N/D":
            return None
        return float(value)
    except ValueError:
        return None


def parse_int(value: str) -> int | None:
    try:
        if not value or value == "N/D":
            return None
        return int(float(value))
    except ValueError:
        return None


def fetch_stooq_quotes(tickers: list[str]) -> dict[str, Quote]:
    symbols = "+".join(f"{ticker.lower()}.us" for ticker in tickers)
    # Field order: symbol, date, time, open, high, low, close, volume, previous close.
    url = f"https://stooq.com/q/l/?s={symbols}&f=sd2t2ohlcvp&h&e=csv"
    quotes: dict[str, Quote] = {}
    try:
        raw = http_get(url, timeout=18).decode("utf-8", errors="replace")
        for row in csv.DictReader(io.StringIO(raw)):
            symbol = (row.get("Symbol") or "").upper()
            ticker = symbol.removesuffix(".US")
            if ticker not in tickers:
                continue
            price = parse_float(row.get("Close", ""))
            prev_close = parse_float(row.get("Prev", ""))
            day_pct = ((price - prev_close) / prev_close * 100) if price and prev_close else None
            quotes[ticker] = Quote(
                ticker=ticker,
                price=price,
                prev_close=prev_close,
                day_change_pct=day_pct,
                open_price=parse_float(row.get("Open", "")),
                volume=parse_int(row.get("Volume", "")),
                avg_volume_10d=None,
                volume_ratio=None,
                last_timestamp=None,
            )
    except Exception as exc:  # noqa: BLE001
        for ticker in tickers:
            quotes[ticker] = Quote(ticker=ticker, price=None, prev_close=None, day_change_pct=None, open_price=None, volume=None, avg_volume_10d=None, volume_ratio=None, last_timestamp=None, error=f"stooq batch failed: {type(exc).__name__}: {exc}")
    return quotes


def fetch_rss_headlines(ticker: str) -> list[str]:
    url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={urllib.parse.quote(ticker)}&region=US&lang=en-US"
    try:
        xml = http_get(url, timeout=10).decode("utf-8", errors="replace")
    except Exception:
        return []
    titles = re.findall(r"(?is)<title><!\[CDATA\[(.*?)\]\]></title>|<title>(.*?)</title>", xml)
    out: list[str] = []
    for a, b in titles:
        title = html.unescape((a or b).strip())
        if title and not title.startswith("Yahoo! Finance"):
            out.append(re.sub(r"\s+", " ", title))
    return out[:5]


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.2f}%"


def num(value: int | float | None) -> str:
    if value is None:
        return "n/a"
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if abs(value) >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:.0f}"


def money(value: float | None) -> str:
    if value is None:
        return "n/a"
    if value < 1:
        return f"${value:.4f}"
    return f"${value:.2f}"


def initial_stop_cushion_pct(
    history: history_analysis.HistoryFeature | None,
    action: str,
    ticker: str,
) -> float:
    if history and not history.error and history.volatility_20d_pct is not None:
        base = history.volatility_20d_pct * 0.9
    else:
        base = 3.5
    if action == "aggressive_buy":
        base += 1.5
    elif action in {"predictive_buy", "early_buy"}:
        base += 0.5
    if ticker in LEVERAGED_ETFS:
        base += 1.0

    floor = 4.5 if action == "aggressive_buy" or ticker in LEVERAGED_ETFS else 3.0
    ceiling = 10.0 if action == "aggressive_buy" or ticker in LEVERAGED_ETFS else 8.0
    return min(max(base, floor), ceiling)


def trade_plan_for_buy(
    action: str,
    ticker: str,
    quote: Quote,
    history: history_analysis.HistoryFeature | None,
) -> TradePlan | None:
    if quote.price is None or quote.price <= 0:
        return None

    entry_buffer_pct = 1.0 if action == "aggressive_buy" or ticker in LEVERAGED_ETFS else 0.6
    max_entry = quote.price * (1 + entry_buffer_pct / 100)
    # Recommend a modest rebound trigger for a pre-fill trailing buy. Zero is
    # valid when volatility is unavailable or does not justify a buffer.
    volatility_pct = (
        max(0.0, history.volatility_20d_pct)
        if history and not history.error and history.volatility_20d_pct is not None
        else 0.0
    )
    trailing_buy_offset_pct = min(entry_buffer_pct, volatility_pct * 0.25)
    trailing_buy_offset = round(quote.price * trailing_buy_offset_pct / 100, 2)
    cushion = initial_stop_cushion_pct(history, action, ticker)
    stop = max_entry * (1 - cushion / 100)
    limit_gap_pct = 1.0 if cushion >= 5.0 else 0.5
    stop_limit = stop * (1 - limit_gap_pct / 100)
    notes = [
        f"max entry allows about {entry_buffer_pct:.1f}% above the quoted price",
        f"initial trailing stop-limit trail amount is {cushion:.1f}%",
        f"stop-limit offset is {limit_gap_pct:.1f}% below the trailing stop trigger",
        f"pre-fill trailing buy offset is ${trailing_buy_offset:.2f} ({trailing_buy_offset_pct:.2f}%) from the trailing low",
    ]
    if action == "aggressive_buy" or ticker in LEVERAGED_ETFS:
        notes.append("aggressive/high-beta setup: use smaller size and expect wider intraday swings")
    return TradePlan(
        max_entry=max_entry,
        stop=stop,
        stop_limit=stop_limit,
        cushion_pct=cushion,
        limit_gap_pct=limit_gap_pct,
        trailing_buy_offset=trailing_buy_offset,
        trailing_buy_offset_pct=trailing_buy_offset_pct,
        notes=notes,
    )


def trailing_stop_limit_plan_text(plan: TradePlan) -> str:
    return (
        f"post_fill_trailing_stop_limit trail {plan.cushion_pct:.1f}% "
        f"limit_offset {plan.limit_gap_pct:.1f}% "
        f"(max_entry {money(plan.max_entry)}; stop_ref {money(plan.stop)}; limit_ref {money(plan.stop_limit)})"
    )


def trailing_buy_plan_text(plan: TradePlan) -> str:
    return (
        f"pre_fill_trailing_buy offset ${plan.trailing_buy_offset:.2f} "
        f"({plan.trailing_buy_offset_pct:.2f}%; trigger after rebound from trailing low)"
    )


def intraday_buy_protection_text(paper: dict) -> str:
    """Return the paired post-fill intraday trailing-stop recommendation."""
    protection = paper.get("protection") if isinstance(paper, dict) else None
    if not isinstance(protection, dict) or protection.get("status") != "ok":
        reason = protection.get("reason", "unavailable") if isinstance(protection, dict) else "unavailable"
        return f"after-buy intraday trailing stop: unavailable ({reason})"
    trailing_pct = protection.get("trailing_pct")
    stop = protection.get("stop")
    if not isinstance(trailing_pct, (int, float)) or not isinstance(stop, (int, float)):
        return "after-buy intraday trailing stop: unavailable (missing calculated offset)"
    return f"after-buy intraday trailing stop: {trailing_pct:.2f}% (suggested stop ${stop:.2f}; recent swing low minus 0.3 ATR)"


MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def event_date_from_text(text: str, now: dt.datetime) -> str | None:
    lower = text.lower()
    if "today" in lower:
        return now.date().isoformat()
    if "tomorrow" in lower:
        return (now.date() + dt.timedelta(days=1)).isoformat()
    match = re.search(
        r"\b("
        r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?"
        r")\.?\s+(\d{1,2})(?:,\s*(\d{4}))?\b",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    month = MONTHS.get(match.group(1).lower().rstrip("."))
    day = int(match.group(2))
    year = int(match.group(3) or now.year)
    if month is None:
        return None
    try:
        candidate = dt.date(year, month, day)
    except ValueError:
        return None
    if candidate < now.date() - dt.timedelta(days=30) and match.group(3) is None:
        candidate = dt.date(year + 1, month, day)
    return candidate.isoformat()


def binary_event_risk_for_ticker(
    ticker: str,
    packet_hits: dict[str, list[str]],
    headlines: list[str],
    catalyst_events: dict[str, list[dict]],
    now: dt.datetime,
    post_earnings_mode: bool = False,
) -> list[str]:
    combined = " ".join(packet_hits.get(ticker, []) + headlines)
    combined_lower = combined.lower()
    confirmed_positive_results = (
        "earnings" in combined_lower
        and any(term in combined_lower for term in (
            "beat", "beats", "raised guidance", "raises guidance", "strong results", "record revenue",
        ))
    )
    risks = []
    # A confirmed, positive report is the signal for the post-earnings model,
    # not an upcoming binary event. Other models retain the conservative veto.
    if any(pattern.search(combined) for pattern in EVENT_RISK_PATTERNS) and not (
        post_earnings_mode and confirmed_positive_results
    ):
        risks.append("binary event risk detected; wait for post-event confirmation")
    for event in catalyst_events.get(ticker, []):
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "").lower()
        event_date = parsed_datetime(event.get("event_date"))
        days_until_event = (event_date.date() - now.date()).days if event_date else None
        earnings_in_risk_window = (
            days_until_event is not None
            and (1 <= days_until_event <= 3 if post_earnings_mode and confirmed_positive_results else -1 <= days_until_event <= 3)
        )
        if event_type == "earnings" and earnings_in_risk_window:
            risks.append(f"upcoming earnings catalyst on {event_date.date().isoformat()}; wait for post-event confirmation")
    return list(dict.fromkeys(risks))


def update_catalyst_calendar(
    tickers: list[str],
    packet_hits: dict[str, list[str]],
    headlines: dict[str, list[str]],
    source_verifications: dict[str, list[dict]],
    now: dt.datetime,
) -> dict[str, list[dict]]:
    state = read_json_object(CATALYST_CALENDAR_FILE, {"schema": "openclaw.market_worker.catalyst_calendar.v1", "events": {}})
    events = state.setdefault("events", {})
    if not isinstance(events, dict):
        events = {}
        state["events"] = events

    for ticker in tickers:
        rows = [row for row in events.get(ticker, []) if isinstance(row, dict)]
        seen_keys = {
            (
                str(row.get("type") or ""),
                str(row.get("event_date") or ""),
                str(row.get("snippet") or "")[:140],
            )
            for row in rows
        }
        snippets = packet_hits.get(ticker, []) + headlines.get(ticker, [])
        snippets.extend(
            str(item.get("snippet") or "")
            for item in source_verifications.get(ticker, [])
            if isinstance(item, dict) and item.get("snippet")
        )
        for snippet in snippets:
            compact = re.sub(r"\s+", " ", snippet).strip()
            if not compact or not any(pattern.search(compact) for pattern in CATALYST_EVENT_PATTERNS):
                continue
            lower = compact.lower()
            event_type = "earnings" if "earnings" in lower else (
                "analyst_event" if "analyst day" in lower or "investor day" in lower or "conference" in lower else
                "regulatory" if any(term in lower for term in ("fda", "pdufa", "approval")) else
                "corporate"
            )
            row = {
                "type": event_type,
                "event_date": event_date_from_text(compact, now),
                "detected_at": now.isoformat(),
                "snippet": compact[:320],
            }
            key = (row["type"], str(row["event_date"] or ""), row["snippet"][:140])
            if key not in seen_keys:
                rows.append(row)
                seen_keys.add(key)
        fresh_rows = []
        for row in rows:
            detected = parsed_datetime(row.get("detected_at"))
            event_date = parsed_datetime(row.get("event_date"))
            keep = bool(detected and now - detected <= dt.timedelta(days=CATALYST_CALENDAR_MAX_AGE_DAYS))
            keep = keep or bool(event_date and event_date.date() >= now.date() - dt.timedelta(days=3))
            if keep:
                fresh_rows.append(row)
        if fresh_rows:
            events[ticker] = fresh_rows[-8:]
        else:
            events.pop(ticker, None)

    state["generated_at"] = now.isoformat()
    write_json_atomic(CATALYST_CALENDAR_FILE, state)
    return {ticker: rows for ticker, rows in events.items() if isinstance(rows, list)}


def sync_thesis_tracker(
    open_positions: dict[str, dict],
    packet_hits: dict[str, list[str]],
    headlines: dict[str, list[str]],
    now: dt.datetime,
) -> dict:
    state = read_json_object(THESIS_TRACKER_FILE, {"schema": "openclaw.market_worker.thesis_tracker.v1", "positions": {}})
    positions = state.setdefault("positions", {})
    if not isinstance(positions, dict):
        positions = {}
        state["positions"] = positions

    for ticker, position in open_positions.items():
        record = positions.setdefault(
            ticker,
            {
                "status": "open",
                "created_at": now.isoformat(),
                "thesis": "Open position; thesis to be confirmed from future packet evidence.",
                "pillars": [],
                "risks": [],
                "invalidation_terms": ["downgrade", "cuts guidance", "miss", "weak demand", "probe", "lawsuit"],
                "updates": [],
            },
        )
        record["status"] = "open"
        record["last_seen_at"] = now.isoformat()
        if position.get("avg_price") is not None:
            record["entry_price"] = position.get("avg_price")
        context = (packet_hits.get(ticker) or headlines.get(ticker) or [])[:1]
        if context:
            updates = record.setdefault("updates", [])
            if isinstance(updates, list):
                update = {"date": now.date().isoformat(), "data_point": context[0][:260], "impact": "monitor"}
                if update not in updates:
                    updates.append(update)
                record["updates"] = updates[-12:]

    for ticker, record in list(positions.items()):
        if ticker not in open_positions and isinstance(record, dict):
            record["status"] = "closed"
            record.setdefault("closed_at", now.isoformat())
    state["generated_at"] = now.isoformat()
    write_json_atomic(THESIS_TRACKER_FILE, state)
    return state


def thesis_notes_for_ticker(ticker: str, thesis_state: dict, combined_text: str) -> tuple[list[str], list[str]]:
    positions = thesis_state.get("positions") if isinstance(thesis_state, dict) else {}
    record = positions.get(ticker) if isinstance(positions, dict) else None
    if not isinstance(record, dict) or record.get("status") != "open":
        return [], []
    evidence = []
    blockers = []
    thesis = str(record.get("thesis") or "").strip()
    if thesis:
        evidence.append(f"tracked thesis: {thesis[:180]}")
    lower = combined_text.lower()
    for term in record.get("invalidation_terms") or []:
        term_text = str(term).strip().lower()
        if term_text and term_text in lower:
            blockers.append(f"tracked thesis invalidation term appeared: {term_text}")
    return evidence, blockers


def update_idea_screen(
    rows: list[tuple[str, int, str, str, list[str], list[str]]],
    quotes: dict[str, Quote],
    history_features: dict[str, history_analysis.HistoryFeature],
    source_verifications: dict[str, list[dict]],
    now: dt.datetime,
) -> dict:
    prior = read_json_object(IDEA_SCREEN_FILE, {"schema": "openclaw.market_worker.idea_screen.v1", "candidates": {}})
    candidates = {
        str(ticker).upper(): row
        for ticker, row in (prior.get("candidates") or {}).items()
        if isinstance(row, dict)
    } if isinstance(prior.get("candidates"), dict) else {}
    for action, score, ticker, decision, evidence, blockers in rows:
        if action in {"sell", "hold"}:
            continue
        quote = quotes.get(ticker)
        history = history_features.get(ticker)
        idea_score = max(0, score)
        reasons = list(evidence[:4])
        verified = any(item.get("verified") for item in source_verifications.get(ticker, []) if isinstance(item, dict))
        if verified:
            idea_score += 2
            reasons.append("verified catalyst available")
        if quote and quote.day_change_pct is not None and quote.day_change_pct >= 1.5:
            idea_score += 1
            reasons.append(f"meaningful daily move: {pct(quote.day_change_pct)}")
        if quote and quote.volume_ratio is not None and quote.volume_ratio >= 0.8:
            idea_score += 1
            reasons.append(f"high participation: {quote.volume_ratio:.2f}x volume")
        if history and not history.error and history.sma5 is not None and history.sma20 is not None and history.sma5 > history.sma20:
            idea_score += 1
            reasons.append("constructive 5d/20d trend")
        if any("binary event risk" in blocker for blocker in blockers):
            idea_score -= 2
        if idea_score >= 3:
            candidates[ticker] = {
                "score": round(float(idea_score), 2),
                "last_action": action,
                "decision": decision,
                "reasons": list(dict.fromkeys(reasons))[:6],
                "blockers": blockers[:4],
                "last_seen_at": now.isoformat(),
            }
    fresh_candidates = {}
    for ticker, row in candidates.items():
        last_seen = parsed_datetime(row.get("last_seen_at"))
        if last_seen is None and not stale_timestamp(prior.get("generated_at"), now, IDEA_SCREEN_REFRESH_DAYS * 2):
            row["last_seen_at"] = str(prior.get("generated_at"))
            last_seen = parsed_datetime(row.get("last_seen_at"))
        if last_seen and now - last_seen <= dt.timedelta(days=IDEA_SCREEN_REFRESH_DAYS * 2):
            fresh_candidates[ticker] = row
    ranked = dict(
        sorted(fresh_candidates.items(), key=lambda item: (-(market_number(item[1].get("score", 0)) or 0), item[0]))[:IDEA_SCREEN_CANDIDATE_LIMIT]
    )
    state = {
        "schema": "openclaw.market_worker.idea_screen.v1",
        "generated_at": now.isoformat(),
        "next_refresh_after": (now + dt.timedelta(days=IDEA_SCREEN_REFRESH_DAYS)).isoformat(),
        "method": "Deterministic weekly-ish screen using catalyst verification, price movement, volume participation, and constructive history; candidates are inputs for later confirmation, not recommendations.",
        "candidates": ranked,
    }
    write_json_atomic(IDEA_SCREEN_FILE, state)
    return state


def signal_thresholds(window: str) -> dict[str, float]:
    if CONFIRMATION_PROFILE == "schwab-aggressive":
        if window == "opening":
            return {"min_change": 0.3, "max_change": 7.0, "min_sector": 0.0, "min_volume": 0.15, "score": 4}
        if window == "one-hour":
            return {"min_change": 0.5, "max_change": 6.0, "min_sector": 0.05, "min_volume": 0.2, "score": 5}
        if window == "midday":
            return {"min_change": 0.6, "max_change": 5.5, "min_sector": 0.05, "min_volume": 0.2, "score": 4}
        if window == "late-day":
            return {"min_change": 0.7, "max_change": 5.5, "min_sector": 0.1, "min_volume": 0.35, "score": 5}
        return {"min_change": 0.6, "max_change": 5.5, "min_sector": 0.05, "min_volume": 0.2, "score": 4}
    if window == "opening":
        return {"min_change": 0.6, "max_change": 4.0, "min_sector": 0.15, "min_volume": 0.25, "score": 5}
    if window == "one-hour":
        return {"min_change": 0.9, "max_change": 3.5, "min_sector": 0.2, "min_volume": 0.35, "score": 6}
    if window == "midday":
        return {"min_change": 1.0, "max_change": 3.0, "min_sector": 0.25, "min_volume": 0.35, "score": 5}
    if window == "late-day":
        return {"min_change": 1.1, "max_change": 3.0, "min_sector": 0.25, "min_volume": 0.6, "score": 6}
    return {"min_change": 1.0, "max_change": 3.0, "min_sector": 0.2, "min_volume": 0.25, "score": 5}


def next_market_day(day: dt.date) -> dt.date:
    day += dt.timedelta(days=1)
    while day.weekday() >= 5:
        day += dt.timedelta(days=1)
    return day


def valid_until_for(window: str, now: dt.datetime) -> dt.datetime:
    try:
        idx = CONFIRMATION_ORDER.index(window)
    except ValueError:
        return now
    for next_window in CONFIRMATION_ORDER[idx + 1 :]:
        if next_window == "friday-closeout" and now.weekday() != 4:
            continue
        candidate = dt.datetime.combine(now.date(), CONFIRMATION_TIMES[next_window], TZ)
        if candidate > now:
            return candidate
    return dt.datetime.combine(next_market_day(now.date()), CONFIRMATION_TIMES["opening"], TZ)


def sell_thresholds(window: str) -> dict[str, float]:
    if window == "opening":
        return {"max_day_change": -0.8, "max_sector": -0.2, "loss": -1.5, "hard_loss": -3.0, "score": 4}
    if window == "one-hour":
        return {"max_day_change": -1.0, "max_sector": -0.25, "loss": -1.75, "hard_loss": -3.25, "score": 4}
    if window == "midday":
        return {"max_day_change": -1.1, "max_sector": -0.3, "loss": -2.0, "hard_loss": -3.5, "score": 4}
    if window in {"late-day", "friday-closeout"}:
        return {"max_day_change": -1.2, "max_sector": -0.35, "loss": -2.0, "hard_loss": -4.0, "score": 5}
    if window == "after-close":
        return {"max_day_change": -2.0, "max_sector": -0.7, "loss": -3.0, "hard_loss": -5.0, "score": 5}
    return {"max_day_change": -1.0, "max_sector": -0.25, "loss": -2.0, "hard_loss": -3.5, "score": 4}


def position_entry_price(position: dict) -> float | None:
    for key in ("avg_price", "price", "cost_basis"):
        value = position.get(key)
        if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
            return float(value)
    return None


def sector_change_for(ticker: str, quotes: dict[str, Quote]) -> float | None:
    sector = TICKER_SECTOR[ticker]
    sector_quotes = [quotes[t] for t in CONTEXT[sector] if t in quotes and quotes[t].day_change_pct is not None]
    return statistics.mean(q.day_change_pct for q in sector_quotes if q.day_change_pct is not None) if sector_quotes else None


def score_sell_position(
    ticker: str,
    quote: Quote,
    quotes: dict[str, Quote],
    packet_hits: dict[str, list[str]],
    headlines: list[str],
    position: dict,
    window: str,
    history: history_analysis.HistoryFeature | None = None,
    thesis_state: dict | None = None,
) -> tuple[int, list[str], list[str]]:
    thresholds = sell_thresholds(window)
    evidence: list[str] = []
    blockers: list[str] = []
    score = 0

    if quote.error:
        blockers.append(f"quote fetch failed: {quote.error}")
        return 0, evidence, blockers

    combined_text = " ".join(packet_hits.get(ticker, []) + headlines).lower()
    negative_text = any(word in combined_text for word in NEGATIVE_WORDS)
    positive_text = any(word in combined_text for word in POSITIVE_WORDS)
    sector_change = sector_change_for(ticker, quotes)
    entry = position_entry_price(position)
    position_change = ((quote.price - entry) / entry * 100) if quote.price is not None and entry else None

    if position_change is not None and position_change <= thresholds["hard_loss"]:
        score += 3
        evidence.append(f"position below entry: {pct(position_change)}")
    elif position_change is not None and position_change <= thresholds["loss"]:
        score += 2
        evidence.append(f"position below entry: {pct(position_change)}")
    elif position_change is not None and position_change > 1.0:
        blockers.append(f"position still above entry: {pct(position_change)}")

    if quote.day_change_pct is not None and quote.day_change_pct <= thresholds["max_day_change"]:
        score += 2
        evidence.append(f"price weak today: {pct(quote.day_change_pct)}")
    elif quote.day_change_pct is not None and quote.day_change_pct >= 0.4:
        blockers.append(f"price resilient today: {pct(quote.day_change_pct)}")

    if sector_change is not None and sector_change <= thresholds["max_sector"]:
        score += 1
        evidence.append(f"{TICKER_SECTOR[ticker]} context weak: {pct(sector_change)} avg context ETF move")
    elif sector_change is not None and sector_change >= 0.2:
        blockers.append(f"{TICKER_SECTOR[ticker]} context supportive: {pct(sector_change)} avg context ETF move")

    if negative_text:
        score += 2
        evidence.append("negative headline language detected")
    if positive_text:
        blockers.append("positive headline language detected")

    thesis_evidence, thesis_blockers = thesis_notes_for_ticker(ticker, thesis_state or {}, combined_text)
    if thesis_evidence:
        evidence.extend(thesis_evidence)
    if thesis_blockers:
        score += 2
        evidence.extend(thesis_blockers)

    if quote.volume_ratio is not None and quote.volume_ratio >= 0.6 and quote.day_change_pct is not None and quote.day_change_pct < 0:
        score += 1
        evidence.append(f"weak move has volume confirmation: {quote.volume_ratio:.2f}x 10-day full-day avg")

    if history and not history.error:
        if history.sma20 is not None and quote.price is not None and quote.price < history.sma20:
            score += 1
            evidence.append(f"price broke below 20-day average: {quote.price:.2f} < {history.sma20:.2f}")
        if history.trend_20d_pct is not None and history.trend_20d_pct < -3:
            score += 1
            evidence.append(f"20-day trend is deteriorating: {pct(history.trend_20d_pct)}")
        if history.sma5 is not None and history.sma20 is not None and history.sma5 >= history.sma20:
            blockers.append(f"history trend still constructive: 5-day avg {history.sma5:.2f} >= 20-day avg {history.sma20:.2f}")

    return score, evidence, blockers


def classify_decision(
    ticker: str,
    score: int,
    evidence: list[str],
    blockers: list[str],
    window: str,
    open_positions: dict[str, dict],
    quote: Quote,
    quotes: dict[str, Quote],
    packet_hits: dict[str, list[str]],
    headlines: list[str],
    pullback_setup: tuple[int, list[str], list[str]],
    history: history_analysis.HistoryFeature | None = None,
    thesis_state: dict | None = None,
    entry_models: dict[str, tuple[int, list[str], list[str]]] | None = None,
) -> tuple[str, int, str, list[str], list[str]]:

    if ticker in open_positions:
        sell_score, sell_evidence, sell_blockers = score_sell_position(
            ticker, quote, quotes, packet_hits, headlines, open_positions[ticker], window, history, thesis_state
        )
        sell_ready = sell_score >= sell_thresholds(window)["score"] and sell_evidence
        if sell_ready:
            return "sell", sell_score, f"SELL SIGNAL: {ticker}", sell_evidence, sell_blockers
        if pullback_setup[0] >= 8:
            return "hold", pullback_setup[0], f"HOLD: {ticker} - pullback model is positive, but no second BUY", pullback_setup[1], sell_blockers
        return "hold", sell_score, f"HOLD: {ticker} - no sell signal", sell_evidence, sell_blockers
    models = {"pullback_reclaim": pullback_setup, **(entry_models or {})}
    eligible: list[tuple[str, int, list[str], list[str]]] = []
    for model_key, (model_score, model_evidence, model_blockers) in models.items():
        unsafe = any(fragment in blocker for blocker in model_blockers for fragment in ENTRY_MODEL_HARD_BLOCKER_FRAGMENTS)
        if model_key in BUY_ELIGIBLE_ENTRY_MODELS and model_score >= 8 and not unsafe:
            eligible.append((model_key, model_score, model_evidence, model_blockers))
    if eligible:
        eligible.sort(key=lambda item: (-item[1], item[0]))
        labels = ", ".join(ENTRY_MODEL_NAMES[key] for key, *_ in eligible)
        best_key, best_score, best_evidence, best_blockers = eligible[0]
        return (
            "buy", best_score,
            f"BUY CANDIDATE: {ticker} - models: {labels}; choose paper trade or actual Schwab trade",
            [f"qualifying models: {labels}", *best_evidence], best_blockers,
        )
    best_key, (best_score, best_evidence, best_blockers) = max(models.items(), key=lambda item: item[1][0])
    if any(fragment in blocker for blocker in best_blockers for fragment in ENTRY_MODEL_HARD_BLOCKER_FRAGMENTS):
        return "no_buy", best_score, f"NO BUY: {ticker} - entry safety gate", best_evidence, best_blockers
    if best_score >= 6:
        return "watch", best_score, f"WATCH: {ticker} - {ENTRY_MODEL_NAMES[best_key]} setup is incomplete", best_evidence, best_blockers
    return "no_buy", best_score, f"NO BUY: {ticker}", best_evidence, best_blockers


def apply_low_spending_power_gate(
    rows: list[tuple[str, int, str, str, list[str], list[str]]],
    spending_power: float | None,
    holdoff_symbols: set[str] | None = None,
) -> tuple[list[tuple[str, int, str, str, list[str], list[str]]], list[str]]:
    if not ROBINHOOD_SPENDING_POWER_GATE_ENABLED:
        return rows, ["Robinhood spending-power gate disabled for this confirmation profile"]
    holdoff_symbols = holdoff_symbols or set()
    if spending_power is None or spending_power >= LOW_SPENDING_POWER_THRESHOLD:
        return rows, []

    buy_actions = {"predictive_buy", "aggressive_buy", "early_buy", "buy"}
    buy_rows = [row for row in rows if row[0] in buy_actions]
    if not buy_rows:
        return rows, [f"Robinhood spending power ${spending_power:.2f} is below ${LOW_SPENDING_POWER_THRESHOLD:.2f}; no BUY gate needed"]

    updated_rows = []
    for row in rows:
        action, score, ticker, _decision, evidence, blockers = row
        if action in buy_actions:
            updated_rows.append(
                (
                    "no_buy",
                    score,
                    f"{ticker}",
                    f"NO BUY: {ticker} - spending power below ${LOW_SPENDING_POWER_THRESHOLD:.2f} and no weaker HOLD available to sell",
                    evidence,
                    [
                        *blockers,
                        f"Robinhood spending power ${spending_power:.2f} is below ${LOW_SPENDING_POWER_THRESHOLD:.2f}; cash-only account cannot assume same-day reuse of sell proceeds",
                    ],
                )
            )
        else:
            updated_rows.append(row)
    return updated_rows, [
        f"Robinhood spending power ${spending_power:.2f} is below ${LOW_SPENDING_POWER_THRESHOLD:.2f}; BUY signals blocked until settled cash is available"
    ]


def read_buy_excluded_symbols() -> dict[str, str]:
    excluded = dict(DEFAULT_BUY_EXCLUDED_SYMBOLS)
    if not BROKER_ELIGIBILITY_FILE.exists():
        return excluded

    try:
        data = json.loads(BROKER_ELIGIBILITY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return excluded

    configured = data.get("buy_excluded_symbols") if isinstance(data, dict) else data
    if isinstance(configured, list):
        for item in configured:
            if isinstance(item, str):
                symbol = item.upper().strip()
                if symbol:
                    excluded[symbol] = "configured as ineligible for buy recommendations"
            elif isinstance(item, dict):
                symbol = str(item.get("symbol") or "").upper().strip()
                reason = str(item.get("reason") or "configured as ineligible for buy recommendations").strip()
                if symbol:
                    excluded[symbol] = reason
    elif isinstance(configured, dict):
        for symbol, reason in configured.items():
            normalized = str(symbol).upper().strip()
            if normalized:
                excluded[normalized] = str(reason or "configured as ineligible for buy recommendations").strip()
    return excluded


def apply_broker_eligibility_gate(
    rows: list[tuple[str, int, str, str, list[str], list[str]]],
    buy_excluded_symbols: dict[str, str] | None = None,
) -> tuple[list[tuple[str, int, str, str, list[str], list[str]]], list[str]]:
    excluded = buy_excluded_symbols if buy_excluded_symbols is not None else read_buy_excluded_symbols()
    if not excluded:
        return rows, []

    buy_actions = {"predictive_buy", "aggressive_buy", "early_buy", "buy"}
    updated_rows = []
    notes: list[str] = []
    blocked: list[str] = []
    for action, score, ticker, decision, evidence, blockers in rows:
        reason = excluded.get(ticker)
        if action in buy_actions and reason:
            blocked.append(f"{ticker}: {reason}")
            updated_rows.append(
                (
                    "no_buy",
                    score,
                    ticker,
                    f"NO BUY: {ticker} - broker/tax eligibility blocked",
                    evidence,
                    [*blockers, f"broker/tax eligibility blocker: {reason}"],
                )
            )
        else:
            updated_rows.append((action, score, ticker, decision, evidence, blockers))

    if blocked:
        notes.append("Broker/tax eligibility gate blocked BUY-style recommendations for configured symbols.")
        notes.extend(blocked[:8])
    return updated_rows, notes


def apply_account_fit_gate(
    rows: list[tuple[str, int, str, str, list[str], list[str]]],
    quotes: dict[str, Quote],
    spending_power: float | None,
) -> tuple[list[tuple[str, int, str, str, list[str], list[str]]], list[str]]:
    buy_actions = {"predictive_buy", "aggressive_buy", "early_buy", "buy"}
    if not WHOLE_SHARE_PROTECTIVE_STOPS_ONLY and not CASH_ONLY_NO_MARGIN:
        return rows, []

    updated_rows = []
    notes: list[str] = []
    blocked: list[str] = []
    spend_cap = MAX_WHOLE_SHARE_TRADE_VALUE
    if CASH_ONLY_NO_MARGIN and spending_power is not None:
        spend_cap = min(spend_cap, spending_power)

    for action, score, ticker, decision, evidence, blockers in rows:
        if action not in buy_actions:
            updated_rows.append((action, score, ticker, decision, evidence, blockers))
            continue

        quote = quotes.get(ticker)
        price = quote.price if quote else None
        account_blockers: list[str] = []
        if price is None or price <= 0:
            account_blockers.append("account fit unavailable: quote price missing for whole-share sizing")
        elif WHOLE_SHARE_PROTECTIVE_STOPS_ONLY and price > MAX_WHOLE_SHARE_TRADE_VALUE:
            account_blockers.append(
                f"whole-share protective-stop gate: {money(price)} share price exceeds ${MAX_WHOLE_SHARE_TRADE_VALUE:.2f} transaction cap"
            )
        elif CASH_ONLY_NO_MARGIN and spending_power is not None and price > spending_power:
            account_blockers.append(
                f"cash-only gate: {money(price)} share price exceeds settled/available spending power {money(spending_power)}"
            )
        elif WHOLE_SHARE_PROTECTIVE_STOPS_ONLY:
            whole_shares = math.floor(spend_cap / price)
            if whole_shares < 1:
                account_blockers.append(
                    f"whole-share protective-stop gate: no whole share fits inside available cap {money(spend_cap)}"
                )
            else:
                evidence = [
                    *evidence,
                    f"account fit: {whole_shares} whole share(s) fit inside {money(spend_cap)} cap; protective stop eligible",
                ]

        if account_blockers:
            blocked.append(f"{ticker}: {account_blockers[0]}")
            updated_rows.append(
                (
                    "no_buy",
                    score,
                    ticker,
                    f"NO BUY: {ticker} - account constraints block whole-share protective-stop setup",
                    evidence,
                    [*blockers, *account_blockers],
                )
            )
        else:
            updated_rows.append((action, score, ticker, decision, evidence, blockers))

    if blocked:
        notes.append("Account-fit gate blocked BUY signals that cannot be bought as whole shares within cash-only constraints.")
        notes.extend(blocked[:8])
    return updated_rows, notes


def apply_schwab_balance_gate(
    rows: list[tuple[str, int, str, str, list[str], list[str]]],
    quotes: dict[str, Quote],
    schwab_buying_power: float | None,
    schwab_buying_power_status: str,
) -> tuple[list[tuple[str, int, str, str, list[str], list[str]]], list[str]]:
    if not SCHWAB_BALANCE_GATE_ENABLED:
        return rows, []

    buy_actions = {"predictive_buy", "aggressive_buy", "early_buy", "buy"}
    if not any(row[0] in buy_actions for row in rows):
        return rows, [f"Schwab balance gate checked: {schwab_buying_power_status}"]
    if schwab_buying_power is None:
        return rows, [f"Schwab balance gate could not verify buying power; BUY rows left advisory-only: {schwab_buying_power_status}"]

    updated_rows = []
    blocked: list[str] = []
    for action, score, ticker, decision, evidence, blockers in rows:
        if action not in buy_actions:
            updated_rows.append((action, score, ticker, decision, evidence, blockers))
            continue
        quote = quotes.get(ticker)
        price = quote.price if quote else None
        if price is None or price <= 0:
            blocked.append(f"{ticker}: quote price missing")
            updated_rows.append(
                (
                    "no_buy",
                    score,
                    ticker,
                    f"NO BUY: {ticker} - Schwab balance gate cannot size a trade",
                    evidence,
                    [*blockers, "Schwab balance gate: quote price missing"],
                )
            )
        elif price > schwab_buying_power:
            blocked.append(f"{ticker}: {money(price)} share price exceeds {money(schwab_buying_power)} Schwab buying power")
            updated_rows.append(
                (
                    "no_buy",
                    score,
                    ticker,
                    f"NO BUY: {ticker} - Schwab buying power too low",
                    evidence,
                    [*blockers, f"Schwab balance gate: {money(price)} share price exceeds {money(schwab_buying_power)} available"],
                )
            )
        else:
            updated_rows.append(
                (
                    action,
                    score,
                    ticker,
                    decision,
                    [*evidence, f"Schwab balance gate: one share fits inside {money(schwab_buying_power)} ({schwab_buying_power_status})"],
                    blockers,
                )
            )

    notes = [f"Schwab balance gate used {money(schwab_buying_power)} from {schwab_buying_power_status}"]
    notes.extend(blocked[:8])
    return updated_rows, notes


def apply_existing_position_buy_gate(
    rows: list[tuple[str, int, str, str, list[str], list[str]]],
    all_positions: dict[str, dict],
) -> tuple[list[tuple[str, int, str, str, list[str], list[str]]], list[str]]:
    buy_actions = {"predictive_buy", "aggressive_buy", "early_buy", "buy"}
    if not all_positions:
        return rows, []

    updated_rows = []
    blocked: list[str] = []
    for action, score, ticker, decision, evidence, blockers in rows:
        position = all_positions.get(ticker)
        if action not in buy_actions or not position:
            updated_rows.append((action, score, ticker, decision, evidence, blockers))
            continue
        brokers = position.get("brokers") or [position.get("broker") or "unknown"]
        broker_text = ",".join(str(item) for item in brokers if item)
        blocker = f"already held in existing position ({broker_text}); no second buy recommendation"
        blocked.append(f"{ticker}: {broker_text}")
        updated_rows.append(
            (
                "no_buy",
                score,
                ticker,
                f"NO BUY: {ticker} - already held in another account",
                evidence,
                [*blockers, blocker],
            )
        )
    if not blocked:
        return updated_rows, []
    return updated_rows, ["Existing-position gate blocked duplicate BUY-style recommendations.", *blocked[:8]]


def apply_same_session_trade_holdoff(
    rows: list[tuple[str, int, str, str, list[str], list[str]]],
    traded_symbols: set[str],
) -> tuple[list[tuple[str, int, str, str, list[str], list[str]]], list[str]]:
    if not traded_symbols:
        return rows, []

    buy_actions = {"predictive_buy", "aggressive_buy", "early_buy", "buy"}
    updated_rows = []
    blocked: list[str] = []
    for action, score, ticker, decision, evidence, blockers in rows:
        if ticker not in traded_symbols or action not in buy_actions | {"sell"}:
            updated_rows.append((action, score, ticker, decision, evidence, blockers))
            continue
        blocked.append(f"{ticker} {action.upper()}")
        holdoff_blocker = "same-session trade holdoff: symbol already changed position during this regular session"
        if action == "sell":
            updated_rows.append(
                (
                    "hold",
                    score,
                    ticker,
                    f"HOLD: {ticker} - same-session trade holdoff blocks another sell",
                    evidence,
                    [*blockers, holdoff_blocker],
                )
            )
        else:
            updated_rows.append(
                (
                    "no_buy",
                    score,
                    ticker,
                    f"NO BUY: {ticker} - same-session trade holdoff blocks another buy",
                    evidence,
                    [*blockers, holdoff_blocker],
                )
            )
    if not blocked:
        return rows, []
    unique = ", ".join(sorted(set(blocked)))
    return updated_rows, [f"Same-session trade holdoff blocked: {unique}"]


def average_liquidity_blockers(quote: Quote, explicit_watchlist_name: bool = False) -> list[str]:
    """Hard BUY blockers for thin names; explicit user watchlist names opt out."""
    if explicit_watchlist_name or quote.avg_volume_10d is None:
        return []
    blockers = []
    if quote.price is not None:
        avg_dollar_volume = quote.price * quote.avg_volume_10d
        if avg_dollar_volume < MIN_BUY_AVG_DOLLAR_VOLUME:
            blockers.append(
                f"average dollar volume too low: ${avg_dollar_volume:,.0f} < ${MIN_BUY_AVG_DOLLAR_VOLUME:,.0f}"
            )
    if quote.avg_volume_10d < MIN_BUY_AVG_VOLUME:
        blockers.append(
            f"average share volume too low: {num(quote.avg_volume_10d)} < {num(MIN_BUY_AVG_VOLUME)} shares/day"
        )
    return blockers


def entry_model_baseline(
    ticker: str,
    quote: Quote,
    packet_hits: dict[str, list[str]],
    headlines: list[str],
    source_verifications: dict[str, list[dict]],
    explicit_watchlist_name: bool,
    binary_risks: list[str] | None,
    require_ticker_catalyst: bool = True,
) -> tuple[int, list[str], list[str], str, bool]:
    """Return shared catalyst/liquidity checks for the non-pullback models."""
    evidence: list[str] = []
    blockers: list[str] = []
    if quote.error or quote.price is None:
        return 0, evidence, [f"quote fetch failed: {quote.error or 'missing price'}"], "", False
    combined_text = " ".join(packet_hits.get(ticker, []) + headlines).lower()
    verified = any(item.get("verified") for item in source_verifications.get(ticker, []))
    if verified:
        evidence.append("verified independent ticker-specific catalyst source found")
    elif require_ticker_catalyst:
        blockers.append("no verified independent ticker-specific catalyst source")
    if any(word in combined_text for word in NEGATIVE_WORDS):
        blockers.append("negative headline language detected")
    if binary_risks:
        blockers.extend(binary_risks)
    if quote.price < MIN_BUY_PRICE and not explicit_watchlist_name:
        blockers.append(f"price below durable-trade floor: {quote.price:.2f} < {MIN_BUY_PRICE:.2f}")
    blockers.extend(average_liquidity_blockers(quote, explicit_watchlist_name))
    if quote.volume is None or quote.volume < MIN_OPENING_BUY_CURRENT_VOLUME:
        blockers.append(
            f"current participation too thin: {num(quote.volume)} shares < {num(MIN_OPENING_BUY_CURRENT_VOLUME)}"
            if quote.volume is not None else "current participation unavailable"
        )
    return (3 if verified else 0), evidence, blockers, combined_text, verified


def score_evidence_revision_momentum_setup(
    ticker: str, quote: Quote, quotes: dict[str, Quote], packet_hits: dict[str, list[str]],
    headlines: list[str], source_verifications: dict[str, list[dict]], intraday: Intraday,
    history: history_analysis.HistoryFeature | None = None, explicit_watchlist_name: bool = False,
    binary_risks: list[str] | None = None,
) -> tuple[int, list[str], list[str]]:
    """Evidence-led overnight continuation, not a mean-reversion bet.

    A recommendation needs a verified, favorable broker revision, earnings
    validation, or material corporate development *and* market confirmation.
    Headline consensus is a bounded public-news sentiment input; untraceable
    social-media sentiment is deliberately not used as trade evidence.
    """
    score, evidence, blockers, combined_text, verified = entry_model_baseline(
        ticker, quote, packet_hits, headlines, source_verifications,
        explicit_watchlist_name, binary_risks,
    )
    if quote.error or quote.price is None:
        return score, evidence, blockers

    broker_support = any(term in combined_text for term in BROKER_BULLISH_WORDS)
    earnings_support = any(term in combined_text for term in EARNINGS_VALIDATION_WORDS)
    corporate_support = any(term in combined_text for term in CORPORATE_VALIDATION_WORDS)
    positive_mentions = sum(combined_text.count(word) for word in POSITIVE_WORDS)
    negative_mentions = sum(combined_text.count(word) for word in NEGATIVE_WORDS)
    sector_change = sector_change_for(ticker, quotes)
    relative_strength = (
        quote.day_change_pct - sector_change
        if quote.day_change_pct is not None and sector_change is not None else None
    )

    if broker_support:
        score += 3
        evidence.append("verified coverage includes a favorable broker revision")
    elif earnings_support:
        score += 3
        evidence.append("verified coverage includes favorable reported earnings/guidance")
    elif corporate_support:
        score += 2
        evidence.append("verified coverage includes a material corporate development")
    else:
        blockers.append("no favorable broker revision, earnings validation, or material corporate development")
    if positive_mentions > negative_mentions:
        score += 1
        evidence.append("fresh public-news tone is net positive")
    else:
        blockers.append("fresh public-news tone is not net positive")
    if quote.day_change_pct is not None and 0.4 <= quote.day_change_pct <= 6.0:
        score += 1
        evidence.append(f"price confirms evidence: {pct(quote.day_change_pct)} today")
    else:
        blockers.append("price does not confirm the favorable evidence")
    if relative_strength is not None and relative_strength >= 0.5:
        score += 2
        evidence.append(f"relative strength leads {TICKER_SECTOR[ticker]} context by {relative_strength:+.2f} points")
    else:
        blockers.append("relative strength lead below 0.50 points")
    if quote.volume_ratio is not None and quote.volume_ratio >= 0.6:
        score += 1
        evidence.append(f"volume confirms participation: {quote.volume_ratio:.2f}x normal")
    else:
        blockers.append("volume does not confirm the favorable evidence")
    if history and not history.error and history.sma5 is not None and history.sma20 is not None and history.sma5 > history.sma20:
        score += 1
        evidence.append("daily trend supports continuation")
    else:
        blockers.append("daily trend does not support continuation")
    if intraday.return_15m_pct is not None and intraday.return_15m_pct >= 0:
        score += 1
        evidence.append("intraday confirmation is holding")
    else:
        blockers.append("intraday confirmation is fading")
    required = (
        "no verified", "negative headline", "binary event", "below durable",
        "average dollar", "average share", "current participation",
        "no favorable broker", "not net positive", "does not confirm",
        "relative strength", "volume does not", "daily trend", "intraday confirmation",
    )
    if not verified or any(any(fragment in blocker for fragment in required) for blocker in blockers):
        score = min(score, 7)
    return score, evidence, blockers


def score_catalyst_trend_continuation_setup(
    ticker: str, quote: Quote, quotes: dict[str, Quote], packet_hits: dict[str, list[str]],
    headlines: list[str], source_verifications: dict[str, list[dict]], intraday: Intraday,
    history: history_analysis.HistoryFeature | None = None, explicit_watchlist_name: bool = False,
    binary_risks: list[str] | None = None,
) -> tuple[int, list[str], list[str]]:
    """Fresh catalyst, relative strength, volume, and a held intraday trigger."""
    score, evidence, blockers, combined_text, verified = entry_model_baseline(
        ticker, quote, packet_hits, headlines, source_verifications, explicit_watchlist_name, binary_risks
    )
    if quote.error or quote.price is None:
        return score, evidence, blockers
    sector_change = sector_change_for(ticker, quotes)
    relative_strength = quote.day_change_pct - sector_change if quote.day_change_pct is not None and sector_change is not None else None
    if any(word in combined_text for word in POSITIVE_WORDS):
        score += 1; evidence.append("fresh catalyst language is positive")
    else:
        blockers.append("fresh catalyst lacks positive follow-through language")
    if quote.day_change_pct is not None and 0.5 <= quote.day_change_pct <= 5.0:
        score += 1; evidence.append(f"continuation move is controlled: {pct(quote.day_change_pct)} today")
    else:
        blockers.append(f"continuation move outside 0.5% to 5.0% trigger: {pct(quote.day_change_pct)} today")
    if relative_strength is not None and relative_strength >= 0.5:
        score += 2; evidence.append(f"relative strength leads {TICKER_SECTOR[ticker]} context by {relative_strength:+.2f} points")
    else:
        blockers.append("relative strength lead below 0.50 points" if relative_strength is not None else "relative strength unavailable")
    if quote.volume_ratio is not None and quote.volume_ratio >= 0.5:
        score += 1; evidence.append(f"participation confirms: {quote.volume_ratio:.2f}x 10-day full-day average")
    else:
        blockers.append("continuation volume below 0.50x normal" if quote.volume_ratio is not None else "continuation volume unavailable")
    if history and not history.error and history.sma5 is not None and history.sma20 is not None and history.sma5 > history.sma20:
        score += 1; evidence.append("daily trend supports continuation")
    else:
        blockers.append("daily trend does not support continuation")
    if intraday.return_15m_pct is not None and intraday.return_15m_pct >= -0.1:
        score += 1; evidence.append(f"intraday trigger is holding: 15m {pct(intraday.return_15m_pct)}")
    else:
        blockers.append("intraday continuation trigger is fading")
    if not verified or any("lacks positive" in b or "outside 0.5" in b or "relative strength" in b or "volume below" in b or "daily trend" in b or "trigger is fading" in b for b in blockers):
        score = min(score, 7)
    return score, evidence, blockers


def score_post_earnings_drift_setup(
    ticker: str, quote: Quote, quotes: dict[str, Quote], packet_hits: dict[str, list[str]],
    headlines: list[str], source_verifications: dict[str, list[dict]], intraday: Intraday,
    history: history_analysis.HistoryFeature | None = None, explicit_watchlist_name: bool = False,
    binary_risks: list[str] | None = None,
) -> tuple[int, list[str], list[str]]:
    """Post-report strength; the calendar still vetoes a *future* earnings event."""
    score, evidence, blockers, combined_text, verified = entry_model_baseline(
        ticker, quote, packet_hits, headlines, source_verifications, explicit_watchlist_name, binary_risks
    )
    if quote.error or quote.price is None:
        return score, evidence, blockers
    sector_change = sector_change_for(ticker, quotes)
    rs = quote.day_change_pct - sector_change if quote.day_change_pct is not None and sector_change is not None else None
    post_earnings = "earnings" in combined_text and any(word in combined_text for word in {"beat", "beats", "raises", "raised", "strong", "record"})
    if post_earnings:
        score += 2; evidence.append("reported earnings reaction is positive")
    else:
        blockers.append("no positive post-earnings reaction identified")
    if quote.day_change_pct is not None and 0.5 <= quote.day_change_pct <= 7.0:
        score += 1; evidence.append(f"post-earnings move remains buyable: {pct(quote.day_change_pct)} today")
    else:
        blockers.append("post-earnings move is too extended or weak")
    if rs is not None and rs >= 0.3:
        score += 1; evidence.append(f"post-earnings relative strength leads by {rs:+.2f} points")
    else:
        blockers.append("post-earnings relative strength insufficient")
    if quote.volume_ratio is not None and quote.volume_ratio >= 0.5:
        score += 1; evidence.append(f"post-earnings volume supports drift: {quote.volume_ratio:.2f}x normal")
    else:
        blockers.append("post-earnings volume insufficient")
    if history and not history.error and history.close_vs_sma20_pct is not None and history.close_vs_sma20_pct <= max(12.0, (history.volatility_20d_pct or 0) * 3):
        score += 1; evidence.append("post-earnings extension remains controlled")
    else:
        blockers.append("post-earnings extension is too large or unavailable")
    if intraday.return_15m_pct is not None and intraday.return_15m_pct >= -0.15:
        score += 1; evidence.append("post-earnings momentum is holding intraday")
    else:
        blockers.append("post-earnings momentum is fading")
    if not verified or any("no positive post" in b or "too extended" in b or "relative strength insufficient" in b or "volume insufficient" in b or "extension is too" in b or "momentum is fading" in b for b in blockers):
        score = min(score, 7)
    return score, evidence, blockers


def score_sector_leader_breakout_setup(
    ticker: str, quote: Quote, quotes: dict[str, Quote], packet_hits: dict[str, list[str]],
    headlines: list[str], source_verifications: dict[str, list[dict]], intraday: Intraday,
    history: history_analysis.HistoryFeature | None = None, explicit_watchlist_name: bool = False,
    binary_risks: list[str] | None = None,
) -> tuple[int, list[str], list[str]]:
    """Sector confirmation plus leadership near a 60-day high, without chasing extremes."""
    score, evidence, blockers, _text, verified = entry_model_baseline(
        ticker, quote, packet_hits, headlines, source_verifications, explicit_watchlist_name, binary_risks,
        require_ticker_catalyst=False,
    )
    if quote.error or quote.price is None:
        return score, evidence, blockers
    sector_change = sector_change_for(ticker, quotes)
    rs = quote.day_change_pct - sector_change if quote.day_change_pct is not None and sector_change is not None else None
    if sector_change is not None and sector_change >= 0.25:
        score += 1; evidence.append(f"{TICKER_SECTOR[ticker]} sector is leading: {pct(sector_change)}")
    else:
        blockers.append("sector is not strong enough for breakout")
    if quote.day_change_pct is not None and 0.75 <= quote.day_change_pct <= 5.0:
        score += 1; evidence.append(f"breakout move is controlled: {pct(quote.day_change_pct)} today")
    else:
        blockers.append("breakout move is too extended or weak")
    if rs is not None and rs >= 0.3:
        score += 2; evidence.append(f"stock leads its sector by {rs:+.2f} points")
    else:
        blockers.append("stock is not leading its sector")
    if quote.volume_ratio is not None and quote.volume_ratio >= 0.5:
        score += 1; evidence.append(f"breakout volume confirms: {quote.volume_ratio:.2f}x normal")
    else:
        blockers.append("breakout volume insufficient")
    if history and not history.error and history.range_position_60d is not None and 0.82 <= history.range_position_60d <= 0.98:
        score += 1; evidence.append(f"price is in a controlled 60-day breakout zone: {history.range_position_60d:.2f}")
    else:
        blockers.append("price is not in a controlled 60-day breakout zone")
    if intraday.return_15m_pct is not None and intraday.return_15m_pct >= -0.1:
        score += 1; evidence.append("breakout is holding intraday")
    else:
        blockers.append("breakout is fading intraday")
    # Verified sector strength plus technical leadership is a valid breakout
    # catalyst; a separate company headline is helpful but not mandatory.
    if sector_change is not None and sector_change >= 0.25 and rs is not None and rs >= 0.3:
        score += 2
        evidence.append("sector strength and technical leadership substitute for a ticker-specific catalyst")
    if any("not strong enough" in b or "too extended" in b or "not leading" in b or "volume insufficient" in b or "not in a controlled" in b or "fading intraday" in b for b in blockers):
        score = min(score, 7)
    return score, evidence, blockers


def score_news_dislocation_setup(
    ticker: str, quote: Quote, quotes: dict[str, Quote], packet_hits: dict[str, list[str]],
    headlines: list[str], source_verifications: dict[str, list[dict]], intraday: Intraday,
    history: history_analysis.HistoryFeature | None = None, explicit_watchlist_name: bool = False,
    binary_risks: list[str] | None = None,
) -> tuple[int, list[str], list[str]]:
    """Buy only a verified, non-fundamental news shock after it stabilizes.

    Packet evidence includes the worker's normalized RSS/Atom entries and
    downloaded email corpus.  The worker routes retained items through Luna and
    Terra when enabled; this confirmation function deliberately does not ask an
    LLM to make the trade decision.
    """
    evidence: list[str] = []
    blockers: list[str] = []
    score = 0
    if quote.error or quote.price is None:
        return 0, evidence, [f"quote fetch failed: {quote.error or 'missing price'}"]

    combined_text = " ".join(packet_hits.get(ticker, []) + headlines).lower()
    verified = any(item.get("verified") for item in source_verifications.get(ticker, []))
    transient = any(term in combined_text for term in DISLOCATION_TRANSIENT_WORDS)
    fundamental_damage = any(term in combined_text for term in FUNDAMENTAL_DAMAGE_WORDS)
    sector_change = sector_change_for(ticker, quotes)
    relative_move = (
        quote.day_change_pct - sector_change
        if quote.day_change_pct is not None and sector_change is not None
        else None
    )

    if verified:
        score += 2
        evidence.append("verified independent source documents the dislocation")
    else:
        blockers.append("news-dislocation requires a verified independent source")
    if transient:
        score += 2
        evidence.append("source language is consistent with a sentiment/positioning shock")
    else:
        blockers.append("source does not identify a temporary/sentiment dislocation")
    if fundamental_damage:
        blockers.append("fundamental damage indicated in source evidence")
    else:
        score += 1
        evidence.append("no fundamental-damage terms found in source evidence")
    if binary_risks:
        blockers.extend(binary_risks)
    if quote.price < MIN_BUY_PRICE and not explicit_watchlist_name:
        blockers.append(f"price below durable-trade floor: {quote.price:.2f} < {MIN_BUY_PRICE:.2f}")
    blockers.extend(average_liquidity_blockers(quote, explicit_watchlist_name))
    if quote.volume is None or quote.volume < MIN_OPENING_BUY_CURRENT_VOLUME:
        blockers.append(
            f"current participation too thin: {num(quote.volume)} shares < {num(MIN_OPENING_BUY_CURRENT_VOLUME)}"
            if quote.volume is not None else "current participation unavailable"
        )

    if quote.day_change_pct is not None and -8.0 <= quote.day_change_pct <= -1.0:
        score += 1
        evidence.append(f"abnormal but bounded dislocation: {pct(quote.day_change_pct)} today")
    else:
        blockers.append(f"dislocation move outside -8.0% to -1.0% range: {pct(quote.day_change_pct)} today")
    if relative_move is not None and relative_move <= -0.5:
        score += 1
        evidence.append(f"stock-specific weakness is distinct from sector: {relative_move:+.2f} points")
    else:
        blockers.append("move is not sufficiently stock-specific versus sector")
    if quote.volume_ratio is not None and quote.volume_ratio >= 0.8:
        score += 1
        evidence.append(f"dislocation has abnormal participation: {quote.volume_ratio:.2f}x normal")
    else:
        blockers.append("dislocation volume below 0.80x normal or unavailable")
    if (
        history and not history.error and history.sma5 is not None and history.sma20 is not None
        and history.sma5 >= history.sma20 and (history.trend_20d_pct is None or history.trend_20d_pct >= -1.0)
    ):
        score += 1
        evidence.append("pre-shock daily trend was constructive")
    else:
        blockers.append("pre-shock daily trend is not constructive or unavailable")
    if (
        intraday.return_15m_pct is not None and intraday.return_30m_pct is not None
        and intraday.return_15m_pct >= -0.10 and intraday.return_15m_pct >= intraday.return_30m_pct
    ):
        score += 2
        evidence.append(
            f"stabilization trigger confirmed: 15m {pct(intraday.return_15m_pct)} vs 30m {pct(intraday.return_30m_pct)}"
        )
    else:
        blockers.append(
            f"no stabilization trigger: 15m {pct(intraday.return_15m_pct)} vs 30m {pct(intraday.return_30m_pct)}"
        )

    required = (
        "requires a verified", "temporary/sentiment", "fundamental damage",
        "outside -8.0", "stock-specific", "volume below", "pre-shock", "no stabilization",
    )
    if any(any(fragment in blocker for fragment in required) for blocker in blockers):
        score = min(score, 7)
    return score, evidence, blockers


def score_pullback_reclaim_setup(
    ticker: str,
    quote: Quote,
    quotes: dict[str, Quote],
    packet_hits: dict[str, list[str]],
    headlines: list[str],
    source_verifications: dict[str, list[dict]],
    intraday: Intraday,
    history: history_analysis.HistoryFeature | None = None,
    explicit_watchlist_name: bool = False,
    binary_risks: list[str] | None = None,
) -> tuple[int, list[str], list[str]]:
    """Score the sole entry model: a liquid, catalyst-backed pullback/reclaim.

    A qualifying row is a candidate for the user to choose as either a paper
    trade or an actual Schwab trade. This function never places orders.
    """
    evidence: list[str] = []
    blockers: list[str] = []
    score = 0
    combined_text = " ".join(packet_hits.get(ticker, []) + headlines).lower()
    verified_sources = [item for item in source_verifications.get(ticker, []) if item.get("verified")]
    sector_change = sector_change_for(ticker, quotes)

    if quote.error or quote.price is None:
        return 0, evidence, [f"quote fetch failed: {quote.error or 'missing price'}"]
    if verified_sources:
        score += 3
        evidence.append("verified independent ticker-specific catalyst source found")
    else:
        blockers.append("no verified independent ticker-specific catalyst source")
    if any(word in combined_text for word in NEGATIVE_WORDS):
        blockers.append("negative headline language detected")
    if binary_risks:
        blockers.extend(binary_risks)

    if quote.price < MIN_BUY_PRICE and not explicit_watchlist_name:
        blockers.append(f"price below durable-trade floor: {quote.price:.2f} < {MIN_BUY_PRICE:.2f}")
    blockers.extend(average_liquidity_blockers(quote, explicit_watchlist_name))
    if quote.volume is None or quote.volume < MIN_OPENING_BUY_CURRENT_VOLUME:
        blockers.append(
            f"current participation too thin: {num(quote.volume)} shares < {num(MIN_OPENING_BUY_CURRENT_VOLUME)}"
            if quote.volume is not None
            else "current participation unavailable"
        )
    else:
        score += 1
        evidence.append(f"current participation is adequate: {num(quote.volume)} shares")

    if history and not history.error and history.sma5 is not None and history.sma20 is not None:
        if history.sma5 > history.sma20 and (history.trend_20d_pct is None or history.trend_20d_pct >= 0):
            score += 2
            evidence.append(f"daily trend remains constructive: 5-day avg {history.sma5:.2f} > 20-day avg {history.sma20:.2f}")
        else:
            blockers.append("daily trend is not constructive for a pullback/reclaim")
        if history.close_vs_sma20_pct is not None and -3.0 <= history.close_vs_sma20_pct <= 3.0:
            score += 1
            evidence.append(f"prior close was in the controlled reclaim zone: {pct(history.close_vs_sma20_pct)} vs 20-day avg")
        else:
            blockers.append("daily price is outside the controlled reclaim zone around the 20-day average")
    else:
        blockers.append("daily history unavailable for pullback/reclaim")

    if quote.day_change_pct is not None and -2.0 <= quote.day_change_pct <= 2.5:
        score += 1
        evidence.append(f"current move is not chased: {pct(quote.day_change_pct)} today")
    else:
        blockers.append(f"current move is too extended or weak for entry: {pct(quote.day_change_pct)} today")
    if sector_change is not None and sector_change >= -0.5:
        score += 1
        evidence.append(f"sector context is not hostile: {pct(sector_change)} avg context ETF move")
    else:
        blockers.append(f"sector context is hostile: {pct(sector_change)} avg context ETF move")
    if (
        intraday.return_15m_pct is not None
        and intraday.return_30m_pct is not None
        and intraday.return_15m_pct >= 0
        and intraday.return_15m_pct >= intraday.return_30m_pct
    ):
        score += 1
        evidence.append(f"intraday reclaim is improving: 15m {pct(intraday.return_15m_pct)} vs 30m {pct(intraday.return_30m_pct)}")
    else:
        blockers.append(f"no intraday reclaim confirmation: 15m {pct(intraday.return_15m_pct)} vs 30m {pct(intraday.return_30m_pct)}")

    return score, evidence, blockers


def score_early_setup(
    ticker: str,
    quote: Quote,
    quotes: dict[str, Quote],
    packet_hits: dict[str, list[str]],
    headlines: list[str],
    source_verifications: dict[str, list[dict]],
    intraday: Intraday,
    history: history_analysis.HistoryFeature | None = None,
    explicit_watchlist_name: bool = False,
) -> tuple[int, list[str], list[str]]:
    evidence: list[str] = []
    blockers: list[str] = []
    score = 0
    combined_text = " ".join(packet_hits.get(ticker, []) + headlines).lower()
    verified_sources = [item for item in source_verifications.get(ticker, []) if item.get("verified")]
    positive_text = any(word in combined_text for word in POSITIVE_WORDS)
    negative_text = any(word in combined_text for word in NEGATIVE_WORDS)
    event_risk = any(pattern.search(combined_text) for pattern in EVENT_RISK_PATTERNS)
    sector_change = sector_change_for(ticker, quotes)
    relative_strength = (
        quote.day_change_pct - sector_change
        if quote.day_change_pct is not None and sector_change is not None
        else None
    )
    history_constructive = bool(
        history
        and not history.error
        and history.sma5 is not None
        and history.sma20 is not None
        and history.sma5 > history.sma20
        and (history.trend_20d_pct is None or history.trend_20d_pct > 0)
    )
    intraday_constructive = bool(
        intraday.return_15m_pct is not None
        and intraday.return_30m_pct is not None
        and intraday.return_15m_pct >= 0.12
        and intraday.return_30m_pct >= 0
    )

    if verified_sources:
        score += 2
        evidence.append("verified ticker-specific catalyst source found")
    else:
        blockers.append("no verified ticker-specific catalyst source")
    if positive_text:
        score += 1
        evidence.append("fresh context has positive language")
    else:
        blockers.append("fresh context lacks positive language")
    if negative_text:
        blockers.append("negative headline language detected")
    if event_risk:
        blockers.append("binary event risk detected")

    if quote.day_change_pct is not None and -0.75 <= quote.day_change_pct <= 2.0:
        score += 1
        evidence.append(f"price is not extended yet: {pct(quote.day_change_pct)} today")
    else:
        blockers.append(f"price already extended or too weak: {pct(quote.day_change_pct)} today")
    if quote.price is not None and quote.price < MIN_BUY_PRICE and not explicit_watchlist_name:
        blockers.append(f"price below durable-trade floor: {quote.price:.2f} < {MIN_BUY_PRICE:.2f}")
    if relative_strength is not None and relative_strength >= 0.35:
        score += 1
        evidence.append(f"relative strength leads {TICKER_SECTOR[ticker]} context by {relative_strength:+.2f} points")
    elif history_constructive and intraday_constructive and sector_change is not None and sector_change >= -0.2:
        score += 1
        evidence.append("constructive pullback/reclaim setup: history trend and intraday acceleration offset limited sector-relative lead")
    else:
        blockers.append(
            f"relative strength lead insufficient: {relative_strength:+.2f} points"
            if relative_strength is not None
            else "relative strength unavailable"
        )
    if intraday.return_15m_pct is not None and intraday.return_15m_pct >= 0.12:
        score += 1
        evidence.append(f"near-term acceleration: {intraday.return_15m_pct:+.2f}% over 15m")
    else:
        blockers.append(
            f"no near-term acceleration: {intraday.return_15m_pct:+.2f}% over 15m"
            if intraday.return_15m_pct is not None
            else "near-term acceleration unavailable"
        )
    if intraday.return_30m_pct is not None and intraday.return_30m_pct >= 0:
        score += 1
        evidence.append(f"30m trend constructive: {intraday.return_30m_pct:+.2f}%")
    else:
        blockers.append(
            f"30m trend not constructive: {intraday.return_30m_pct:+.2f}%"
            if intraday.return_30m_pct is not None
            else "30m trend unavailable"
        )

    if history and not history.error:
        if history.sma5 is not None and history.sma20 is not None and history.sma5 > history.sma20:
            score += 1
            evidence.append(f"history trend constructive: 5-day avg {history.sma5:.2f} > 20-day avg {history.sma20:.2f}")
        else:
            blockers.append("history trend not constructive")
        if history.range_position_60d is not None and history.range_position_60d > 0.97:
            blockers.append(f"history range extended: {history.range_position_60d:.2f} of 60-day range")
        if history.close_vs_sma20_pct is not None and history.volatility_20d_pct is not None:
            extension_limit = max(6.0, history.volatility_20d_pct * 2.5)
            if history.close_vs_sma20_pct > extension_limit:
                blockers.append(
                    f"history extension high: {pct(history.close_vs_sma20_pct)} above 20-day avg exceeds {extension_limit:.2f}% limit"
                )
    elif history and history.error:
        blockers.append(f"history unavailable: {history.error}")
    blockers.extend(average_liquidity_blockers(quote, explicit_watchlist_name))
    if quote.volume is not None and quote.volume < MIN_OPENING_BUY_CURRENT_VOLUME:
        blockers.append(f"current participation too thin: {num(quote.volume)} shares < {num(MIN_OPENING_BUY_CURRENT_VOLUME)}")

    required_blockers = (
        "no verified ticker-specific catalyst",
        "fresh context lacks positive",
        "negative headline",
        "binary event risk",
        "price already extended",
        "price below durable-trade floor",
        "relative strength lead insufficient",
        "no near-term acceleration",
        "history trend not constructive",
        "history unavailable",
        "history range extended",
        "history extension high",
        "average dollar volume too low",
        "average share volume too low",
        "current participation too thin",
    )
    if any(any(required in blocker for required in required_blockers) for blocker in blockers):
        score = min(score, 5)
    return score, evidence, blockers


def score_predictive_setup(
    ticker: str,
    quote: Quote,
    quotes: dict[str, Quote],
    packet_hits: dict[str, list[str]],
    headlines: list[str],
    source_verifications: dict[str, list[dict]],
    intraday: Intraday,
    history: history_analysis.HistoryFeature | None = None,
    explicit_watchlist_name: bool = False,
) -> tuple[int, list[str], list[str]]:
    evidence: list[str] = []
    blockers: list[str] = []
    score = 0
    combined_text = " ".join(packet_hits.get(ticker, []) + headlines).lower()
    verified_sources = [item for item in source_verifications.get(ticker, []) if item.get("verified")]
    positive_text = any(word in combined_text for word in POSITIVE_WORDS)
    negative_text = any(word in combined_text for word in NEGATIVE_WORDS)
    event_risk = any(pattern.search(combined_text) for pattern in EVENT_RISK_PATTERNS)
    sector_change = sector_change_for(ticker, quotes)

    if quote.error:
        blockers.append(f"quote fetch failed: {quote.error}")
        return 0, evidence, blockers
    if verified_sources:
        score += 2
        evidence.append("verified ticker-specific catalyst source found")
    else:
        blockers.append("no verified ticker-specific catalyst source")
    if positive_text:
        score += 1
        evidence.append("fresh context has positive language")
    if negative_text:
        blockers.append("negative headline language detected")
    if event_risk:
        blockers.append("binary event risk detected")

    if quote.day_change_pct is not None and -2.5 <= quote.day_change_pct <= 1.0:
        score += 1
        evidence.append(f"pre-crowd price zone: {pct(quote.day_change_pct)} today")
    else:
        blockers.append(f"not in predictive price zone: {pct(quote.day_change_pct)} today")

    stabilizing = bool(
        intraday.return_15m_pct is not None
        and intraday.return_30m_pct is not None
        and intraday.return_15m_pct >= -0.15
        and intraday.return_30m_pct >= -0.8
        and intraday.return_15m_pct >= intraday.return_30m_pct
    )
    if stabilizing:
        score += 2
        evidence.append(
            f"short-term stabilization: 15m {pct(intraday.return_15m_pct)} vs 30m {pct(intraday.return_30m_pct)}"
        )
    else:
        blockers.append(
            f"no stabilization yet: 15m {pct(intraday.return_15m_pct)} vs 30m {pct(intraday.return_30m_pct)}"
        )

    if history and not history.error:
        trend_constructive = bool(
            history.sma5 is not None
            and history.sma20 is not None
            and history.sma5 > history.sma20
            and (history.trend_20d_pct is None or history.trend_20d_pct >= 0)
        )
        reclaim_zone = bool(
            history.close_vs_sma20_pct is not None
            and -4.0 <= history.close_vs_sma20_pct <= 6.0
        )
        if trend_constructive:
            score += 2
            evidence.append(f"history trend constructive: 5-day avg {history.sma5:.2f} > 20-day avg {history.sma20:.2f}")
        elif reclaim_zone:
            score += 1
            evidence.append(f"near 20-day reclaim zone: {pct(history.close_vs_sma20_pct)} vs 20-day avg")
        else:
            blockers.append("history setup not constructive for rebound")
        if history.range_position_60d is not None and history.range_position_60d > 0.92:
            blockers.append(f"history range too extended for prediction: {history.range_position_60d:.2f} of 60-day range")
    elif history and history.error:
        blockers.append(f"history unavailable: {history.error}")

    if sector_change is not None and sector_change >= -0.3:
        score += 1
        evidence.append(f"{TICKER_SECTOR[ticker]} context not hostile: {pct(sector_change)} avg context ETF move")
    else:
        blockers.append(f"{TICKER_SECTOR[ticker]} context hostile: {pct(sector_change)} avg context ETF move")

    if quote.volume_ratio is not None and quote.volume_ratio >= 0.2:
        score += 1
        evidence.append(f"enough participation for setup: {quote.volume_ratio:.2f}x 10-day full-day avg")
    else:
        blockers.append(
            f"participation too thin for prediction: {quote.volume_ratio:.2f}x 10-day full-day avg"
            if quote.volume_ratio is not None
            else "participation unavailable for prediction"
        )
    if quote.day_change_pct is not None and quote.day_change_pct < 0 and quote.volume_ratio is not None and quote.volume_ratio >= 1.2:
        blockers.append(f"down move has distribution volume: {quote.volume_ratio:.2f}x 10-day full-day avg")
    if quote.price is not None and quote.price < MIN_BUY_PRICE and not explicit_watchlist_name:
        blockers.append(f"price below durable-trade floor: {quote.price:.2f} < {MIN_BUY_PRICE:.2f}")
    blockers.extend(average_liquidity_blockers(quote, explicit_watchlist_name))
    min_current_volume = MIN_OPENING_BUY_CURRENT_VOLUME
    if quote.volume is not None and quote.volume < min_current_volume:
        blockers.append(f"current participation too thin: {num(quote.volume)} shares < {num(min_current_volume)}")

    required_blockers = (
        "no verified ticker-specific catalyst",
        "negative headline",
        "binary event risk",
        "not in predictive price zone",
        "no stabilization yet",
        "history setup not constructive",
        "history unavailable",
        "history range too extended",
        "context hostile",
        "participation too thin",
        "participation unavailable",
        "distribution volume",
        "price below durable-trade floor",
        "average dollar volume too low",
        "average share volume too low",
        "current participation too thin",
    )
    if any(any(required in blocker for required in required_blockers) for blocker in blockers):
        score = min(score, 6)
    return score, evidence, blockers


def score_aggressive_setup(
    ticker: str,
    quote: Quote,
    quotes: dict[str, Quote],
    packet_hits: dict[str, list[str]],
    headlines: list[str],
    source_verifications: dict[str, list[dict]],
    intraday: Intraday,
    history: history_analysis.HistoryFeature | None = None,
    explicit_watchlist_name: bool = False,
) -> tuple[int, list[str], list[str]]:
    evidence: list[str] = []
    blockers: list[str] = []
    score = 0
    combined_text = " ".join(packet_hits.get(ticker, []) + headlines).lower()
    verified_sources = [item for item in source_verifications.get(ticker, []) if item.get("verified")]
    positive_text = any(word in combined_text for word in POSITIVE_WORDS)
    negative_text = any(word in combined_text for word in NEGATIVE_WORDS)
    event_risk = any(pattern.search(combined_text) for pattern in EVENT_RISK_PATTERNS)
    sector_change = sector_change_for(ticker, quotes)
    leveraged_sector_proxy = (
        (ticker in BULL_LEVERAGED_ETFS and sector_change is not None and sector_change >= 0.5)
        or (ticker in INVERSE_LEVERAGED_ETFS and sector_change is not None and sector_change <= -0.5)
    )

    if quote.error:
        blockers.append(f"quote fetch failed: {quote.error}")
        return 0, evidence, blockers
    if verified_sources:
        score += 2
        evidence.append("verified ticker-specific catalyst source found")
    elif leveraged_sector_proxy:
        score += 1
        evidence.append("leveraged ETF sector-momentum proxy is strong")
    else:
        blockers.append("no verified catalyst or strong leveraged-ETF sector proxy")
    if positive_text and ticker not in INVERSE_LEVERAGED_ETFS:
        score += 1
        evidence.append("fresh context has positive language")
    elif positive_text:
        blockers.append("bullish context conflicts with inverse leveraged ETF")
    if negative_text and ticker in INVERSE_LEVERAGED_ETFS:
        score += 1
        evidence.append("bearish context supports inverse leveraged ETF")
    elif negative_text:
        blockers.append("negative headline language detected")
    if event_risk:
        blockers.append("binary event risk detected")

    if quote.day_change_pct is not None and AGGRESSIVE_BUY_MIN_CHANGE <= quote.day_change_pct <= AGGRESSIVE_BUY_MAX_CHANGE:
        score += 3
        evidence.append(f"price is already moving: {pct(quote.day_change_pct)} today")
    else:
        blockers.append(
            f"price not in aggressive momentum band: {pct(quote.day_change_pct)} "
            f"outside {AGGRESSIVE_BUY_MIN_CHANGE:.1f}% to {AGGRESSIVE_BUY_MAX_CHANGE:.1f}%"
        )
    if quote.volume_ratio is not None and quote.volume_ratio >= AGGRESSIVE_BUY_MIN_VOLUME_RATIO:
        score += 2
        evidence.append(f"unusual participation: {quote.volume_ratio:.2f}x 10-day full-day avg")
    else:
        blockers.append(
            f"volume not aggressive enough: {quote.volume_ratio:.2f}x < {AGGRESSIVE_BUY_MIN_VOLUME_RATIO:.2f}x"
            if quote.volume_ratio is not None
            else "volume not aggressive enough: unavailable"
        )
    if ticker in INVERSE_LEVERAGED_ETFS:
        sector_supports = sector_change is not None and sector_change <= -0.35
        sector_message = f"{TICKER_SECTOR[ticker]} context supports inverse momentum: {pct(sector_change)} avg context ETF move"
        sector_blocker = f"{TICKER_SECTOR[ticker]} context not weak enough for inverse ETF: {pct(sector_change)} avg context ETF move"
    else:
        sector_supports = sector_change is not None and sector_change >= 0.35
        sector_message = f"{TICKER_SECTOR[ticker]} context supports momentum: {pct(sector_change)} avg context ETF move"
        sector_blocker = f"{TICKER_SECTOR[ticker]} context not strong enough: {pct(sector_change)} avg context ETF move"
    if sector_supports:
        score += 1
        evidence.append(sector_message)
    else:
        blockers.append(sector_blocker)
    if intraday.return_15m_pct is not None and intraday.return_15m_pct >= -0.05:
        score += 1
        evidence.append(f"near-term momentum holding: 15m {pct(intraday.return_15m_pct)}")
    else:
        blockers.append(f"near-term momentum fading: 15m {pct(intraday.return_15m_pct)}")
    if intraday.return_30m_pct is not None and intraday.return_30m_pct >= 0.15:
        score += 1
        evidence.append(f"30m momentum positive: {pct(intraday.return_30m_pct)}")
    else:
        blockers.append(f"30m momentum not strong: {pct(intraday.return_30m_pct)}")

    if quote.price is not None and quote.price < MIN_BUY_PRICE and not explicit_watchlist_name and ticker not in LEVERAGED_ETFS:
        blockers.append(f"price below durable-trade floor: {quote.price:.2f} < {MIN_BUY_PRICE:.2f}")
    blockers.extend(average_liquidity_blockers(quote, explicit_watchlist_name or ticker in LEVERAGED_ETFS))
    min_current_volume = MIN_OPENING_BUY_CURRENT_VOLUME if quote.price and quote.price >= 20 else MIN_BUY_CURRENT_VOLUME
    if quote.volume is not None and quote.volume < min_current_volume:
        blockers.append(f"current participation too thin: {num(quote.volume)} shares < {num(min_current_volume)}")
    if history and not history.error:
        if history.range_position_60d is not None and history.range_position_60d > 0.99:
            blockers.append(f"history range extremely extended: {history.range_position_60d:.2f} of 60-day range")
        if history.close_vs_sma20_pct is not None and history.volatility_20d_pct is not None:
            extension_limit = max(10.0, history.volatility_20d_pct * 3.0)
            if history.close_vs_sma20_pct > extension_limit:
                blockers.append(
                    f"history extension too high for aggressive entry: {pct(history.close_vs_sma20_pct)} exceeds {extension_limit:.2f}%"
                )

    required_blockers = (
        "quote fetch failed",
        "no verified catalyst",
        "negative headline",
        "binary event risk",
        "price not in aggressive momentum band",
        "volume not aggressive enough",
        "context not strong enough",
        "near-term momentum fading",
        "price below durable-trade floor",
        "average dollar volume too low",
        "average share volume too low",
        "current participation too thin",
        "history range extremely extended",
        "history extension too high",
    )
    if any(any(required in blocker for required in required_blockers) for blocker in blockers):
        score = min(score, 6)
    return score, evidence, blockers


def score_ticker(
    ticker: str,
    quote: Quote,
    quotes: dict[str, Quote],
    packet_hits: dict[str, list[str]],
    headlines: list[str],
    window: str,
    source_verifications: dict[str, list[dict]],
    history: history_analysis.HistoryFeature | None = None,
    explicit_watchlist_name: bool = False,
) -> tuple[int, list[str], list[str]]:
    sector = TICKER_SECTOR[ticker]
    thresholds = signal_thresholds(window)
    sector_quotes = [quotes[t] for t in CONTEXT[sector] if t in quotes and quotes[t].day_change_pct is not None]
    sector_change = statistics.mean(q.day_change_pct for q in sector_quotes if q.day_change_pct is not None) if sector_quotes else None
    evidence = []
    blockers = []
    score = 0

    combined_text = " ".join(packet_hits.get(ticker, []) + headlines).lower()
    has_catalyst = any(word in combined_text for word in CATALYST_WORDS)
    verified_sources = [item for item in source_verifications.get(ticker, []) if item.get("verified")]
    positive_text = any(word in combined_text for word in POSITIVE_WORDS)
    negative_text = any(word in combined_text for word in NEGATIVE_WORDS)
    event_risk = any(pattern.search(combined_text) for pattern in EVENT_RISK_PATTERNS)

    if quote.error:
        blockers.append(f"quote fetch failed: {quote.error}")
        return 0, evidence, blockers
    if verified_sources:
        score += 2
        evidence.append("verified catalyst source found")
    elif has_catalyst:
        score += 1
        evidence.append("unverified catalyst mention found")
        blockers.append("no downloaded source verified the ticker-specific catalyst")
    else:
        blockers.append("no fresh catalyst found in latest packets/headlines")
    if positive_text:
        score += 1
        evidence.append("headline language leans positive")
    if negative_text:
        score -= 2
        blockers.append("negative headline language detected")
    if event_risk:
        score -= 2
        blockers.append("binary event risk detected; wait for post-event confirmation")
    if quote.day_change_pct is not None and quote.day_change_pct >= thresholds["min_change"]:
        score += 2
        evidence.append(f"price confirms: {pct(quote.day_change_pct)} today")
    else:
        blockers.append(f"price confirmation weak: {pct(quote.day_change_pct)} today")
    if quote.day_change_pct is not None and quote.day_change_pct > thresholds["max_change"]:
        blockers.append(
            f"price already extended: {pct(quote.day_change_pct)} exceeds {thresholds['max_change']:.1f}% anti-chase limit"
        )
    if quote.price is not None and quote.price < MIN_BUY_PRICE and not explicit_watchlist_name:
        blockers.append(f"price below durable-trade floor: {quote.price:.2f} < {MIN_BUY_PRICE:.2f}")
    if history and not history.error:
        if history.sma5 is not None and history.sma20 is not None and history.sma5 > history.sma20:
            score += 1
            evidence.append(f"history trend supports: 5-day avg {history.sma5:.2f} > 20-day avg {history.sma20:.2f}")
        elif history.sma5 is not None and history.sma20 is not None:
            blockers.append(f"history trend weak: 5-day avg {history.sma5:.2f} <= 20-day avg {history.sma20:.2f}")
        if history.range_position_60d is not None and history.range_position_60d > 0.97:
            blockers.append(f"history range extended: {history.range_position_60d:.2f} of 60-day range")
        if history.close_vs_sma20_pct is not None and history.volatility_20d_pct is not None:
            extension_limit = max(6.0, history.volatility_20d_pct * 2.5)
            if history.close_vs_sma20_pct > extension_limit:
                blockers.append(
                    f"history extension high: {pct(history.close_vs_sma20_pct)} above 20-day avg exceeds {extension_limit:.2f}% limit"
                )
    elif history and history.error:
        blockers.append(f"history unavailable: {history.error}")
    if sector_change is not None and sector_change >= thresholds["min_sector"]:
        score += 1
        evidence.append(f"{sector} context confirms: {pct(sector_change)} avg context ETF move")
    else:
        blockers.append(f"{sector} context not confirming: {pct(sector_change)} avg context ETF move")
    if quote.volume_ratio is not None and quote.volume_ratio >= thresholds["min_volume"]:
        score += 1
        evidence.append(f"volume pace acceptable: {quote.volume_ratio:.2f}x 10-day full-day avg")
    elif window in {"one-hour", "midday", "late-day"}:
        blockers.append(f"volume confirmation weak: {quote.volume_ratio:.2f}x 10-day full-day avg" if quote.volume_ratio is not None else "volume confirmation unavailable")
    blockers.extend(average_liquidity_blockers(quote, explicit_watchlist_name))
    min_current_volume = MIN_OPENING_BUY_CURRENT_VOLUME if window == "opening" else MIN_BUY_CURRENT_VOLUME
    if quote.volume is not None and quote.volume < min_current_volume:
        blockers.append(f"current participation too thin: {num(quote.volume)} shares < {num(min_current_volume)}")
    return score, evidence, blockers


def intraday_paper_signals(tickers: list[str], open_positions: dict[str, dict]) -> dict[str, dict]:
    """Fetch a small bounded set of Schwab five-minute charts for paper alerts.

    This is intentionally separate from the normal quote feed.  A failed chart
    never changes a normal BUY/SELL decision; it only marks that paper model
    unavailable for the report.
    """
    if not INTRADAY_PAPER_ENABLED:
        return {}
    # Held names are always included so matching intraday exit signals are not
    # starved by the bounded universe.  The rest is deterministic for audit.
    ordered = list(dict.fromkeys([*sorted(open_positions), *sorted(tickers)]))[:INTRADAY_PAPER_MAX_TICKERS]
    try:
        config = market_schwab.load_env()
    except Exception as exc:  # noqa: BLE001
        return {ticker: {"symbol": ticker, "status": "unavailable", "signals": [], "reason": f"Schwab config failed: {type(exc).__name__}"} for ticker in ordered}

    def fetch_one(ticker: str) -> tuple[str, dict]:
        try:
            payload = market_schwab.fetch_intraday_history(config, ticker, days=1, frequency=5)
            candles = payload.get("candles") if isinstance(payload, dict) else []
            return ticker, market_intraday_models.analyze(ticker, candles if isinstance(candles, list) else [])
        except Exception as exc:  # noqa: BLE001
            return ticker, {"symbol": ticker, "status": "unavailable", "signals": [], "reason": f"Schwab five-minute history failed: {type(exc).__name__}: {exc}"}

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, INTRADAY_PAPER_WORKERS)) as pool:
        return dict(pool.map(fetch_one, ordered))


def apply_intraday_paper_buy_price_gates(
    paper_signals: dict[str, dict],
    quotes: dict[str, Quote],
    explicit_watchlist: set[str],
    schwab_buying_power: float | None,
) -> dict[str, dict]:
    """Apply the normal price/whole-share protections to intraday paper BUYs.

    Intraday alerts remain research-only, but a paper BUY should not advertise a
    name that is below the durable-price floor, cannot fit the whole-share cap,
    or exceeds known cash buying power.  Bearish paper signals are retained;
    they are handled separately by the notification position rule.
    """
    filtered: dict[str, dict] = {}
    for ticker, paper in paper_signals.items():
        if not isinstance(paper, dict):
            continue
        record = dict(paper)
        signals = paper.get("signals") if isinstance(paper.get("signals"), list) else []
        quote = quotes.get(ticker)
        blocker = ""
        price = quote.price if quote else None
        if price is None:
            blocker = "quote price unavailable for intraday paper BUY"
        elif price < MIN_BUY_PRICE and ticker not in explicit_watchlist:
            blocker = f"price below durable-trade floor: {price:.2f} < {MIN_BUY_PRICE:.2f}"
        elif WHOLE_SHARE_PROTECTIVE_STOPS_ONLY and price > MAX_WHOLE_SHARE_TRADE_VALUE:
            blocker = (
                f"whole-share protective-stop gate: {money(price)} share price exceeds "
                f"${MAX_WHOLE_SHARE_TRADE_VALUE:.2f} transaction cap"
            )
        elif CASH_ONLY_NO_MARGIN and schwab_buying_power is not None and price > schwab_buying_power:
            blocker = f"Schwab buying power too low: {money(price)} share price exceeds {money(schwab_buying_power)}"

        if blocker:
            removed = [signal for signal in signals if isinstance(signal, dict) and signal.get("side") == "BUY"]
            signals = [signal for signal in signals if not (isinstance(signal, dict) and signal.get("side") == "BUY")]
            if removed:
                record["suppressed_buy_models"] = [str(signal.get("model")) for signal in removed]
                record["buy_gate_blocker"] = blocker
        record["signals"] = signals
        filtered[ticker] = record
    return filtered


def apply_intraday_paper_existing_position_gate(
    paper_signals: dict[str, dict],
    open_positions: dict[str, dict],
) -> dict[str, dict]:
    """Suppress intraday paper BUY alerts for symbols already held.

    Intraday alerts are research-only, but a held symbol must not create a
    duplicate BUY recommendation.  Existing bearish signals remain available
    for the separate held/unheld position gate.
    """
    gated: dict[str, dict] = {}
    for ticker, paper in paper_signals.items():
        if not isinstance(paper, dict):
            continue
        record = dict(paper)
        signals = paper.get("signals") if isinstance(paper.get("signals"), list) else []
        if ticker in open_positions:
            removed = [signal for signal in signals if isinstance(signal, dict) and signal.get("side") == "BUY"]
            signals = [signal for signal in signals if not (isinstance(signal, dict) and signal.get("side") == "BUY")]
            if removed:
                record["suppressed_buy_models"] = [str(signal.get("model")) for signal in removed]
                record["buy_gate_blocker"] = "already held; no duplicate intraday BUY recommendation"
        record["signals"] = signals
        gated[ticker] = record
    return gated


def apply_intraday_paper_position_gate(
    paper_signals: dict[str, dict],
    open_positions: dict[str, dict],
) -> dict[str, dict]:
    """Prevent unheld intraday paper SELL alerts.

    Intraday models are research-only.  When a bearish signal appears for a
    symbol with no open position, render it as CANCEL BUY guidance instead of
    a SELL recommendation.  Held symbols retain their PAPER SELL alert.
    """
    gated: dict[str, dict] = {}
    for ticker, paper in paper_signals.items():
        if not isinstance(paper, dict):
            continue
        record = dict(paper)
        signals = paper.get("signals") if isinstance(paper.get("signals"), list) else []
        if ticker not in open_positions:
            converted = []
            for signal in signals:
                if isinstance(signal, dict) and signal.get("side") == "SELL":
                    converted.append({
                        **signal,
                        "side": "CANCEL_BUY",
                        "reason": "no open position; cancel any pending BUY rather than issue a SELL recommendation",
                    })
                else:
                    converted.append(signal)
            signals = converted
        record["signals"] = signals
        gated[ticker] = record
    return gated


def rank_intraday_paper_alerts(
    paper_intraday: dict[str, dict],
) -> tuple[list[tuple[int, str, list[str]]], list[tuple[int, str, list[str]]], list[tuple[int, str, list[str]]]]:
    """Return BUY, SELL, and CANCEL BUY alerts ranked by signal strength.

    Scores rank agreement among independent intraday models; they are not
    estimated returns or a claim of profitability.
    """
    ranked: dict[str, list[tuple[int, str, list[str]]]] = {"BUY": [], "SELL": [], "CANCEL_BUY": []}
    for ticker, paper in paper_intraday.items():
        if not isinstance(paper, dict):
            continue
        signals = paper.get("signals") if isinstance(paper.get("signals"), list) else []
        scores = paper.get("scores") if isinstance(paper.get("scores"), dict) else {}
        for side in ranked:
            models = [str(signal.get("model")) for signal in signals if isinstance(signal, dict) and signal.get("side") == side]
            if models:
                score_key = "SELL" if side == "CANCEL_BUY" else side
                ranked[side].append((int(scores.get(score_key, 0) or 0), ticker, models))
    for alerts in ranked.values():
        alerts.sort(key=lambda item: (-item[0], item[1]))
    return ranked["BUY"], ranked["SELL"], ranked["CANCEL_BUY"]


def build_report(
    window: str,
    tickers: list[str] | None = None,
    job_id: str | None = None,
    job_thesis: str | None = None,
    source_packet: str | None = None,
) -> tuple[Path, str]:
    started = time.monotonic()

    def log_stage(name: str) -> None:
        elapsed = time.monotonic() - started
        print(f"stage {name}: {elapsed:.1f}s", file=sys.stderr, flush=True)

    now = dt.datetime.now(TZ)
    valid_until = valid_until_for(window, now)
    OUT_DIR.mkdir(exist_ok=True)
    open_positions = read_positions()
    all_broker_positions = read_all_broker_positions()
    explicit_watchlist = explicit_watchlist_symbols()
    for ticker in explicit_watchlist:
        TICKER_SECTOR.setdefault(ticker, "consumer")
    news_candidates = packet_news_candidates()
    for ticker, sector in news_candidates.items():
        TICKER_SECTOR.setdefault(ticker, sector)
    idea_symbols = idea_screen_symbols(now)
    catalyst_symbols = catalyst_calendar_symbols(now)
    for ticker in idea_symbols | catalyst_symbols:
        TICKER_SECTOR.setdefault(ticker, "consumer")
    requested = [ticker.upper() for ticker in tickers] if tickers else sorted(
        set(ALL_TICKERS) | set(open_positions) | set(news_candidates) | explicit_watchlist | idea_symbols | catalyst_symbols
    )
    selected = [ticker for ticker in requested if ticker in TICKER_SECTOR]
    if not selected:
        raise SystemExit(f"no valid tickers selected: {', '.join(requested)}")
    log_stage(f"selected {len(selected)} tickers")
    quote_tickers = sorted(set(selected) | {ctx for ticker in selected for ctx in CONTEXT[TICKER_SECTOR[ticker]]})
    packet_hits = packet_mentions(selected)
    log_stage("packet mentions")
    source_verifications = verify_sources(selected)
    log_stage("source verification")
    quotes = fetch_primary_quotes(quote_tickers)
    log_stage("quotes")
    intraday = fetch_nasdaq_intraday_batch(selected)
    log_stage("intraday")
    history_features = history_analysis.features_for_symbols(selected)
    log_stage("history")
    headlines = {ticker: fetch_rss_headlines(ticker) for ticker in selected}
    log_stage("rss headlines")
    catalyst_events = update_catalyst_calendar(selected, packet_hits, headlines, source_verifications, now)
    thesis_state = sync_thesis_tracker(open_positions, packet_hits, headlines, now)
    schwab_buying_power, schwab_buying_power_status = read_schwab_buying_power()
    traded_this_session = same_session_traded_symbols(now, open_positions)
    log_stage("state")
    paper_intraday = intraday_paper_signals(selected, all_broker_positions)
    paper_intraday = apply_intraday_paper_buy_price_gates(
        paper_intraday, quotes, explicit_watchlist, schwab_buying_power
    )
    paper_intraday = apply_intraday_paper_existing_position_gate(paper_intraday, all_broker_positions)
    paper_intraday = apply_intraday_paper_position_gate(paper_intraday, open_positions)
    log_stage("intraday paper models")

    rows = []
    for ticker in selected:
        explicit_watchlist_name = ticker in explicit_watchlist or ticker in open_positions
        binary_risks = binary_event_risk_for_ticker(ticker, packet_hits, headlines[ticker], catalyst_events, now)
        post_earnings_binary_risks = binary_event_risk_for_ticker(
            ticker, packet_hits, headlines[ticker], catalyst_events, now, post_earnings_mode=True
        )
        pullback_setup = score_pullback_reclaim_setup(
            ticker, quotes[ticker], quotes, packet_hits, headlines[ticker], source_verifications, intraday[ticker],
            history_features.get(ticker),
            explicit_watchlist_name,
            binary_risks,
        )
        entry_models = {
            "evidence_revision": score_evidence_revision_momentum_setup(
                ticker, quotes[ticker], quotes, packet_hits, headlines[ticker], source_verifications, intraday[ticker],
                history_features.get(ticker), explicit_watchlist_name, binary_risks,
            ),
            "catalyst_trend": score_catalyst_trend_continuation_setup(
                ticker, quotes[ticker], quotes, packet_hits, headlines[ticker], source_verifications, intraday[ticker],
                history_features.get(ticker), explicit_watchlist_name, binary_risks,
            ),
            "post_earnings": score_post_earnings_drift_setup(
                ticker, quotes[ticker], quotes, packet_hits, headlines[ticker], source_verifications, intraday[ticker],
                history_features.get(ticker), explicit_watchlist_name, post_earnings_binary_risks,
            ),
            "sector_breakout": score_sector_leader_breakout_setup(
                ticker, quotes[ticker], quotes, packet_hits, headlines[ticker], source_verifications, intraday[ticker],
                history_features.get(ticker), explicit_watchlist_name, binary_risks,
            ),
            "news_dislocation": score_news_dislocation_setup(
                ticker, quotes[ticker], quotes, packet_hits, headlines[ticker], source_verifications, intraday[ticker],
                history_features.get(ticker), explicit_watchlist_name, binary_risks,
            ),
        }
        action, decision_score, decision, decision_evidence, decision_blockers = classify_decision(
            ticker, 0, [], [], window, open_positions, quotes[ticker], quotes, packet_hits, headlines[ticker],
            pullback_setup,
            history_features.get(ticker),
            thesis_state,
            entry_models,
        )
        rows.append((action, decision_score, ticker, decision, decision_evidence, decision_blockers))
    rows, trade_holdoff_notes = apply_same_session_trade_holdoff(rows, traded_this_session)
    rows, existing_position_notes = apply_existing_position_buy_gate(rows, all_broker_positions)
    rows, broker_eligibility_notes = apply_broker_eligibility_gate(rows)
    rows, account_fit_notes = apply_account_fit_gate(rows, quotes, None)
    rows, schwab_balance_notes = apply_schwab_balance_gate(rows, quotes, schwab_buying_power, schwab_buying_power_status)
    idea_screen_state = update_idea_screen(rows, quotes, history_features, source_verifications, now)
    buy_actions = {"predictive_buy", "aggressive_buy", "early_buy", "buy"}
    trade_plans = {
        row[2]: trade_plan_for_buy(row[0], row[2], quotes[row[2]], history_features.get(row[2]))
        for row in rows
        if row[0] in buy_actions
    }
    log_stage("scoring")
    priority = {"sell": 0, "predictive_buy": 1, "aggressive_buy": 2, "early_buy": 3, "buy": 4, "hold": 5, "watch": 6, "no_buy": 7}
    rows.sort(key=lambda row: (priority.get(row[0], 9), -row[1], row[2]))

    lines = [
        "source: market-signal-confirm",
        "status: proposed",
        "target: market-scout",
        f"confirmation_profile: {CONFIRMATION_PROFILE}",
        f"position_brokers: {', '.join(sorted(POSITION_BROKERS)) if POSITION_BROKERS else 'all'}",
        f"confirmation_window: {window}",
        f"generated_at: {now.isoformat()}",
        f"valid_until: {valid_until.isoformat()}",
        "policy: research alert only; no orders executed; permits defensive same-day exits and defaults to holding through weekends unless a sell signal triggers",
        f"schwab_buying_power: {schwab_buying_power:.2f}" if schwab_buying_power is not None else f"schwab_buying_power: unavailable ({schwab_buying_power_status})",
        f"schwab_balance_gate: {'enabled' if SCHWAB_BALANCE_GATE_ENABLED else 'disabled'}",
        f"max_whole_share_trade_value: {MAX_WHOLE_SHARE_TRADE_VALUE:.2f}",
        f"whole_share_protective_stops_only: {str(WHOLE_SHARE_PROTECTIVE_STOPS_ONLY).lower()}",
        f"cash_only_no_margin: {str(CASH_ONLY_NO_MARGIN).lower()}",
        f"same_session_trade_holdoff_symbols: {', '.join(sorted(traded_this_session)) if traded_this_session else 'none'}",
        f"broker_buy_excluded_symbols: {', '.join(sorted(read_buy_excluded_symbols())) or 'none'}",
        f"idea_screen_candidates: {len((idea_screen_state.get('candidates') or {}))}",
        f"catalyst_calendar_symbols: {len(catalyst_events)}",
        f"thesis_tracker_positions: {len((thesis_state.get('positions') or {})) if isinstance(thesis_state.get('positions'), dict) else 0}",
        f"schwab_quotes: {'enabled' if SCHWAB_QUOTES_ENABLED else 'disabled'}",
    ]
    if job_id:
        lines.append(f"job_id: {job_id}")
    if source_packet:
        lines.append(f"source_packet: {source_packet}")
    if job_thesis:
        lines.extend(["", "## queued_thesis", job_thesis])
    lines.extend(
        [
            "",
            "## action_now",
            f"- This report supersedes prior market-worker recommendations and expires at {valid_until.strftime('%H:%M %Z')}.",
            "- SELL SIGNAL means an open position has enough price, sector, or headline deterioration to consider exiting.",
            f"- Schwab balance gate is {'enabled' if SCHWAB_BALANCE_GATE_ENABLED else 'disabled'}; when enabled and buying power is known, BUY-style calls must fit at least one whole share.",
            f"- Account-fit gate {'blocks' if (WHOLE_SHARE_PROTECTIVE_STOPS_ONLY or CASH_ONLY_NO_MARGIN) else 'does not block'} BUY-style calls based on whole-share/cash-only constraints.",
            "- Broker/tax eligibility gate blocks BUY-style calls for symbols configured as unsupported or tax-complicated.",
            "- BUY CANDIDATE can qualify through catalyst + relative-strength continuation, post-earnings drift, sector-leader breakout, news-dislocation stabilization, or pullback/reclaim. Every BUY names all qualifying model(s).",
            "- INTRADAY PAPER BUY/SELL is a completed-five-minute-bar research alert only. BUY uses opening-range breakout, VWAP reclaim, or trend-pullback continuation; held symbols do not receive PAPER BUY and may receive PAPER SELL. For unheld symbols, bearish intraday signals become CANCEL BUY guidance. It never submits an order or overrides the normal position SELL model.",
            "- News-dislocation reads normalized RSS/Atom and email-derived packet evidence. It requires an independently verified source, no fundamental-damage terms, abnormal volume, a bounded stock-specific drop, and an intraday stabilization trigger.",
            "- For every BUY CANDIDATE, you choose whether to record a paper trade or make an actual Schwab trade. No orders are placed automatically.",
            "- BUY candidates include a max-entry ceiling plus advisory stop-limit details only for use if you elect an actual trade.",
            "- Same-session trade holdoff blocks another BUY or SELL for any symbol whose position already changed during the current CT session.",
            "- Existing-position gate blocks BUY-style calls for tickers already held in Schwab.",
            "- Idea screen, catalyst calendar, and thesis tracker files are advisory inputs; recommendations still require quote/source confirmation.",
            "- This workflow uses Schwab and public market-data sources only; no Robinhood integration is invoked.",
            "- Earnings or other binary-event risk blocks BUY-style calls until post-event confirmation.",
            "- WATCH means price/catalyst are interesting but at least one required confirmation is missing.",
            "- HOLD means the feedback ledger says you hold it and the sell threshold did not trigger.",
            "",
            "## decisions",
        ]
    )
    sells = [row for row in rows if row[0] == "sell"]
    buys = [row for row in rows if row[0] == "buy"]
    holds = [row for row in rows if row[0] == "hold"]
    watches = [row for row in rows if row[0] == "watch"]
    paper_buys, paper_sells, paper_cancel_buys = rank_intraday_paper_alerts(paper_intraday)
    for score, ticker, models in paper_sells:
        lines.append(f"- INTRADAY PAPER SELL: {ticker} - score {score}; models: {', '.join(models)}; research alert only")
    for score, ticker, models in paper_cancel_buys:
        lines.append(f"- CANCEL BUY: {ticker} - score {score}; intraday bearish model ({', '.join(models)}) and no open position; research alert only")
    for score, ticker, models in paper_buys:
        protection_text = intraday_buy_protection_text(paper_intraday.get(ticker, {}))
        lines.append(f"- INTRADAY PAPER BUY: {ticker} - score {score}; models: {', '.join(models)}; {protection_text}; research alert only")
    if sells:
        lines.extend(f"- {row[3]} (sell score {row[1]})" for row in sells)
    if buys:
        lines.extend(f"- {row[3]} (entry score {row[1]})" for row in buys)
    else:
        lines.append("- NO BUY SIGNALS")
    lines.extend(f"- {row[3]} (score {row[1]})" for row in holds[:5])
    lines.extend(f"- {row[3]} (score {row[1]})" for row in watches[:5])
    if paper_buys or paper_sells or paper_cancel_buys:
        lines.extend(["", "## intraday_paper_details"])
        ranked_paper = sorted(
            paper_intraday.items(),
            key=lambda item: (
                -max((item[1].get("scores") or {}).values(), default=0),
                item[0],
            ),
        )
        for ticker, paper in ranked_paper:
            signals = paper.get("signals") if isinstance(paper, dict) else []
            if not isinstance(signals, list) or not signals:
                continue
            metrics = paper.get("metrics") or {}
            scores = paper.get("scores") or {}
            lines.append(f"### intraday paper {ticker}")
            lines.append(f"- signal_strength_scores: BUY {scores.get('BUY', 0)}; SELL {scores.get('SELL', 0)}; not a profitability forecast")
            if any(signal.get("side") == "BUY" for signal in signals if isinstance(signal, dict)):
                lines.append(f"- protection: {intraday_buy_protection_text(paper)}")
            lines.append(
                f"- levels: close {metrics.get('last_close', 'n/a')}; OR high {metrics.get('opening_high', 'n/a')}; OR low {metrics.get('opening_low', 'n/a')}; VWAP {metrics.get('vwap', 'n/a')}"
            )
            lines.extend(f"- {signal.get('side')}: {signal.get('model')} — {signal.get('reason')}" for signal in signals if isinstance(signal, dict))
    if trade_plans:
        lines.extend(["", "## buy_trade_plans"])
        for row in buys:
            plan = trade_plans.get(row[2])
            if not plan:
                continue
            lines.append(f"- {row[2]}: {trailing_stop_limit_plan_text(plan)}")
            lines.append(f"- {row[2]}: {trailing_buy_plan_text(plan)}")
            lines.extend(f"  - {note}" for note in plan.notes)
    if existing_position_notes:
        lines.extend(["", "## existing_position_gate"])
        lines.extend(f"- {note}" for note in existing_position_notes)
    if schwab_balance_notes:
        lines.extend(["", "## schwab_balance_gate"])
        lines.extend(f"- {note}" for note in schwab_balance_notes)
    if account_fit_notes:
        lines.extend(["", "## account_fit_gate"])
        lines.extend(f"- {note}" for note in account_fit_notes)
    if broker_eligibility_notes:
        lines.extend(["", "## broker_eligibility_gate"])
        lines.extend(f"- {note}" for note in broker_eligibility_notes)
    if trade_holdoff_notes:
        lines.extend(["", "## same_session_trade_holdoff"])
        lines.extend(f"- {note}" for note in trade_holdoff_notes)
    if idea_screen_state.get("candidates"):
        lines.extend(["", "## idea_screen"])
        for ticker, candidate in list((idea_screen_state.get("candidates") or {}).items())[:10]:
            reasons = "; ".join((candidate.get("reasons") or [])[:3]) if isinstance(candidate, dict) else ""
            score_text = candidate.get("score") if isinstance(candidate, dict) else "n/a"
            lines.append(f"- {ticker}: score {score_text}; {reasons}".rstrip("; "))
    if catalyst_events:
        lines.extend(["", "## catalyst_calendar"])
        for ticker in sorted(catalyst_events)[:15]:
            rows_for_ticker = catalyst_events.get(ticker) or []
            for event in rows_for_ticker[:2]:
                if not isinstance(event, dict):
                    continue
                date_text = event.get("event_date") or "date unknown"
                lines.append(f"- {ticker}: {event.get('type', 'event')} {date_text}; {str(event.get('snippet') or '')[:180]}")
    if open_positions:
        lines.extend(["", "## open_positions"])
        for ticker in sorted(open_positions):
            pos = open_positions[ticker]
            detail = []
            if pos.get("shares") is not None:
                detail.append(f"shares {pos['shares']}")
            if pos.get("avg_price") is not None:
                detail.append(f"avg_price {pos['avg_price']}")
            if pos.get("bought_at"):
                detail.append(f"bought_at {pos['bought_at']}")
            if pos.get("brokers"):
                detail.append(f"brokers {','.join(pos['brokers'])}")
            elif pos.get("broker"):
                detail.append(f"broker {pos['broker']}")
            lines.append(f"- {ticker}: {'; '.join(detail) if detail else 'open'}")
    lines.extend(
        [
            "",
            "## feedback_command",
            "- Schwab holdings refresh automatically before market-worker checks.",
            "- After buying at Schwab: `./market_position_feedback.py buy TICKER --shares SHARES --price PRICE --broker schwab --note \"why\"`",
            "- After exiting at Schwab: `./market_position_feedback.py close TICKER --price PRICE --broker schwab --note \"why\"`",
        ]
    )
    lines.extend(["", "## full_check"])
    for _, score, ticker, decision, evidence, blockers in rows:
        q = quotes[ticker]
        lines.append(f"### {ticker}")
        lines.append(f"- decision: {decision}")
        lines.append(f"- score: {score}")
        plan = trade_plans.get(ticker)
        if plan:
            lines.append(f"- trade_plan: {trailing_stop_limit_plan_text(plan)}")
            lines.append(f"- trailing_buy_plan: {trailing_buy_plan_text(plan)}")
            lines.append("- trade_plan_notes:")
            lines.extend(f"  - {note}" for note in plan.notes)
        if ticker in catalyst_events:
            lines.append("- catalyst_events:")
            for event in catalyst_events[ticker][:3]:
                if isinstance(event, dict):
                    lines.append(f"  - {event.get('type', 'event')} {event.get('event_date') or 'date unknown'}: {str(event.get('snippet') or '')[:220]}")
        thesis_record = (thesis_state.get("positions") or {}).get(ticker) if isinstance(thesis_state.get("positions"), dict) else None
        if isinstance(thesis_record, dict) and thesis_record.get("status") == "open":
            lines.append(f"- thesis: {str(thesis_record.get('thesis') or '')[:220]}")
            if thesis_record.get("invalidation_terms"):
                lines.append(f"- thesis_invalidation_terms: {', '.join(str(term) for term in thesis_record.get('invalidation_terms')[:8])}")
        lines.append(f"- quote: price {q.price if q.price is not None else 'n/a'}; day {pct(q.day_change_pct)}; volume {num(q.volume)}; volume_ratio {q.volume_ratio:.2f}x" if q.volume_ratio is not None else f"- quote: price {q.price if q.price is not None else 'n/a'}; day {pct(q.day_change_pct)}; volume {num(q.volume)}; volume_ratio n/a")
        chart = intraday[ticker]
        lines.append(
            f"- intraday: 15m {pct(chart.return_15m_pct)}; 30m {pct(chart.return_30m_pct)}; from_open {pct(chart.from_open_pct)}"
        )
        paper = paper_intraday.get(ticker, {})
        if paper.get("status") == "ok":
            metrics = paper.get("metrics") or {}
            signals = paper.get("signals") or []
            lines.append(
                f"- intraday_paper: close {metrics.get('last_close', 'n/a')}; OR high {metrics.get('opening_high', 'n/a')}; OR low {metrics.get('opening_low', 'n/a')}; VWAP {metrics.get('vwap', 'n/a')}; volume_confirmed {metrics.get('volume_confirmed', False)}"
            )
            if signals:
                lines.append("- intraday_paper_signals:")
                lines.extend(f"  - {signal.get('side')}: {signal.get('model')} — {signal.get('reason')}" for signal in signals if isinstance(signal, dict))
        elif paper:
            lines.append(f"- intraday_paper: unavailable; {paper.get('reason', 'no completed chart')}")
        hist = history_features.get(ticker)
        if hist:
            if hist.error and hist.bars < 20:
                lines.append(f"- history: unavailable; bars {hist.bars}; error {hist.error}")
            else:
                lines.append(
                    f"- history: sma5 {hist.sma5:.2f}; sma20 {hist.sma20:.2f}; trend20d {pct(hist.trend_20d_pct)}; "
                    f"vs_sma20 {pct(hist.close_vs_sma20_pct)}; vol20d {pct(hist.volatility_20d_pct)}; "
                    f"range60d {hist.range_position_60d:.2f}" if hist.sma5 is not None and hist.sma20 is not None else
                    f"- history: bars {hist.bars}; error {hist.error or 'insufficient history'}"
                )
        if source_verifications.get(ticker):
            verified_count = sum(1 for item in source_verifications[ticker] if item.get("verified"))
            lines.append(f"- source_verification: {verified_count}/{len(source_verifications[ticker])} verified")
        if headlines[ticker]:
            lines.append("- headlines:")
            lines.extend(f"  - {h}" for h in headlines[ticker][:3])
        if evidence:
            lines.append("- evidence:")
            lines.extend(f"  - {e}" for e in evidence)
        if blockers:
            lines.append("- blockers:")
            lines.extend(f"  - {b}" for b in blockers)
        if packet_hits.get(ticker):
            lines.append("- latest_packet_context:")
            lines.extend(f"  - {hit}" for hit in packet_hits[ticker][:2])
        lines.append("")

    job_suffix = f"-{re.sub(r'[^A-Za-z0-9_-]+', '-', job_id)[:24]}" if job_id else ""
    out = OUT_DIR / f"{now.strftime('%Y-%m-%dT%H%M')}-market-confirm-{window}{job_suffix}.md"
    out.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    sources_out = out.with_name(f"{out.stem}-sources.json")
    sources_out.write_text(
        json.dumps(
            {
                "schema": "openclaw.market_worker.source_verification.v1",
                "generated_at": now.isoformat(),
                "confirmation_report": out.name,
                "sources": source_verifications,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    if sells:
        summary = "SELL SIGNALS: " + "; ".join(f"{row[2]} score {row[1]}" for row in sells[:5])
        if buys:
            summary += " | BUY CANDIDATES: " + "; ".join(f"{row[2]} score {row[1]}" for row in buys[:3])
    elif buys:
        summary = "BUY CANDIDATES: " + "; ".join(f"{row[2]} score {row[1]}" for row in buys[:5])
    elif watches:
        summary = "NO BUY SIGNALS; WATCH: " + ", ".join(f"{row[2]} score {row[1]}" for row in watches[:5])
    else:
        summary = "NO BUY SIGNALS"
    if paper_sells or paper_buys or paper_cancel_buys:
        paper_summary = []
        if paper_sells:
            paper_summary.append("INTRADAY PAPER SELL: " + ", ".join(ticker for _, ticker, _ in paper_sells[:4]))
        if paper_buys:
            paper_summary.append("INTRADAY PAPER BUY: " + ", ".join(ticker for _, ticker, _ in paper_buys[:4]))
        if paper_cancel_buys:
            paper_summary.append("CANCEL BUY: " + ", ".join(ticker for _, ticker, _ in paper_cancel_buys[:4]))
        summary += " | " + " | ".join(paper_summary)
    summary += f" | expires {valid_until.strftime('%H:%M %Z')}"
    return out, summary


def read_recommendation_state() -> dict:
    if not RECOMMENDATION_STATE_FILE.exists():
        return {"schema": "openclaw.market_worker.recommendation_state.v1", "active": {}, "intraday_buy_origins": {}}
    try:
        data = json.loads(RECOMMENDATION_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema": "openclaw.market_worker.recommendation_state.v1", "active": {}}
    data.setdefault("schema", "openclaw.market_worker.recommendation_state.v1")
    data.setdefault("active", {})
    data.setdefault("intraday_buy_origins", {})
    return data


def intraday_buy_origins() -> dict[str, dict]:
    """Return symbols whose intraday BUY notification was delivered."""
    state = read_recommendation_state()
    origins = state.get("intraday_buy_origins")
    return origins if isinstance(origins, dict) else {}


def write_recommendation_state(state: dict) -> None:
    tmp = RECOMMENDATION_STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(RECOMMENDATION_STATE_FILE)


def parse_report_recommendations(report_path: Path) -> tuple[str | None, dict[str, dict]]:
    text = report_path.read_text(encoding="utf-8", errors="replace")
    valid_match = re.search(r"^valid_until:\s*(.+)$", text, flags=re.M)
    valid_until = valid_match.group(1).strip() if valid_match else None
    active: dict[str, dict] = {}
    decisions = re.search(r"(?ms)^## decisions\n(.*?)(?:\n## |\Z)", text)
    if not decisions:
        return valid_until, active
    for line in decisions.group(1).splitlines():
        match = re.match(r"-\s+(CANCEL BUY|INTRADAY PAPER BUY|INTRADAY PAPER SELL|PREDICTIVE BUY SIGNAL|AGGRESSIVE BUY SIGNAL|EARLY BUY SIGNAL|BUY SIGNAL|BUY CANDIDATE|WATCH|SELL SIGNAL):\s+([A-Z][A-Z0-9.]*)\b", line)
        if match:
            action = (
                "CANCEL_BUY" if match.group(1) == "CANCEL BUY"
                else "BUY" if match.group(1) in {"PREDICTIVE BUY SIGNAL", "AGGRESSIVE BUY SIGNAL", "EARLY BUY SIGNAL", "BUY CANDIDATE"}
                else "PAPER_BUY" if match.group(1) == "INTRADAY PAPER BUY"
                else "PAPER_SELL" if match.group(1) == "INTRADAY PAPER SELL"
                else match.group(1).replace(" SIGNAL", "")
            )
            ticker = match.group(2)
        else:
            old_match = re.match(r"-\s+buy\s+([A-Z][A-Z0-9.]*)\b", line)
            if not old_match:
                continue
            action = "BUY"
            ticker = old_match.group(1)
        active[ticker] = {"action": action, "line": line.strip(), "valid_until": valid_until}
    return valid_until, active


def notification_detail(report_path: Path, ticker: str, action: str, decision_line: str) -> str:
    text = report_path.read_text(encoding="utf-8", errors="replace")
    section_match = re.search(rf"(?ms)^### {re.escape(ticker)}\n(.*?)(?=^### |\Z)", text)
    section = section_match.group(1) if section_match else ""

    catalyst = ""
    sources_path = report_path.with_name(f"{report_path.stem}-sources.json")
    try:
        source_data = json.loads(sources_path.read_text(encoding="utf-8"))
        candidates = ((source_data.get("sources") or {}).get(ticker) or [])
        verified = next((item for item in candidates if item.get("verified")), None)
        if verified:
            catalyst = re.sub(r"\s+", " ", str(verified.get("snippet") or "")).strip()
    except (OSError, json.JSONDecodeError):
        pass

    evidence = []
    evidence_match = re.search(r"(?ms)^- evidence:\n(.*?)(?=^- [a-z_]+:|^### |\Z)", section)
    if evidence_match:
        for item in re.findall(r"^  - (.+)$", evidence_match.group(1), flags=re.M):
            if item not in {"verified catalyst source found", "headline language leans positive"}:
                evidence.append(item)

    if catalyst:
        windows = []
        for match in re.finditer(rf"\b{re.escape(ticker)}\b", catalyst, flags=re.IGNORECASE):
            window = catalyst[max(0, match.start() - 90) : min(len(catalyst), match.end() + 260)].strip()
            score = sum(1 for word in CATALYST_WORDS | POSITIVE_WORDS if word in window.lower())
            windows.append((score, window))
        catalyst = max(windows, default=(0, catalyst), key=lambda item: item[0])[1]
        table_like = any(
            phrase in catalyst
            for phrase in ("Net Assets Expense Ratio", "Expense Ratio YTD Return", "Market Weight Market Cap", "Day Return")
        )
        has_specific_catalyst = any(word in catalyst.lower() for word in CATALYST_WORDS | POSITIVE_WORDS)
        if table_like or not has_specific_catalyst:
            catalyst = "No distinct ticker-specific catalyst extracted; signal is driven by confirmed price and sector momentum."

    lines = [decision_line.removeprefix("- ")]
    if catalyst:
        lines.append(f"Catalyst/context: {catalyst[:260].rstrip()}")
    trade_plan_match = re.search(r"^- trade_plan:\s+(.+)$", section, flags=re.M)
    if action == "BUY" and trade_plan_match:
        lines.append(f"Trade plan: {trade_plan_match.group(1)}")
    if evidence:
        lines.append(f"Confirmation: {'; '.join(evidence[:3])}")
    return "\n".join(lines)


MAX_TELEGRAM_STATE_CHANGES = 6


def balanced_notification_changes(changes: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """Return all sell/cancel alerts and at most six BUY alerts.

    BUY recommendations are intentionally capped to keep the Telegram batch
    actionable. SELL, PAPER SELL, and CANCEL BUY alerts are position/risk
    notifications and must not be hidden by that BUY cap.
    """
    buys = [item for item in changes if item[1] in {"BUY", "PAPER_BUY"}]
    sells = [item for item in changes if item[1] in {"SELL", "PAPER_SELL"}]
    cancels = [item for item in changes if item[1] == "CANCEL_BUY"]
    other = [
        item for item in changes
        if item[1] not in {"BUY", "PAPER_BUY", "SELL", "PAPER_SELL", "CANCEL_BUY"}
    ]
    return sells + cancels + buys[:MAX_TELEGRAM_STATE_CHANGES] + other


def recommendation_state_change_details(report_path: Path, summary: str) -> tuple[str | None, str, list[tuple[str, str]]]:
    valid_until, active = parse_report_recommendations(report_path)
    active = {
        ticker: recommendation
        for ticker, recommendation in active.items()
        if recommendation.get("action") in {"BUY", "SELL", "PAPER_BUY", "PAPER_SELL", "CANCEL_BUY"}
    }
    state = read_recommendation_state()
    previous = state.setdefault("active", {})
    # A paper SELL is useful once as a directional observation even without a
    # position. Repeating it after the signal clears and returns is noise
    # unless the user holds the symbol and may need an exit alert.
    seen_unheld_paper_sells = state.setdefault("notified_unheld_paper_sells", {})
    if not isinstance(seen_unheld_paper_sells, dict):
        seen_unheld_paper_sells = {}
        state["notified_unheld_paper_sells"] = seen_unheld_paper_sells
    changes: list[tuple[str, str, str]] = []
    open_position_tickers = set(read_positions())

    for ticker, current in active.items():
        prior = previous.get(ticker)
        notified = bool(prior.get("notified", True)) if prior else False
        same_action = bool(prior and prior.get("action") == current["action"])
        should_notify = False
        if current["action"] == "PAPER_SELL" and ticker not in open_position_tickers:
            should_notify = ticker not in seen_unheld_paper_sells and (not same_action or not notified)
        elif current["action"] in {"SELL", "PAPER_SELL"}:
            should_notify = not same_action or not notified
        elif current["action"] in {"BUY", "PAPER_BUY"}:
            should_notify = not same_action or not notified
        elif current["action"] == "CANCEL_BUY":
            # A cancellation is only useful when it reverses a BUY alert that
            # was actually delivered. This prevents bearish signals from
            # producing standalone CANCEL BUY notifications for symbols the
            # user never received a BUY recommendation for (including BUYs
            # omitted by the six-item Telegram cap).
            prior_buy_was_sent = bool(
                prior
                and prior.get("action") in {"BUY", "PAPER_BUY"}
                and prior.get("notified", False)
            )
            should_notify = prior_buy_was_sent and (not same_action or not notified)
            if not prior_buy_was_sent:
                # Suppress this state transition once; a later BUY transition
                # can still notify normally because its action differs.
                notified = True
        elif not same_action:
            should_notify = True
        if should_notify:
            changes.append((ticker, current["action"], notification_detail(report_path, ticker, current["action"], current["line"])))
        previous[ticker] = {
            "action": current["action"],
            "first_seen_at": prior.get("first_seen_at") if same_action and prior else dt.datetime.now(TZ).isoformat(),
            "valid_until": current.get("valid_until"),
            "last_seen_at": dt.datetime.now(TZ).isoformat(),
            # Do not acknowledge an alert before Telegram actually accepts it.
            "notified": notified,
        }

    for ticker in sorted(list(previous)):
        if ticker in active:
            continue
        prior_action = previous[ticker].get("action")
        if prior_action in {"BUY", "SELL", "CANCEL_BUY"}:
            if not previous[ticker].get("notified", True):
                previous.pop(ticker, None)
                continue
            if prior_action == "SELL" and ticker in open_position_tickers:
                changes.append((ticker, f"CANCEL_{prior_action}", f"CANCEL {prior_action}: {ticker} no longer appears in latest recommendations"))
            elif prior_action in {"BUY", "CANCEL_BUY"} and ticker not in open_position_tickers:
                changes.append((ticker, f"CANCEL_{prior_action}", f"CANCEL {prior_action}: {ticker} no longer appears in latest recommendations"))
        previous.pop(ticker, None)

    state["last_report"] = report_path.name
    state["last_summary"] = summary
    state["last_valid_until"] = valid_until
    write_recommendation_state(state)

    if not changes:
        return None, "telegram notification skipped: no recommendation state changes", []
    selected = balanced_notification_changes(changes)
    headline = "\n\n".join(item[2] for item in selected)
    if valid_until:
        headline += f"\n\nExpires: {dt.datetime.fromisoformat(valid_until).strftime('%H:%M %Z')}"
    return headline, "recommendation state changed", [(ticker, action) for ticker, action, _ in selected]


def recommendation_state_changes(report_path: Path, summary: str) -> tuple[str | None, str]:
    """Compatibility wrapper for callers/tests that only need rendered text."""
    message, status, _ = recommendation_state_change_details(report_path, summary)
    return message, status


def acknowledge_recommendation_delivery(delivered: list[tuple[str, str]]) -> None:
    """Mark only successfully delivered active recommendations as notified."""
    if not delivered:
        return
    state = read_recommendation_state()
    active = state.setdefault("active", {})
    intraday_buy_origins = state.setdefault("intraday_buy_origins", {})
    if not isinstance(intraday_buy_origins, dict):
        intraday_buy_origins = {}
        state["intraday_buy_origins"] = intraday_buy_origins
    seen_unheld_paper_sells = state.setdefault("notified_unheld_paper_sells", {})
    if not isinstance(seen_unheld_paper_sells, dict):
        seen_unheld_paper_sells = {}
        state["notified_unheld_paper_sells"] = seen_unheld_paper_sells
    open_position_tickers = set(read_positions())
    for ticker, action in delivered:
        record = active.get(ticker)
        if record and record.get("action") == action:
            record["notified"] = True
            record["notified_at"] = dt.datetime.now(TZ).isoformat()
            if action == "PAPER_BUY":
                intraday_buy_origins[ticker] = {
                    "recommended_at": record.get("notified_at"),
                    "valid_until": record.get("valid_until"),
                    "source": "delivered_intraday_paper_buy",
                }
            if action == "PAPER_SELL" and ticker not in open_position_tickers:
                seen_unheld_paper_sells[ticker] = record["notified_at"]
    write_recommendation_state(state)


def notify_telegram(window: str, report_path: Path, summary: str) -> str:
    if not NOTIFY_TELEGRAM:
        return "telegram notification disabled"

    if not TELEGRAM_TARGET:
        return "telegram notification skipped: MARKET_CONFIRM_TELEGRAM_TARGET is empty"

    state_summary, state_status, delivered = recommendation_state_change_details(report_path, summary)
    if not state_summary:
        return state_status

    message = (
        f"Market confirmation ({CONFIRMATION_PROFILE}; {window})\n"
        f"{state_summary}\n"
        f"{report_path}"
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
        if not NOTIFY_DRY_RUN:
            acknowledge_recommendation_delivery(delivered)
        return "telegram notification ok" + (f": {output[:300]}" if output else "")
    return f"telegram notification failed ({result.returncode}): {output[:500]}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--window", required=True, choices=["opening", "one-hour", "midday", "late-day", "friday-closeout", "after-close"])
    parser.add_argument("--tickers", help="optional comma-separated tickers to confirm; defaults to full watchlist")
    parser.add_argument("--job-id", help="optional dynamic queue job id to include in the report")
    parser.add_argument("--job-thesis", help="optional queued signal/thesis text to include in the report")
    parser.add_argument("--source-packet", help="optional source packet path/name to include in the report")
    args = parser.parse_args()
    tickers = [part.strip().upper() for part in args.tickers.split(",") if part.strip()] if args.tickers else None
    out, summary = build_report(
        args.window,
        tickers=tickers,
        job_id=args.job_id,
        job_thesis=args.job_thesis,
        source_packet=args.source_packet,
    )
    print(f"{out}: {summary}")
    print(notify_telegram(args.window, out, summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
