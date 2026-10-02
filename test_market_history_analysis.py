import unittest
from unittest.mock import patch

import market_history_analysis as history


def bar(close: float) -> dict:
    return {
        "open_price": str(close),
        "close_price": str(close),
        "high_price": str(close),
        "low_price": str(close),
        "volume": 1000,
        "session": "reg",
    }


class MarketHistoryAnalysisTest(unittest.TestCase):
    def test_feature_from_bars_computes_trend_and_extension(self) -> None:
        bars = [bar(float(value)) for value in range(100, 130)]
        feature = history.feature_from_bars("TEST", bars)
        self.assertEqual(feature.bars, 30)
        self.assertAlmostEqual(feature.latest_close or 0, 129.0)
        self.assertGreater(feature.sma5 or 0, feature.sma20 or 0)
        self.assertGreater(feature.trend_20d_pct or 0, 0)
        self.assertEqual(feature.range_position_60d, 1.0)

    def test_feature_from_bars_reports_insufficient_history(self) -> None:
        feature = history.feature_from_bars("TEST", [bar(10.0), bar(11.0)])
        self.assertEqual(feature.bars, 2)
        self.assertIn("fewer than 20", feature.error or "")

    def test_schwab_historicals_convert_daily_candles(self) -> None:
        payload = {
            "candles": [
                {"datetime": 1788048000000, "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5, "volume": 1234}
            ]
        }
        with (
            patch.object(history, "SCHWAB_ENABLED", True),
            patch.object(history.market_schwab, "load_env", return_value={}),
            patch.object(history.market_schwab, "fetch_price_history", return_value=payload),
        ):
            rows = history.schwab_historicals(["TEST"])

        self.assertEqual(rows["TEST"][0]["close_price"], 10.5)
        self.assertEqual(rows["TEST"][0]["volume"], 1234)
        self.assertEqual(rows["TEST"][0]["source"], "schwab")


if __name__ == "__main__":
    unittest.main()
