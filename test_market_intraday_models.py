import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import market_intraday_models as models
import market_signal_confirm as confirm


def candle(at: datetime, open_: float, high: float, low: float, close: float, volume: float = 1000) -> dict:
    return {"datetime": int(at.timestamp() * 1000), "open": open_, "high": high, "low": low, "close": close, "volume": volume}


class IntradayModelsTest(unittest.TestCase):
    def test_opening_range_breakout_is_a_paper_buy(self) -> None:
        start = datetime(2026, 9, 30, 9, 30, tzinfo=models.ET)
        rows = [candle(start + timedelta(minutes=5 * index), 100, 100.5, 99.5, 100.0) for index in range(6)]
        rows.extend([
            candle(start + timedelta(minutes=30), 100.0, 100.4, 99.9, 100.2),
            candle(start + timedelta(minutes=35), 100.2, 100.5, 100.1, 100.4),
            candle(start + timedelta(minutes=40), 100.4, 100.6, 100.3, 100.5),
            candle(start + timedelta(minutes=45), 100.5, 101.2, 100.5, 101.0, 1200),
        ])
        result = models.analyze("TEST", rows, now=start + timedelta(hours=1))
        self.assertEqual(result["status"], "ok")
        self.assertIn("Opening-range breakout", [signal["model"] for signal in result["signals"] if signal["side"] == "BUY"])

    def test_protection_plan_uses_recent_swing_low_and_atr(self) -> None:
        start = datetime(2026, 9, 30, 9, 30, tzinfo=models.ET)
        rows = [
            candle(start + timedelta(minutes=5 * index), 100 + index * 0.2, 100.8 + index * 0.2, 99.8 + index * 0.2, 100.4 + index * 0.2)
            for index in range(6)
        ]
        plan = models.protection_plan("TEST", rows, now=start + timedelta(minutes=30))
        self.assertEqual(plan["status"], "ok")
        self.assertLess(plan["stop"], plan["price"])
        self.assertIn("swing low", plan["reason"])

    def test_buy_analysis_includes_post_fill_trailing_stop_recommendation(self) -> None:
        start = datetime(2026, 9, 30, 9, 30, tzinfo=models.ET)
        rows = [
            candle(start + timedelta(minutes=5 * index), 100 + index * 0.2, 100.8 + index * 0.2, 99.8 + index * 0.2, 100.4 + index * 0.2)
            for index in range(10)
        ]
        result = models.analyze("TEST", rows, now=start + timedelta(minutes=50))
        self.assertEqual(result["protection"]["status"], "ok")
        self.assertGreater(result["protection"]["trailing_pct"], 0)
        self.assertGreaterEqual(
            result["protection"]["trailing_pct"],
            result["protection"]["minimum_trailing_pct"],
        )
        self.assertIn("minimum 0.5 ATR", result["protection"]["reason"])

    def test_opening_range_breakdown_is_a_paper_sell(self) -> None:
        start = datetime(2026, 9, 30, 9, 30, tzinfo=models.ET)
        rows = [candle(start + timedelta(minutes=5 * index), 100, 100.5, 99.5, 100.0) for index in range(6)]
        rows.extend([
            candle(start + timedelta(minutes=30), 100.0, 100.1, 99.7, 99.8),
            candle(start + timedelta(minutes=35), 99.8, 99.9, 99.6, 99.7),
            candle(start + timedelta(minutes=40), 99.7, 99.8, 99.5, 99.6),
            candle(start + timedelta(minutes=45), 99.6, 99.6, 98.8, 99.0, 1200),
        ])
        result = models.analyze("TEST", rows, now=start + timedelta(hours=1))
        self.assertIn("Opening-range breakdown", [signal["model"] for signal in result["signals"] if signal["side"] == "SELL"])

    def test_signal_strength_scores_and_rank_are_not_alphabetical(self) -> None:
        paper = {
            "ZETA": {"signals": [{"side": "BUY", "model": "one"}], "scores": {"BUY": 10}},
            "ALPHA": {"signals": [{"side": "BUY", "model": "one"}, {"side": "BUY", "model": "two"}], "scores": {"BUY": 21}},
        }
        buys, sells, cancels = confirm.rank_intraday_paper_alerts(paper)
        self.assertEqual([ticker for _score, ticker, _models in buys], ["ALPHA", "ZETA"])
        self.assertEqual(sells, [])
        self.assertEqual(cancels, [])

    def test_report_parser_keeps_paper_buy_and_sell_distinct(self) -> None:
        with TemporaryDirectory() as directory:
            report = Path(directory) / "report.md"
            report.write_text(
                "valid_until: 2026-09-30T14:40:00-05:00\n\n## decisions\n"
                "- INTRADAY PAPER BUY: SPY - models: VWAP reclaim; research alert only\n"
                "- INTRADAY PAPER SELL: QQQ - models: VWAP rejection; research alert only\n",
                encoding="utf-8",
            )
            _, active = confirm.parse_report_recommendations(report)
        self.assertEqual(active["SPY"]["action"], "PAPER_BUY")
        self.assertEqual(active["QQQ"]["action"], "PAPER_SELL")


if __name__ == "__main__":
    unittest.main()
