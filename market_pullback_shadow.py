#!/usr/bin/env python3
"""Write a non-notifying paper-shadow report for pullbacks in supported trends.

The report is deliberately advisory: it reads the already cached daily bars,
does not call a broker, does not place orders, and does not send Telegram.
"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get("MARKET_WORKER_DATA_DIR", str(ROOT.parent / "handoffs" / "market-worker")))
CACHE = DATA_ROOT / "market-history-cache.json"
OUT_DIR = Path(__import__("os").environ.get("MARKET_PULLBACK_SHADOW_OUT_DIR", str(DATA_ROOT / "pullback-shadow")))
TZ = ZoneInfo("America/Chicago")
MIN_AVG_VOLUME = int(__import__("os").environ.get("MARKET_PULLBACK_SHADOW_MIN_AVG_VOLUME", "500000"))


def mean(values: list[float]) -> float:
    return statistics.fmean(values)


def cleaned_bars(payload: dict) -> list[dict]:
    bars = [
        bar
        for bar in payload.get("bars", [])
        if bar.get("begins_at") and bar.get("open_price") and bar.get("close_price") and bar.get("volume")
    ]
    return sorted(bars, key=lambda bar: bar["begins_at"])


def signal_for(ticker: str, bars: list[dict], spy_by_date: dict[str, dict]) -> dict | None:
    if ticker == "SPY" or len(bars) < 50:
        return None
    bar = bars[-1]
    date = bar["begins_at"][:10]
    spy = spy_by_date.get(date)
    if not spy:
        return None
    closes = [float(item["close_price"]) for item in bars]
    volumes = [float(item["volume"]) for item in bars[-10:]]
    spy_closes = [float(item["close_price"]) for item in spy_by_date.values() if item["begins_at"][:10] <= date]
    if len(spy_closes) < 50:
        return None
    price = closes[-1]
    sma20, sma50 = mean(closes[-20:]), mean(closes[-50:])
    spy20, spy50 = mean(spy_closes[-20:]), mean(spy_closes[-50:])
    relative20 = price / closes[-21] - spy_closes[-1] / spy_closes[-21]
    pullback3 = price / closes[-4] - 1
    avg_volume = mean(volumes)
    if not (
        avg_volume >= MIN_AVG_VOLUME
        and price > sma20 > sma50
        and spy_closes[-1] > spy20 > spy50
        and relative20 >= 0.02
        and -0.06 <= pullback3 <= -0.01
        and price >= 0.97 * sma20
    ):
        return None
    return {
        "ticker": ticker,
        "signal_date": date,
        "close": price,
        "sma20": sma20,
        "sma50": sma50,
        "pullback3_pct": pullback3 * 100,
        "relative20_pct": relative20 * 100,
        "average_volume": avg_volume,
    }


def main() -> int:
    if not CACHE.exists():
        print(f"pullback shadow skipped: history cache not found: {CACHE}")
        return 0
    try:
        payload = json.loads(CACHE.read_text())
        series = {ticker: cleaned_bars(item) for ticker, item in payload.get("symbols", {}).items()}
        spy_bars = series.get("SPY", [])
    except (OSError, json.JSONDecodeError) as exc:
        print(f"pullback shadow skipped: could not read history cache: {exc}")
        return 0
    if len(spy_bars) < 50:
        print("pullback shadow skipped: SPY history is incomplete")
        return 0

    spy_by_date = {bar["begins_at"][:10]: bar for bar in spy_bars}
    candidates = [candidate for ticker, bars in series.items() if (candidate := signal_for(ticker, bars, spy_by_date))]
    # Do not let a duplicated cache symbol dominate the report.
    unique = {candidate["ticker"]: candidate for candidate in candidates}
    candidates = sorted(unique.values(), key=lambda item: (-item["relative20_pct"], item["pullback3_pct"], item["ticker"]))
    now = datetime.now(TZ)
    latest_date = spy_bars[-1]["begins_at"][:10]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report = OUT_DIR / f"{now:%Y-%m-%d}-pullback-shadow.md"
    lines = [
        "source: market-pullback-shadow",
        "status: paper-shadow",
        f"generated_at: {now.isoformat(timespec='seconds')}",
        f"latest_completed_daily_bar: {latest_date}",
        "delivery: none (no Telegram, no broker calls, no orders)",
        "",
        "## method",
        f"- 10-day average volume >= {MIN_AVG_VOLUME:,} shares",
        "- stock close > SMA20 > SMA50 and SPY close > SMA20 > SMA50",
        "- stock 20-day return exceeds SPY by at least 2 percentage points",
        "- stock is down 1–6% across three sessions but remains within 3% of SMA20",
        "- paper entry: next regular-session open; record outcomes after 3 and 5 sessions before considering any promotion",
        "",
        "## candidates",
    ]
    if candidates:
        for item in candidates:
            lines.append(
                f"- {item['ticker']}: close ${item['close']:.2f}; 3-day pullback {item['pullback3_pct']:.2f}%; "
                f"20-day excess vs SPY {item['relative20_pct']:.2f}%; avg volume {item['average_volume']:,.0f}"
            )
    else:
        lines.append("- No qualifying paper-shadow candidates from the cached completed daily bars.")
    report.write_text("\n".join(lines) + "\n")
    print(f"wrote pullback paper-shadow report: {report}; candidates={len(candidates)}; no notifications sent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
