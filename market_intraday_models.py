"""Deterministic five-minute intraday paper-signal models.

Signals are research only. They use completed regular-session bars and never
submit orders. Each bullish setup has a matched bearish exit/reversal signal.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


def _number(row: dict, key: str) -> float | None:
    try:
        value = float(row[key])
    except (KeyError, TypeError, ValueError):
        return None
    return value if value >= 0 else None


def normalize_candles(candles: list[dict]) -> list[Bar]:
    """Convert Schwab candles, retaining only valid regular-session bars."""
    bars: list[Bar] = []
    for candle in candles:
        try:
            timestamp = datetime.fromtimestamp(float(candle["datetime"]) / 1000, tz=ET)
        except (KeyError, TypeError, ValueError, OSError):
            continue
        values = [_number(candle, key) for key in ("open", "high", "low", "close", "volume")]
        if any(value is None for value in values):
            continue
        open_, high, low, close, volume = values
        if not (open_ > 0 and high >= low > 0 and high >= open_ and high >= close and low <= open_ and low <= close):
            continue
        if timestamp.weekday() < 5 and (timestamp.hour, timestamp.minute) >= (9, 30) and (timestamp.hour, timestamp.minute) < (16, 0):
            bars.append(Bar(timestamp, open_, high, low, close, volume))
    return sorted(bars, key=lambda bar: bar.timestamp)


def ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    multiplier = 2 / (period + 1)
    result = sum(values[:period]) / period
    for value in values[period:]:
        result = (value - result) * multiplier + result
    return result


def vwap(bars: list[Bar]) -> float | None:
    volume = sum(bar.volume for bar in bars)
    if volume <= 0:
        return None
    return sum(((bar.high + bar.low + bar.close) / 3) * bar.volume for bar in bars) / volume


def analyze(symbol: str, candles: list[dict], now: datetime | None = None) -> dict:
    """Return matched bullish and bearish paper recommendations for one symbol."""
    now_et = (now or datetime.now(ET)).astimezone(ET)
    bars = normalize_candles(candles)
    today = [bar for bar in bars if bar.timestamp.date() == now_et.date()]
    result = {"symbol": symbol.upper(), "status": "unavailable", "signals": [], "metrics": {}}
    if len(today) < 10:  # six bars for the opening range plus confirmation.
        result["reason"] = "fewer than 10 completed regular-session five-minute bars"
        return result
    opening = today[:6]
    current, previous = today[-1], today[-2]
    opening_high, opening_low = max(bar.high for bar in opening), min(bar.low for bar in opening)
    current_vwap = vwap(today)
    closes = [bar.close for bar in today]
    ema9, ema20 = ema(closes, 9), ema(closes, 20)
    average_bar_volume = sum(bar.volume for bar in today[:-1]) / max(1, len(today) - 1)
    volume_confirmed = current.volume >= average_bar_volume * 0.80
    result["status"] = "ok"
    result["metrics"] = {"last_close": round(current.close, 4), "opening_high": round(opening_high, 4), "opening_low": round(opening_low, 4), "vwap": round(current_vwap, 4) if current_vwap else None, "ema9": round(ema9, 4) if ema9 else None, "ema20": round(ema20, 4) if ema20 else None, "volume_confirmed": volume_confirmed}
    signals: list[dict[str, str]] = []
    if current.close > opening_high and volume_confirmed:
        signals.append({"side": "BUY", "model": "Opening-range breakout", "reason": "close above first-30-minute high with volume confirmation"})
    if current_vwap is not None and previous.close <= current_vwap and current.close > current_vwap and current.close > previous.close and volume_confirmed:
        signals.append({"side": "BUY", "model": "VWAP reclaim", "reason": "reclaimed session VWAP on improving completed bar"})
    if current_vwap is not None and ema9 is not None and ema20 is not None and ema9 > ema20 and current.close > current_vwap and min(bar.low for bar in today[-3:]) <= ema9 <= current.close:
        signals.append({"side": "BUY", "model": "Trend-pullback continuation", "reason": "held VWAP with 9/20 EMA uptrend after a controlled pullback"})
    if current.close < opening_low and volume_confirmed:
        signals.append({"side": "SELL", "model": "Opening-range breakdown", "reason": "close below first-30-minute low with volume confirmation"})
    if current_vwap is not None and previous.close >= current_vwap and current.close < current_vwap and current.close < previous.close and volume_confirmed:
        signals.append({"side": "SELL", "model": "VWAP rejection", "reason": "rejected session VWAP on a weakening completed bar"})
    if current_vwap is not None and ema9 is not None and ema20 is not None and ema9 < ema20 and current.close < current_vwap and max(bar.high for bar in today[-3:]) >= ema9 >= current.close:
        signals.append({"side": "SELL", "model": "Trend-pullback breakdown", "reason": "failed VWAP with 9/20 EMA downtrend after a failed bounce"})
    buy_signal_count = sum(1 for signal in signals if signal["side"] == "BUY")
    sell_signal_count = sum(1 for signal in signals if signal["side"] == "SELL")
    # This is a deterministic signal-strength rank, not a profitability forecast.
    # Independent model agreement is the primary score; confirmed participation
    # is a small tie-breaker so a volume-confirmed setup ranks first.
    result["scores"] = {
        "BUY": buy_signal_count * 10 + (1 if volume_confirmed and buy_signal_count else 0),
        "SELL": sell_signal_count * 10 + (1 if volume_confirmed and sell_signal_count else 0),
    }
    result["signals"] = signals
    return result
