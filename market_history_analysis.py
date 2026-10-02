#!/usr/bin/env python3
"""Fetch and analyze Schwab historical bars for market-worker signals."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import statistics
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

import market_schwab


ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get("MARKET_WORKER_DATA_DIR", str(ROOT.parent / "handoffs" / "market-worker")))
ROBINHOOD_CLIENT = Path(
    os.environ.get(
        "MARKET_HISTORY_ROBINHOOD_CLIENT",
        str(ROOT.parent / "skills" / "robinhood-agentic" / "rh-client.mjs"),
    )
)
CACHE_FILE = Path(os.environ.get("MARKET_HISTORY_CACHE", str(DATA_ROOT / "market-history-cache.json")))
HISTORY_TIMEOUT = int(os.environ.get("MARKET_HISTORY_TIMEOUT", "60"))
DEFAULT_DAYS = int(os.environ.get("MARKET_HISTORY_DAYS", "120"))
MAX_AGE_HOURS = float(os.environ.get("MARKET_HISTORY_MAX_AGE_HOURS", "6"))
ROBINHOOD_ENABLED = False
SCHWAB_ENABLED = os.environ.get("MARKET_HISTORY_SCHWAB", "1").lower() in {"1", "true", "yes"}


@dataclass
class HistoryFeature:
    symbol: str
    latest_close: float | None = None
    sma5: float | None = None
    sma20: float | None = None
    close_vs_sma20_pct: float | None = None
    trend_20d_pct: float | None = None
    volatility_20d_pct: float | None = None
    range_position_60d: float | None = None
    bars: int = 0
    error: str | None = None


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_timestamp(value: object) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def numeric(value: object) -> float | None:
    try:
        parsed = float(str(value).replace("$", "").replace(",", ""))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def read_cache() -> dict:
    if not CACHE_FILE.exists():
        return {"schema": "openclaw.market_history_cache.v1", "symbols": {}}
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema": "openclaw.market_history_cache.v1", "symbols": {}}
    data.setdefault("schema", "openclaw.market_history_cache.v1")
    data.setdefault("symbols", {})
    return data


def write_cache(cache: dict) -> None:
    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(CACHE_FILE)


def stale(entry: dict, max_age_hours: float) -> bool:
    fetched_at = parse_timestamp(entry.get("fetched_at"))
    if fetched_at is None:
        return True
    return now_utc() - fetched_at > dt.timedelta(hours=max_age_hours)


def robinhood_historicals(symbols: list[str], days: int = DEFAULT_DAYS, interval: str = "day") -> dict[str, list[dict]]:
    if not ROBINHOOD_ENABLED:
        raise RuntimeError("Robinhood historicals disabled")
    if not ROBINHOOD_CLIENT.exists():
        raise RuntimeError(f"Robinhood client not found: {ROBINHOOD_CLIENT}")
    end = now_utc()
    start = end - dt.timedelta(days=days)
    args = {
        "symbols": symbols,
        "start_time": start.isoformat().replace("+00:00", "Z"),
        "end_time": end.isoformat().replace("+00:00", "Z"),
        "interval": interval,
        "bounds": "regular",
        "adjustment_type": "split",
    }
    completed = subprocess.run(
        ["node", str(ROBINHOOD_CLIENT), "call", "get_equity_historicals", json.dumps(args)],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=HISTORY_TIMEOUT,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
        raise RuntimeError(f"get_equity_historicals failed: {detail[:500]}")
    payload = json.loads(completed.stdout)
    out: dict[str, list[dict]] = {}
    for result in ((payload.get("data") or {}).get("results") or []):
        symbol = str(result.get("symbol") or "").upper()
        bars = result.get("bars") or []
        out[symbol] = bars if isinstance(bars, list) else []
    return out


def schwab_historicals(symbols: list[str], days: int = DEFAULT_DAYS) -> dict[str, list[dict]]:
    if not SCHWAB_ENABLED:
        raise RuntimeError("Schwab historicals disabled")
    config = market_schwab.load_env()
    out: dict[str, list[dict]] = {}
    for symbol in symbols:
        data = market_schwab.fetch_price_history(config, symbol, days=days)
        candles = data.get("candles") if isinstance(data, dict) else None
        bars: list[dict] = []
        for candle in candles if isinstance(candles, list) else []:
            close = numeric(candle.get("close")) if isinstance(candle, dict) else None
            timestamp_ms = candle.get("datetime") if isinstance(candle, dict) else None
            if close is None:
                continue
            bars.append(
                {
                    "begins_at": (
                        dt.datetime.fromtimestamp(timestamp_ms / 1000, dt.timezone.utc).isoformat()
                        if isinstance(timestamp_ms, (int, float))
                        else None
                    ),
                    "open_price": candle.get("open"),
                    "high_price": candle.get("high"),
                    "low_price": candle.get("low"),
                    "close_price": close,
                    "volume": candle.get("volume"),
                    "interpolated": False,
                    "source": "schwab",
                }
            )
        out[symbol.upper()] = bars
    return out


def fetch_historicals(symbols: list[str], days: int = DEFAULT_DAYS) -> dict[str, list[dict]]:
    errors = []
    if SCHWAB_ENABLED:
        try:
            return schwab_historicals(symbols, days=days)
        except Exception as exc:  # noqa: BLE001 - surface the Schwab failure.
            errors.append(f"Schwab historicals failed: {type(exc).__name__}: {exc}")
    raise RuntimeError("; ".join(errors) or "no historical market-data provider enabled")


def refresh_symbols(symbols: list[str], days: int = DEFAULT_DAYS, max_age_hours: float = MAX_AGE_HOURS) -> dict:
    normalized = sorted({symbol.upper().strip() for symbol in symbols if symbol.strip()})
    cache = read_cache()
    entries = cache.setdefault("symbols", {})
    missing = [
        symbol
        for symbol in normalized
        if symbol not in entries
        or stale(entries[symbol], max_age_hours)
        or entries[symbol].get("error")
        or not entries[symbol].get("bars")
    ]
    for start in range(0, len(missing), 10):
        chunk = missing[start : start + 10]
        if not chunk:
            continue
        fetched_at = now_utc().isoformat().replace("+00:00", "Z")
        try:
            fetched = fetch_historicals(chunk, days=days)
        except Exception as exc:  # noqa: BLE001 - keep failures per symbol.
            for symbol in chunk:
                prior = entries.get(symbol, {})
                entries[symbol] = {**prior, "fetched_at": fetched_at, "error": f"{type(exc).__name__}: {exc}"}
            continue
        for symbol in chunk:
            entries[symbol] = {
                "fetched_at": fetched_at,
                "interval": "day",
                "bounds": "regular",
                "adjustment_type": "split",
                "bars": fetched.get(symbol, []),
                "error": None if fetched.get(symbol) else "no bars returned",
            }
    cache["updated_at"] = now_utc().isoformat().replace("+00:00", "Z")
    write_cache(cache)
    return cache


def closes_from_bars(bars: list[dict]) -> list[float]:
    closes = []
    for bar in bars:
        if bar.get("interpolated"):
            continue
        close = numeric(bar.get("close_price"))
        if close is not None and close > 0:
            closes.append(close)
    return closes


def feature_from_bars(symbol: str, bars: list[dict], error: str | None = None) -> HistoryFeature:
    closes = closes_from_bars(bars)
    if error and not closes:
        return HistoryFeature(symbol=symbol, error=error)
    if len(closes) < 20:
        return HistoryFeature(symbol=symbol, latest_close=closes[-1] if closes else None, bars=len(closes), error="fewer than 20 historical bars")
    latest = closes[-1]
    sma5 = statistics.mean(closes[-5:])
    sma20 = statistics.mean(closes[-20:])
    returns = [
        (closes[index] - closes[index - 1]) / closes[index - 1] * 100
        for index in range(max(1, len(closes) - 20), len(closes))
        if closes[index - 1] > 0
    ]
    window = closes[-60:] if len(closes) >= 60 else closes
    low = min(window)
    high = max(window)
    return HistoryFeature(
        symbol=symbol,
        latest_close=latest,
        sma5=sma5,
        sma20=sma20,
        close_vs_sma20_pct=(latest - sma20) / sma20 * 100 if sma20 else None,
        trend_20d_pct=(latest - closes[-20]) / closes[-20] * 100 if closes[-20] else None,
        volatility_20d_pct=statistics.stdev(returns) if len(returns) >= 2 else None,
        range_position_60d=(latest - low) / (high - low) if high > low else None,
        bars=len(closes),
        error=error,
    )


def features_for_symbols(symbols: list[str], days: int = DEFAULT_DAYS, max_age_hours: float = MAX_AGE_HOURS) -> dict[str, HistoryFeature]:
    cache = refresh_symbols(symbols, days=days, max_age_hours=max_age_hours)
    entries = cache.get("symbols") or {}
    out: dict[str, HistoryFeature] = {}
    for symbol in sorted({symbol.upper().strip() for symbol in symbols if symbol.strip()}):
        entry = entries.get(symbol, {})
        bars = entry.get("bars") if isinstance(entry, dict) else []
        out[symbol] = feature_from_bars(symbol, bars if isinstance(bars, list) else [], entry.get("error") if isinstance(entry, dict) else "missing cache entry")
    return out


def feature_summary(feature: HistoryFeature) -> str:
    def pct(value: float | None) -> str:
        return "n/a" if value is None else f"{value:+.2f}%"

    def ratio(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.2f}"

    return (
        f"{feature.symbol}: close {feature.latest_close if feature.latest_close is not None else 'n/a'}; "
        f"sma5 {feature.sma5:.2f}" if feature.sma5 is not None else f"{feature.symbol}: insufficient history"
    ) + (
        f"; sma20 {feature.sma20:.2f}; vs_sma20 {pct(feature.close_vs_sma20_pct)}; "
        f"trend20d {pct(feature.trend_20d_pct)}; vol20d {pct(feature.volatility_20d_pct)}; "
        f"range60d {ratio(feature.range_position_60d)}"
        if feature.sma20 is not None
        else f"; error {feature.error or 'n/a'}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    refresh = sub.add_parser("refresh")
    refresh.add_argument("--symbols", required=True)
    refresh.add_argument("--days", type=int, default=DEFAULT_DAYS)
    features = sub.add_parser("features")
    features.add_argument("--symbols", required=True)
    features.add_argument("--days", type=int, default=DEFAULT_DAYS)
    args = parser.parse_args()
    symbols = [part.strip().upper() for part in args.symbols.split(",") if part.strip()]
    if args.command == "refresh":
        cache = refresh_symbols(symbols, days=args.days, max_age_hours=0)
        print(json.dumps({"updated_at": cache.get("updated_at"), "symbols": symbols}, indent=2))
    elif args.command == "features":
        data = features_for_symbols(symbols, days=args.days)
        print(json.dumps({symbol: asdict(feature) for symbol, feature in data.items()}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
