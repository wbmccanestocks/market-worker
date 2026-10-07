import datetime as dt
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import market_history_analysis as history
import market_signal_confirm as confirm


def quote(ticker: str, day_change: float) -> confirm.Quote:
    return confirm.Quote(ticker, 101.0, 100.0, day_change, 100.0, 100_000, 1_000_000, 0.1, None)


class MarketSignalConfirmTest(unittest.TestCase):
    def test_requested_single_stock_and_bitcoin_leveraged_pairs_are_active(self) -> None:
        self.assertTrue({"TSLL", "NVDL", "AMZU", "BITX"} <= confirm.BULL_LEVERAGED_ETFS)
        self.assertTrue({"TSLS", "TSLZ", "NVD", "AMZD", "SBIT"} <= confirm.INVERSE_LEVERAGED_ETFS)
        self.assertEqual(confirm.TICKER_SECTOR["TSLS"], "tech")
        self.assertEqual(confirm.TICKER_SECTOR["TSLZ"], "tech")
        self.assertEqual(confirm.TICKER_SECTOR["NVD"], "tech")
        self.assertEqual(confirm.TICKER_SECTOR["AMZD"], "consumer")
        self.assertEqual(confirm.TICKER_SECTOR["SBIT"], "crypto")
        self.assertEqual(confirm.CONTEXT["crypto"], ["IBIT", "BITO"])

    def test_broad_market_pages_are_not_ticker_specific_sources(self) -> None:
        self.assertTrue(confirm.generic_source_url("https://www.marketwatch.com/tools/earnings-calendar"))
        self.assertTrue(confirm.generic_source_url("https://finance.yahoo.com/sectors/technology/"))
        self.assertTrue(confirm.generic_source_url("https://stockstotrade.com/stocks-to-watch/"))
        self.assertFalse(confirm.generic_source_url("https://example.com/news/aapl-raises-guidance"))

    def test_schwab_quote_payload_maps_to_signal_quote(self) -> None:
        payload = {
            "AMD": {
                "quote": {
                    "lastPrice": 469.06,
                    "closePrice": 465.58,
                    "netPercentChange": 0.74745479,
                    "openPrice": 466.22,
                    "totalVolume": 4_231_156,
                    "tradeTime": 1_788_187_547_566,
                },
                "regular": {
                    "regularMarketLastPrice": 469.06,
                    "regularMarketPercentChange": 0.74745479,
                    "regularMarketTradeTime": 1_788_187_547_566,
                },
                "fundamental": {"avg10DaysVolume": 17_427_782.0},
            }
        }
        with (
            patch.object(confirm, "SCHWAB_QUOTES_ENABLED", True),
            patch.object(confirm.market_schwab, "load_env", return_value={}),
            patch.object(confirm.market_schwab, "fetch_quotes", return_value=payload),
        ):
            quotes = confirm.fetch_schwab_quotes(["AMD"])

        self.assertEqual(quotes["AMD"].price, 469.06)
        self.assertEqual(quotes["AMD"].prev_close, 465.58)
        self.assertAlmostEqual(quotes["AMD"].day_change_pct or 0, 0.74745479)
        self.assertEqual(quotes["AMD"].volume, 4_231_156)
        self.assertAlmostEqual(quotes["AMD"].volume_ratio or 0, 4_231_156 / 17_427_782.0)

    def test_average_share_volume_blocks_thin_buy_candidate(self) -> None:
        thin = confirm.Quote("CASH", 80.0, 79.0, 1.0, 79.5, 50_000, 240_000, 0.2, None)
        blockers = confirm.average_liquidity_blockers(thin)
        self.assertTrue(any("average share volume too low" in blocker for blocker in blockers))
        self.assertFalse(confirm.average_liquidity_blockers(thin, explicit_watchlist_name=True))

    def test_early_setup_requires_unextended_relative_strength_and_acceleration(self) -> None:
        quotes = {
            "AAPL": quote("AAPL", 1.1),
            "QQQ": quote("QQQ", 0.3),
            "XLK": quote("XLK", 0.3),
            "SMH": quote("SMH", 0.3),
        }
        intraday = confirm.Intraday("AAPL", 101.0, 100.0, 0.2, 0.4, 1.0)
        score, evidence, blockers = confirm.score_early_setup(
            "AAPL",
            quotes["AAPL"],
            quotes,
            {"AAPL": ["AAPL raises guidance after strong growth"]},
            [],
            {"AAPL": [{"verified": True}]},
            intraday,
        )
        self.assertGreaterEqual(score, 6)
        self.assertFalse(blockers)
        self.assertTrue(any("relative strength" in item for item in evidence))

    def test_extended_move_cannot_be_a_confirmed_buy(self) -> None:
        evidence = [
            "verified catalyst source found",
            "price confirms: +10.00% today",
            "tech context confirms: +1.00% avg context ETF move",
        ]
        blockers = ["price already extended: +10.00% exceeds 3.0% anti-chase limit"]
        action, *_ = confirm.classify_decision(
            "AAPL",
            7,
            evidence,
            blockers,
            "midday",
            {},
            quote("AAPL", 10.0),
            {},
            {},
            [],
            (0, [], []),
        )
        self.assertEqual(action, "no_buy")

    def test_weak_history_cannot_be_a_confirmed_buy(self) -> None:
        evidence = [
            "verified catalyst source found",
            "price confirms: +1.50% today",
            "tech context confirms: +1.00% avg context ETF move",
        ]
        blockers = ["history trend weak: 5-day avg 95.00 <= 20-day avg 100.00"]
        action, *_ = confirm.classify_decision(
            "AAPL",
            6,
            evidence,
            blockers,
            "midday",
            {},
            quote("AAPL", 1.5),
            {},
            {},
            [],
            (0, [], []),
        )
        self.assertEqual(action, "no_buy")

    def test_aggressive_setup_requires_momentum_and_participation(self) -> None:
        quotes = {
            "TSLA": quote("TSLA", 3.2),
            "QQQ": quote("QQQ", 0.8),
            "XLK": quote("XLK", 0.7),
            "SMH": quote("SMH", 0.9),
        }
        quotes["TSLA"].volume_ratio = 1.2
        intraday = confirm.Intraday("TSLA", 101.0, 100.0, 0.1, 0.4, 3.0)
        score, evidence, blockers = confirm.score_aggressive_setup(
            "TSLA",
            quotes["TSLA"],
            quotes,
            {"TSLA": ["TSLA jumps after analyst upgrade and growth outlook"]},
            [],
            {"TSLA": [{"verified": True}]},
            intraday,
            explicit_watchlist_name=True,
        )
        self.assertGreaterEqual(score, 7)
        self.assertFalse(blockers)
        self.assertTrue(any("already moving" in item for item in evidence))

    def test_pullback_reclaim_is_watch_only_without_evidence_led_model(self) -> None:
        quotes = {
            "AAPL": quote("AAPL", 0.8),
            "QQQ": quote("QQQ", 0.2),
            "XLK": quote("XLK", 0.1),
            "SMH": quote("SMH", 0.2),
        }
        quotes["AAPL"].volume = 600_000
        feature = history.HistoryFeature(
            symbol="AAPL", latest_close=100.0, sma5=101.0, sma20=99.0,
            close_vs_sma20_pct=1.0, trend_20d_pct=3.0, volatility_20d_pct=2.0,
            range_position_60d=0.6, bars=60,
        )
        setup = confirm.score_pullback_reclaim_setup(
            "AAPL", quotes["AAPL"], quotes,
            {"AAPL": ["AAPL raises guidance after record demand"]}, [],
            {"AAPL": [{"verified": True}]},
            confirm.Intraday("AAPL", 101.0, 100.0, 0.2, 0.1, 0.8), feature,
        )
        action, _score, decision, _evidence, blockers = confirm.classify_decision(
            "AAPL", 0, [], [], "midday", {}, quotes["AAPL"], quotes, {}, [], setup,
        )
        self.assertEqual(action, "watch")
        self.assertIn("Pullback/reclaim setup is incomplete", decision)
        self.assertFalse(blockers)

    def test_catalyst_trend_continuation_qualifies_and_is_labeled(self) -> None:
        quotes = {symbol: quote(symbol, 0.2) for symbol in ("QQQ", "XLK", "SMH")}
        quotes["AAPL"] = quote("AAPL", 1.2)
        quotes["AAPL"].volume = 600_000
        quotes["AAPL"].volume_ratio = 0.8
        feature = history.HistoryFeature("AAPL", 100.0, 101.0, 99.0, 1.0, 3.0, 2.0, 0.7, 60)
        setup = confirm.score_catalyst_trend_continuation_setup(
            "AAPL", quotes["AAPL"], quotes, {"AAPL": ["AAPL raises guidance after record growth"]}, [],
            {"AAPL": [{"verified": True}]}, confirm.Intraday("AAPL", 101.0, 100.0, 0.1, 0.1, 1.0), feature,
        )
        action, _score, decision, *_ = confirm.classify_decision(
            "AAPL", 0, [], [], "midday", {}, quotes["AAPL"], quotes, {}, [], (0, [], []), entry_models={"catalyst_trend": setup},
        )
        self.assertEqual(action, "buy")
        self.assertIn("Catalyst + relative-strength continuation", decision)

    def test_evidence_revision_momentum_requires_broker_or_fundamental_validation(self) -> None:
        quotes = {symbol: quote(symbol, 0.2) for symbol in ("QQQ", "XLK", "SMH")}
        quotes["AAPL"] = quote("AAPL", 1.3)
        quotes["AAPL"].volume = 600_000
        quotes["AAPL"].volume_ratio = 0.9
        feature = history.HistoryFeature("AAPL", 100.0, 101.0, 99.0, 2.0, 3.0, 2.0, 0.7, 60)
        setup = confirm.score_evidence_revision_momentum_setup(
            "AAPL", quotes["AAPL"], quotes,
            {"AAPL": ["AAPL upgraded to overweight after strong results; broker raises price target."]}, [],
            {"AAPL": [{"verified": True}]}, confirm.Intraday("AAPL", 101.0, 100.0, 0.2, 0.1, 1.0), feature,
        )
        action, _score, decision, *_ = confirm.classify_decision(
            "AAPL", 0, [], [], "midday", {}, quotes["AAPL"], quotes, {}, [], (0, [], []),
            entry_models={"evidence_revision": setup},
        )
        self.assertGreaterEqual(setup[0], 8)
        self.assertEqual(action, "buy")
        self.assertIn("Evidence-backed revision momentum", decision)

    def test_post_earnings_drift_qualifies(self) -> None:
        quotes = {symbol: quote(symbol, 0.2) for symbol in ("QQQ", "XLK", "SMH")}
        quotes["AAPL"] = quote("AAPL", 1.2)
        quotes["AAPL"].volume = 600_000
        quotes["AAPL"].volume_ratio = 0.8
        feature = history.HistoryFeature("AAPL", 100.0, 101.0, 99.0, 4.0, 3.0, 2.0, 0.7, 60)
        setup = confirm.score_post_earnings_drift_setup(
            "AAPL", quotes["AAPL"], quotes, {"AAPL": ["AAPL earnings beat raises guidance on strong growth"]}, [],
            {"AAPL": [{"verified": True}]}, confirm.Intraday("AAPL", 101.0, 100.0, 0.1, 0.1, 1.0), feature,
        )
        self.assertGreaterEqual(setup[0], 8)
        self.assertFalse(any("post-earnings" in blocker for blocker in setup[2]))

    def test_post_earnings_allows_confirmed_report_but_blocks_future_earnings(self) -> None:
        now = dt.datetime(2026, 9, 15, 12, 0, tzinfo=confirm.TZ)
        hits = {"AAPL": ["AAPL earnings beat raises guidance after strong results"]}
        reported_event = {"AAPL": [{"type": "earnings", "event_date": "2026-09-14T00:00:00-05:00"}]}
        future_event = {"AAPL": [{"type": "earnings", "event_date": "2026-09-17T00:00:00-05:00"}]}
        self.assertFalse(confirm.binary_event_risk_for_ticker("AAPL", hits, [], reported_event, now, post_earnings_mode=True))
        self.assertTrue(confirm.binary_event_risk_for_ticker("AAPL", hits, [], future_event, now, post_earnings_mode=True))

    def test_sector_leader_breakout_qualifies(self) -> None:
        quotes = {symbol: quote(symbol, 0.5) for symbol in ("QQQ", "XLK", "SMH")}
        quotes["AAPL"] = quote("AAPL", 1.5)
        quotes["AAPL"].volume = 600_000
        quotes["AAPL"].volume_ratio = 0.8
        feature = history.HistoryFeature("AAPL", 100.0, 101.0, 99.0, 4.0, 3.0, 2.0, 0.9, 60)
        setup = confirm.score_sector_leader_breakout_setup(
            "AAPL", quotes["AAPL"], quotes, {"AAPL": ["AAPL raises guidance after record growth"]}, [],
            {"AAPL": [{"verified": True}]}, confirm.Intraday("AAPL", 101.0, 100.0, 0.1, 0.1, 1.0), feature,
        )
        self.assertGreaterEqual(setup[0], 8)
        self.assertFalse(any("breakout" in blocker for blocker in setup[2]))

    def test_sector_breakout_does_not_require_ticker_specific_catalyst(self) -> None:
        quotes = {symbol: quote(symbol, 0.5) for symbol in ("QQQ", "XLK", "SMH")}
        quotes["AAPL"] = quote("AAPL", 1.5)
        quotes["AAPL"].volume = 600_000
        quotes["AAPL"].volume_ratio = 0.8
        feature = history.HistoryFeature("AAPL", 100.0, 101.0, 99.0, 4.0, 3.0, 2.0, 0.9, 60)
        setup = confirm.score_sector_leader_breakout_setup(
            "AAPL", quotes["AAPL"], quotes, {}, [], {"AAPL": []},
            confirm.Intraday("AAPL", 101.0, 100.0, 0.1, 0.1, 1.0), feature,
        )
        self.assertGreaterEqual(setup[0], 8)
        self.assertFalse(any("ticker-specific catalyst" in blocker for blocker in setup[2]))

    def test_news_dislocation_requires_verified_non_fundamental_stabilization(self) -> None:
        quotes = {symbol: quote(symbol, 0.5) for symbol in ("QQQ", "XLK", "SMH")}
        quotes["AAPL"] = quote("AAPL", -3.0)
        quotes["AAPL"].volume = 600_000
        quotes["AAPL"].volume_ratio = 1.1
        feature = history.HistoryFeature("AAPL", 100.0, 101.0, 99.0, 1.0, 2.0, 2.0, 0.6, 60)
        setup = confirm.score_news_dislocation_setup(
            "AAPL", quotes["AAPL"], quotes,
            {"AAPL": ["AAPL falls after an analyst downgrade on valuation concerns; management reaffirmed outlook."]},
            [], {"AAPL": [{"verified": True}]},
            confirm.Intraday("AAPL", 97.0, 100.0, -0.05, -0.30, -3.0), feature,
        )
        action, _score, decision, _evidence, blockers = confirm.classify_decision(
            "AAPL", 0, [], [], "midday", {}, quotes["AAPL"], quotes, {}, [], (0, [], []),
            entry_models={"news_dislocation": setup},
        )
        self.assertEqual(action, "buy")
        self.assertIn("News-dislocation stabilization", decision)
        self.assertFalse(any("fundamental damage" in blocker for blocker in blockers))

    def test_news_dislocation_rejects_fundamental_damage(self) -> None:
        quotes = {symbol: quote(symbol, 0.5) for symbol in ("QQQ", "XLK", "SMH")}
        quotes["AAPL"] = quote("AAPL", -3.0)
        quotes["AAPL"].volume = 600_000
        quotes["AAPL"].volume_ratio = 1.1
        feature = history.HistoryFeature("AAPL", 100.0, 101.0, 99.0, 1.0, 2.0, 2.0, 0.6, 60)
        setup = confirm.score_news_dislocation_setup(
            "AAPL", quotes["AAPL"], quotes,
            {"AAPL": ["AAPL falls after analyst downgrade as an accounting investigation begins."]},
            [], {"AAPL": [{"verified": True}]},
            confirm.Intraday("AAPL", 97.0, 100.0, -0.05, -0.30, -3.0), feature,
        )
        self.assertLess(setup[0], 8)
        self.assertTrue(any("fundamental damage" in blocker for blocker in setup[2]))

    def test_pullback_reclaim_remains_watch_even_with_soft_confirmation_miss(self) -> None:
        quotes = {
            "AAPL": quote("AAPL", 0.8),
            "QQQ": quote("QQQ", 0.2),
            "XLK": quote("XLK", 0.1),
            "SMH": quote("SMH", 0.2),
        }
        quotes["AAPL"].volume = 600_000
        feature = history.HistoryFeature(
            symbol="AAPL", latest_close=100.0, sma5=101.0, sma20=99.0,
            close_vs_sma20_pct=1.0, trend_20d_pct=3.0, volatility_20d_pct=2.0,
            range_position_60d=0.6, bars=60,
        )
        setup = confirm.score_pullback_reclaim_setup(
            "AAPL", quotes["AAPL"], quotes,
            {"AAPL": ["AAPL raises guidance after record demand"]}, [],
            {"AAPL": [{"verified": True}]},
            confirm.Intraday("AAPL", 101.0, 100.0, -0.1, -0.05, 0.8), feature,
        )
        action, score, _decision, _evidence, blockers = confirm.classify_decision(
            "AAPL", 0, [], [], "midday", {}, quotes["AAPL"], quotes, {}, [], setup,
        )
        self.assertGreaterEqual(score, 8)
        self.assertEqual(action, "watch")
        self.assertTrue(any("no intraday reclaim" in blocker for blocker in blockers))

    def test_binary_event_risk_remains_a_buy_veto(self) -> None:
        quotes = {
            "AAPL": quote("AAPL", 0.8),
            "QQQ": quote("QQQ", 0.2),
            "XLK": quote("XLK", 0.1),
            "SMH": quote("SMH", 0.2),
        }
        quotes["AAPL"].volume = 600_000
        feature = history.HistoryFeature(
            symbol="AAPL", latest_close=100.0, sma5=101.0, sma20=99.0,
            close_vs_sma20_pct=1.0, trend_20d_pct=3.0, volatility_20d_pct=2.0,
            range_position_60d=0.6, bars=60,
        )
        setup = confirm.score_pullback_reclaim_setup(
            "AAPL", quotes["AAPL"], quotes,
            {"AAPL": ["AAPL raises guidance after record demand"]}, [],
            {"AAPL": [{"verified": True}]},
            confirm.Intraday("AAPL", 101.0, 100.0, 0.2, 0.1, 0.8), feature,
            binary_risks=["upcoming earnings event"],
        )
        action, _score, decision, _evidence, _blockers = confirm.classify_decision(
            "AAPL", 0, [], [], "midday", {}, quotes["AAPL"], quotes, {}, [], setup,
        )
        self.assertEqual(action, "no_buy")
        self.assertIn("entry safety gate", decision)

    def test_buy_trade_plan_gives_entry_ceiling_and_stop_limit(self) -> None:
        feature = history.HistoryFeature(
            symbol="AAPL",
            latest_close=100.0,
            sma5=101.0,
            sma20=98.0,
            close_vs_sma20_pct=2.0,
            trend_20d_pct=4.0,
            volatility_20d_pct=2.0,
            range_position_60d=0.7,
            bars=60,
        )
        plan = confirm.trade_plan_for_buy("buy", "AAPL", quote("AAPL", 1.5), feature)
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertGreater(plan.max_entry, 101.0)
        self.assertLess(plan.stop, plan.max_entry * 0.98)
        self.assertLess(plan.stop_limit, plan.stop)
        self.assertEqual(plan.limit_gap_pct, 0.5)
        self.assertIn("post_fill_trailing_stop_limit trail 3.0% limit_offset 0.5%", confirm.trailing_stop_limit_plan_text(plan))
        self.assertAlmostEqual(plan.trailing_buy_offset_pct, 0.5)
        self.assertAlmostEqual(plan.trailing_buy_offset, 0.51)
        self.assertIn("pre_fill_trailing_buy offset $0.51 (0.50%", confirm.trailing_buy_plan_text(plan))

    def test_buy_trade_plan_allows_zero_trailing_buy_offset_without_history(self) -> None:
        plan = confirm.trade_plan_for_buy("buy", "AAPL", quote("AAPL", 100.0), None)
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.trailing_buy_offset_pct, 0.0)
        self.assertEqual(plan.trailing_buy_offset, 0.0)
        self.assertIn("pre_fill_trailing_buy offset $0.00 (0.00%", confirm.trailing_buy_plan_text(plan))

    def test_intraday_buy_protection_text_states_suggested_trailing_stop(self) -> None:
        text = confirm.intraday_buy_protection_text({"protection": {"status": "ok", "trailing_pct": 0.89, "stop": 13.06}})
        self.assertIn("after-buy intraday trailing stop: 0.89%", text)
        self.assertIn("suggested stop $13.06", text)

    def test_intraday_buy_includes_pre_fill_trailing_buy_cash_and_percent(self) -> None:
        feature = history.HistoryFeature(
            symbol="AAPL", latest_close=100.0, sma5=101.0, sma20=98.0,
            close_vs_sma20_pct=2.0, trend_20d_pct=4.0, volatility_20d_pct=2.0,
            range_position_60d=0.7, bars=60,
        )
        text = confirm.intraday_trailing_buy_text("AAPL", quote("AAPL", 1.5), feature)
        self.assertIn("pre-fill intraday trailing BUY: $0.51 (0.50%", text)

    def test_intraday_buy_allows_zero_pre_fill_trailing_buy_offset(self) -> None:
        text = confirm.intraday_trailing_buy_text("AAPL", quote("AAPL", 100.0), None)
        self.assertIn("pre-fill intraday trailing BUY: $0.00 (0.00%", text)

    def test_intraday_buy_protection_text_explains_unavailable_offset(self) -> None:
        text = confirm.intraday_buy_protection_text({"protection": {"status": "unavailable", "reason": "insufficient data"}})
        self.assertIn("after-buy intraday trailing stop: unavailable", text)

    def test_catalyst_calendar_extracts_earnings_event_and_blocks_buy(self) -> None:
        with TemporaryDirectory() as tmpdir:
            now = dt.datetime(2026, 6, 29, 9, 0, tzinfo=confirm.TZ)
            calendar_path = Path(tmpdir) / "market-catalyst-calendar.json"
            with patch.object(confirm, "CATALYST_CALENDAR_FILE", calendar_path):
                events = confirm.update_catalyst_calendar(
                    ["AAPL"],
                    {"AAPL": ["AAPL reports earnings July 1 before the open with guidance in focus"]},
                    {"AAPL": []},
                    {},
                    now,
                )
            self.assertIn("AAPL", events)
            self.assertEqual(events["AAPL"][0]["type"], "earnings")
            risks = confirm.binary_event_risk_for_ticker("AAPL", {}, [], events, now)
            self.assertTrue(any("upcoming earnings" in risk for risk in risks))

    def test_catalyst_calendar_classifies_scheduled_delisting(self) -> None:
        with TemporaryDirectory() as tmpdir:
            now = dt.datetime(2026, 6, 29, 9, 0, tzinfo=confirm.TZ)
            calendar_path = Path(tmpdir) / "market-catalyst-calendar.json"
            with patch.object(confirm, "CATALYST_CALENDAR_FILE", calendar_path):
                events = confirm.update_catalyst_calendar(
                    ["WBD"],
                    {"WBD": ["WBD will cease trading after the Skydance deal closes; last trading day is July 1"]},
                    {"WBD": []},
                    {},
                    now,
                )
            self.assertEqual(events["WBD"][0]["type"], "delisting")
            self.assertEqual(confirm.scheduled_delisting_symbols(events), {"WBD"})

    def test_idea_screen_persists_quality_candidates(self) -> None:
        with TemporaryDirectory() as tmpdir:
            screen_path = Path(tmpdir) / "market-idea-screen.json"
            now = dt.datetime(2026, 6, 29, 9, 0, tzinfo=confirm.TZ)
            quotes = {"AAPL": quote("AAPL", 2.0)}
            quotes["AAPL"].volume_ratio = 1.1
            feature = history.HistoryFeature(
                symbol="AAPL",
                latest_close=100.0,
                sma5=101.0,
                sma20=98.0,
                close_vs_sma20_pct=2.0,
                trend_20d_pct=4.0,
                volatility_20d_pct=2.0,
                range_position_60d=0.7,
                bars=60,
            )
            with patch.object(confirm, "IDEA_SCREEN_FILE", screen_path):
                state = confirm.update_idea_screen(
                    [("watch", 3, "AAPL", "WATCH: AAPL", ["price confirms: +2.00% today"], [])],
                    quotes,
                    {"AAPL": feature},
                    {"AAPL": [{"verified": True}]},
                    now,
                )
                symbols = confirm.idea_screen_symbols(now)
            self.assertIn("AAPL", state["candidates"])
            self.assertIn("AAPL", symbols)

    def test_same_session_position_changes_mark_traded_symbols(self) -> None:
        with TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            now = dt.datetime(2026, 6, 30, 12, 15, tzinfo=confirm.TZ)
            (out_dir / "2026-06-30T1110-market-confirm-one-hour.md").write_text(
                """
## open_positions
- GOOG: shares 0.1; avg_price 350
""".strip()
                + "\n",
                encoding="utf-8",
            )
            (out_dir / "2026-06-30T1140-market-confirm-midday.md").write_text(
                """
## open_positions
- GOOG: shares 0.1; avg_price 350
- LOW: shares 0.1; avg_price 220
""".strip()
                + "\n",
                encoding="utf-8",
            )
            with patch.object(confirm, "OUT_DIR", out_dir):
                traded = confirm.position_change_symbols_for_session(now, {"LOW": {"status": "open"}})

            self.assertEqual(traded, {"GOOG", "LOW"})

    def test_same_session_holdoff_blocks_buy_and_sell_rows(self) -> None:
        rows = [
            ("sell", 3, "LOW", "SELL SIGNAL: LOW", ["weakness"], []),
            ("predictive_buy", 9, "GOOG", "PREDICTIVE BUY SIGNAL: GOOG", ["setup"], []),
            ("buy", 8, "TSM", "BUY SIGNAL: TSM", ["setup"], []),
        ]
        updated, notes = confirm.apply_same_session_trade_holdoff(rows, {"GOOG", "LOW"})

        by_ticker = {row[2]: row for row in updated}
        self.assertEqual(by_ticker["LOW"][0], "hold")
        self.assertIn("same-session trade holdoff", by_ticker["LOW"][5][-1])
        self.assertEqual(by_ticker["GOOG"][0], "no_buy")
        self.assertEqual(by_ticker["TSM"][0], "buy")
        self.assertTrue(notes)

    def test_low_spending_power_rotation_excludes_holdoff_symbols(self) -> None:
        rows = [
            ("buy", 9, "GOOG", "BUY SIGNAL: GOOG", ["setup"], []),
            ("hold", 3, "LOW", "HOLD: LOW - no sell signal", ["price weak today"], []),
        ]

        with patch.object(confirm, "ROBINHOOD_SPENDING_POWER_GATE_ENABLED", True):
            updated, notes = confirm.apply_low_spending_power_gate(rows, 0.0, {"LOW"})

        by_ticker = {row[2]: row for row in updated}
        self.assertEqual(by_ticker["GOOG"][0], "no_buy")
        self.assertEqual(by_ticker["LOW"][0], "hold")
        self.assertTrue(any("BUY signals blocked" in note for note in notes))

    def test_low_spending_power_gate_can_be_disabled_for_schwab_profile(self) -> None:
        rows = [("buy", 9, "GOOG", "BUY SIGNAL: GOOG", ["setup"], [])]

        with patch.object(confirm, "ROBINHOOD_SPENDING_POWER_GATE_ENABLED", False):
            updated, notes = confirm.apply_low_spending_power_gate(rows, 0.0)

        self.assertEqual(updated, rows)
        self.assertTrue(any("disabled" in note for note in notes))

    def test_read_positions_can_filter_to_schwab_only(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manual_path = root / "manual.json"
            robinhood_path = root / "robinhood.json"
            schwab_path = root / "schwab.json"
            manual_path.write_text(
                '{"positions":{"MAN":{"status":"open","shares":1,"broker":"manual"}}}\n',
                encoding="utf-8",
            )
            robinhood_path.write_text(
                '{"positions":{"RH":{"status":"open","shares":1,"broker":"robinhood"}}}\n',
                encoding="utf-8",
            )
            schwab_path.write_text(
                '{"positions":{"SCHW":{"status":"open","shares":1,"broker":"schwab"}}}\n',
                encoding="utf-8",
            )

            with (
                patch.object(confirm, "POSITIONS_FILE", manual_path),
                patch.object(confirm, "ROBINHOOD_POSITIONS_FILE", robinhood_path),
                patch.object(confirm, "SCHWAB_POSITIONS_FILE", schwab_path),
                patch.object(confirm, "POSITION_BROKERS", {"schwab"}),
            ):
                positions = confirm.read_positions()

        self.assertEqual(set(positions), {"SCHW"})

    def test_manual_close_suppresses_older_broker_snapshot(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manual_path = root / "manual.json"
            robinhood_path = root / "robinhood.json"
            schwab_path = root / "schwab.json"
            manual_path.write_text(
                """
{
  "positions": {
    "MRK": {
      "status": "closed",
      "closed_at": "2026-08-28T11:40:00-05:00",
      "broker": "schwab"
    }
  }
}
""".strip()
                + "\n",
                encoding="utf-8",
            )
            robinhood_path.write_text('{"positions":{}}\n', encoding="utf-8")
            schwab_path.write_text(
                """
{
  "generated_at": "2026-08-25T16:45:15+00:00",
  "positions": {
    "MRK": {
      "status": "open",
      "shares": 1,
      "broker": "schwab"
    },
    "HAS": {
      "status": "open",
      "shares": 1,
      "broker": "schwab"
    }
  }
}
""".strip()
                + "\n",
                encoding="utf-8",
            )

            with (
                patch.object(confirm, "POSITIONS_FILE", manual_path),
                patch.object(confirm, "ROBINHOOD_POSITIONS_FILE", robinhood_path),
                patch.object(confirm, "SCHWAB_POSITIONS_FILE", schwab_path),
                patch.object(confirm, "POSITION_BROKERS", {"manual", "robinhood", "schwab"}),
            ):
                positions = confirm.read_positions()

        self.assertEqual(set(positions), {"HAS"})

    def test_account_fit_gate_blocks_fractional_only_buy_signals(self) -> None:
        rows = [
            ("buy", 7, "AVGO", "BUY SIGNAL: AVGO", ["setup"], []),
            ("predictive_buy", 8, "TWO", "PREDICTIVE BUY SIGNAL: TWO", ["setup"], []),
        ]
        quotes = {
            "AVGO": confirm.Quote("AVGO", 381.88, None, None, None, None, None, None, None),
            "TWO": confirm.Quote("TWO", 12.10, None, None, None, None, None, None, None),
        }

        updated, notes = confirm.apply_account_fit_gate(rows, quotes, 36.91)

        by_ticker = {row[2]: row for row in updated}
        self.assertEqual(by_ticker["AVGO"][0], "no_buy")
        self.assertIn("exceeds $125.00 transaction cap", by_ticker["AVGO"][5][-1])
        self.assertEqual(by_ticker["TWO"][0], "predictive_buy")
        self.assertTrue(notes)

    def test_schwab_balance_gate_allows_buy_when_one_share_fits(self) -> None:
        rows = [("buy", 7, "PFE", "BUY SIGNAL: PFE", ["setup"], [])]
        quotes = {"PFE": confirm.Quote("PFE", 27.0, None, None, None, None, None, None, None)}

        with patch.object(confirm, "SCHWAB_BALANCE_GATE_ENABLED", True):
            updated, notes = confirm.apply_schwab_balance_gate(rows, quotes, 100.0, "Schwab stock buying power")

        self.assertEqual(updated[0][0], "buy")
        self.assertTrue(any("one share fits" in item for item in updated[0][4]))
        self.assertTrue(notes)

    def test_schwab_balance_gate_blocks_buy_when_one_share_does_not_fit(self) -> None:
        rows = [("buy", 7, "AVGO", "BUY SIGNAL: AVGO", ["setup"], [])]
        quotes = {"AVGO": confirm.Quote("AVGO", 300.0, None, None, None, None, None, None, None)}

        with patch.object(confirm, "SCHWAB_BALANCE_GATE_ENABLED", True):
            updated, notes = confirm.apply_schwab_balance_gate(rows, quotes, 100.0, "Schwab stock buying power")

        self.assertEqual(updated[0][0], "no_buy")
        self.assertIn("Schwab buying power too low", updated[0][3])
        self.assertTrue(notes)

    def test_existing_position_gate_blocks_duplicate_buy_across_brokers(self) -> None:
        rows = [
            ("buy", 7, "AMD", "BUY SIGNAL: AMD", ["setup"], []),
            ("buy", 7, "PFE", "BUY SIGNAL: PFE", ["setup"], []),
            ("sell", 5, "AMD", "SELL SIGNAL: AMD", ["weak"], []),
        ]
        positions = {"AMD": {"status": "open", "broker": "robinhood", "brokers": ["robinhood"]}}

        updated, notes = confirm.apply_existing_position_buy_gate(rows, positions)

        by_decision = {(row[0], row[2]): row for row in updated}
        self.assertEqual(by_decision[("no_buy", "AMD")][3], "NO BUY: AMD - already held in another account")
        self.assertEqual(by_decision[("buy", "PFE")][0], "buy")
        self.assertEqual(by_decision[("sell", "AMD")][0], "sell")
        self.assertTrue(notes)

    def test_broker_eligibility_gate_blocks_configured_buy_symbols(self) -> None:
        rows = [
            ("buy", 7, "SNY", "BUY SIGNAL: SNY", ["setup"], []),
            ("buy", 7, "AMD", "BUY SIGNAL: AMD", ["setup"], []),
        ]

        updated, notes = confirm.apply_broker_eligibility_gate(
            rows,
            {"SNY": "not supported on Robinhood; Schwab warns the stock may be subject to foreign tax"},
        )

        by_ticker = {row[2]: row for row in updated}
        self.assertEqual(by_ticker["SNY"][0], "no_buy")
        self.assertIn("broker/tax eligibility blocker", by_ticker["SNY"][5][-1])
        self.assertEqual(by_ticker["AMD"][0], "buy")
        self.assertTrue(notes)

    def test_broker_eligibility_gate_allows_non_buy_rows(self) -> None:
        rows = [
            ("sell", 5, "SNY", "SELL SIGNAL: SNY", ["weakness"], []),
            ("hold", 2, "SNY", "HOLD: SNY - no sell signal", ["stable"], []),
        ]

        updated, notes = confirm.apply_broker_eligibility_gate(rows, {"SNY": "foreign tax risk"})

        self.assertEqual([row[0] for row in updated], ["sell", "hold"])
        self.assertFalse(notes)

    def test_constructive_history_can_support_early_buy_without_sector_lead(self) -> None:
        quotes = {
            "AAPL": quote("AAPL", 0.4),
            "QQQ": quote("QQQ", 0.3),
            "XLK": quote("XLK", 0.3),
            "SMH": quote("SMH", 0.3),
        }
        intraday = confirm.Intraday("AAPL", 101.0, 100.0, 0.2, 0.3, 1.0)
        feature = history.HistoryFeature(
            symbol="AAPL",
            latest_close=100.0,
            sma5=101.0,
            sma20=98.0,
            close_vs_sma20_pct=2.0,
            trend_20d_pct=4.0,
            volatility_20d_pct=2.0,
            range_position_60d=0.7,
            bars=60,
        )
        score, evidence, blockers = confirm.score_early_setup(
            "AAPL",
            quotes["AAPL"],
            quotes,
            {"AAPL": ["AAPL raises guidance after strong growth"]},
            [],
            {"AAPL": [{"verified": True}]},
            intraday,
            feature,
        )
        self.assertGreaterEqual(score, 6)
        self.assertFalse(any("relative strength lead insufficient" in blocker for blocker in blockers))
        self.assertTrue(any("constructive pullback" in item for item in evidence))

    def test_sell_cancel_is_suppressed_after_position_disappears(self) -> None:
        with TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "market-recommendation-state.json"
            report_path = Path(tmpdir) / "report.md"
            state_path.write_text(
                """
{
  "schema": "openclaw.market_worker.recommendation_state.v1",
  "active": {
    "AMD": {
      "action": "SELL",
      "valid_until": "2026-06-16T09:40:00-05:00",
      "last_seen_at": "2026-06-16T08:40:00-05:00"
    }
  }
}
""".strip()
                + "\n",
                encoding="utf-8",
            )
            report_path.write_text(
                """
valid_until: 2026-06-16T09:40:00-05:00

## decisions
- NO BUY SIGNALS
""".strip()
                + "\n",
                encoding="utf-8",
            )
            with (
                patch.object(confirm, "RECOMMENDATION_STATE_FILE", state_path),
                patch.object(confirm, "read_positions", return_value={}),
            ):
                message, status = confirm.recommendation_state_changes(report_path, "NO BUY SIGNALS")

            self.assertIsNone(message)
            self.assertEqual(status, "telegram notification skipped: no recommendation state changes")

    def test_sell_cancel_still_sends_when_position_is_still_open(self) -> None:
        with TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "market-recommendation-state.json"
            report_path = Path(tmpdir) / "report.md"
            state_path.write_text(
                """
{
  "schema": "openclaw.market_worker.recommendation_state.v1",
  "active": {
    "AMD": {
      "action": "SELL",
      "valid_until": "2026-06-16T09:40:00-05:00",
      "last_seen_at": "2026-06-16T08:40:00-05:00"
    }
  }
}
""".strip()
                + "\n",
                encoding="utf-8",
            )
            report_path.write_text(
                """
valid_until: 2026-06-16T09:40:00-05:00

## decisions
- HOLD: AMD - no sell signal (score 0)
- NO BUY SIGNALS
""".strip()
                + "\n",
                encoding="utf-8",
            )
            with (
                patch.object(confirm, "RECOMMENDATION_STATE_FILE", state_path),
                patch.object(confirm, "read_positions", return_value={"AMD": {"status": "open"}}),
            ):
                message, status = confirm.recommendation_state_changes(report_path, "NO BUY SIGNALS")

            self.assertEqual(status, "recommendation state changed")
            self.assertIn("CANCEL SELL: AMD", message or "")

    def test_buy_cancel_is_suppressed_after_position_appears(self) -> None:
        with TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "market-recommendation-state.json"
            report_path = Path(tmpdir) / "report.md"
            state_path.write_text(
                """
{
  "schema": "openclaw.market_worker.recommendation_state.v1",
  "active": {
    "PSN": {
      "action": "BUY",
      "valid_until": "2026-06-16T14:40:00-05:00",
      "last_seen_at": "2026-06-16T13:40:00-05:00"
    }
  }
}
""".strip()
                + "\n",
                encoding="utf-8",
            )
            report_path.write_text(
                """
valid_until: 2026-06-16T14:40:00-05:00

## decisions
- NO BUY SIGNALS
""".strip()
                + "\n",
                encoding="utf-8",
            )
            with (
                patch.object(confirm, "RECOMMENDATION_STATE_FILE", state_path),
                patch.object(confirm, "read_positions", return_value={"PSN": {"status": "open"}}),
            ):
                message, status = confirm.recommendation_state_changes(report_path, "NO BUY SIGNALS")

            self.assertIsNone(message)
            self.assertEqual(status, "telegram notification skipped: no recommendation state changes")

    def test_buy_cancel_still_sends_when_position_was_not_opened(self) -> None:
        with TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "market-recommendation-state.json"
            report_path = Path(tmpdir) / "report.md"
            state_path.write_text(
                """
{
  "schema": "openclaw.market_worker.recommendation_state.v1",
  "active": {
    "PSN": {
      "action": "BUY",
      "valid_until": "2026-06-16T14:40:00-05:00",
      "last_seen_at": "2026-06-16T13:40:00-05:00"
    }
  }
}
""".strip()
                + "\n",
                encoding="utf-8",
            )
            report_path.write_text(
                """
valid_until: 2026-06-16T14:40:00-05:00

## decisions
- NO BUY SIGNALS
""".strip()
                + "\n",
                encoding="utf-8",
            )
            with (
                patch.object(confirm, "RECOMMENDATION_STATE_FILE", state_path),
                patch.object(confirm, "read_positions", return_value={}),
            ):
                message, status = confirm.recommendation_state_changes(report_path, "NO BUY SIGNALS")

            self.assertEqual(status, "recommendation state changed")
            self.assertIn("CANCEL BUY: PSN", message or "")

    def test_intraday_notification_batch_keeps_all_sells_and_cancels_but_caps_buys(self) -> None:
        changes = [
            (f"S{i}", "PAPER_SELL", f"sell {i}") for i in range(8)
        ] + [
            (f"C{i}", "CANCEL_BUY", f"cancel {i}") for i in range(8)
        ] + [
            (f"B{i}", "PAPER_BUY", f"buy {i}") for i in range(8)
        ]
        selected = confirm.balanced_notification_changes(changes)
        self.assertEqual(len(selected), 22)
        self.assertEqual([item[0] for item in selected[:8]], [f"S{i}" for i in range(8)])
        self.assertEqual([item[0] for item in selected[8:16]], [f"C{i}" for i in range(8)])
        self.assertEqual([item[0] for item in selected[16:]], [f"B{i}" for i in range(6)])

    def test_only_delivered_intraday_alerts_are_marked_notified(self) -> None:
        with TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "market-recommendation-state.json"
            report_path = Path(tmpdir) / "report.md"
            decisions = "\n".join(
                [f"- INTRADAY PAPER SELL: S{i} - models: test" for i in range(4)]
                + [f"- INTRADAY PAPER BUY: B{i} - models: test" for i in range(4)]
            )
            report_path.write_text(
                f"valid_until: 2026-06-16T09:40:00-05:00\n\n## decisions\n{decisions}\n",
                encoding="utf-8",
            )
            with (
                patch.object(confirm, "RECOMMENDATION_STATE_FILE", state_path),
                patch.object(confirm, "read_positions", return_value={}),
            ):
                _, status, delivered = confirm.recommendation_state_change_details(report_path, "paper")
                self.assertEqual(status, "recommendation state changed")
                self.assertEqual(len(delivered), 8)
                before = confirm.read_recommendation_state()["active"]
                self.assertTrue(all(not record["notified"] for record in before.values()))
                confirm.acknowledge_recommendation_delivery(delivered)
                after = confirm.read_recommendation_state()["active"]

            self.assertEqual(sum(bool(record["notified"]) for record in after.values()), 8)
            self.assertEqual(sum(not bool(record["notified"]) for record in after.values()), 0)

    def test_delisting_gate_blocks_new_buy_but_preserves_sell(self) -> None:
        rows = [
            ("buy", 9, "WBD", "BUY CANDIDATE: WBD", ["momentum"], []),
            ("sell", 8, "WBD", "SELL SIGNAL: WBD", ["risk"], []),
            ("buy", 9, "AMD", "BUY CANDIDATE: AMD", ["momentum"], []),
        ]
        gated, notes = confirm.apply_delisting_buy_gate(rows, {"WBD"})
        self.assertEqual(gated[0][0], "no_buy")
        self.assertIn("scheduled for delisting", gated[0][3])
        self.assertEqual(gated[1][0], "sell")
        self.assertEqual(gated[2][0], "buy")
        self.assertIn("WBD", notes)

    def test_delisting_gate_removes_intraday_buy_and_cancel_path(self) -> None:
        signals = {
            "WBD": {"signals": [
                {"side": "BUY", "model": "VWAP reclaim"},
                {"side": "SELL", "model": "VWAP rejection"},
            ]},
            "AMD": {"signals": [{"side": "BUY", "model": "VWAP reclaim"}]},
        }
        gated = confirm.apply_intraday_paper_delisting_gate(signals, {"WBD"})
        self.assertEqual([s["side"] for s in gated["WBD"]["signals"]], ["SELL"])
        self.assertIn("scheduled for delisting", gated["WBD"]["buy_gate_blocker"])
        self.assertEqual(gated["AMD"]["signals"][0]["side"], "BUY")
        after_position_gate = confirm.apply_intraday_paper_position_gate(gated, {})
        self.assertEqual(after_position_gate["WBD"]["signals"], [])

    def test_scheduled_delisting_symbols_use_catalyst_evidence_and_explicit_override(self) -> None:
        events = {
            "SKY": [{"type": "delisting", "snippet": "will cease trading after the merger closes"}],
            "AMD": [{"type": "merger", "snippet": "announces a merger"}],
        }
        with patch.object(confirm, "EXCLUDED_TICKERS", frozenset({"WBD"})):
            self.assertEqual(confirm.scheduled_delisting_symbols(events), {"SKY", "WBD"})

    def test_intraday_paper_buy_uses_price_and_whole_share_gates(self) -> None:
        signals = {
            "PENNY": {"signals": [{"side": "BUY", "model": "VWAP reclaim"}]},
            "COSTLY": {"signals": [{"side": "BUY", "model": "VWAP reclaim"}]},
            "HELD": {"signals": [{"side": "SELL", "model": "VWAP rejection"}]},
        }
        quotes = {
            "PENNY": confirm.Quote("PENNY", 1.50, 1.40, 1.0, 1.45, 200_000, 1_000_000, 0.2, None),
            "COSTLY": confirm.Quote("COSTLY", 150.00, 149.0, 1.0, 149.5, 200_000, 1_000_000, 0.2, None),
            "HELD": quote("HELD", -1.0),
        }
        filtered = confirm.apply_intraday_paper_buy_price_gates(signals, quotes, set(), 1_000.0)
        self.assertFalse(filtered["PENNY"]["signals"])
        self.assertIn("durable-trade floor", filtered["PENNY"]["buy_gate_blocker"])
        self.assertFalse(filtered["COSTLY"]["signals"])
        self.assertIn("whole-share protective-stop gate", filtered["COSTLY"]["buy_gate_blocker"])
        self.assertEqual(filtered["HELD"]["signals"], signals["HELD"]["signals"])

    def test_price_blocked_intraday_buy_does_not_become_cancel_buy(self) -> None:
        signals = {
            "COSTLY": {
                "signals": [
                    {"side": "BUY", "model": "VWAP reclaim"},
                    {"side": "SELL", "model": "VWAP rejection"},
                ]
            }
        }
        quotes = {
            "COSTLY": confirm.Quote("COSTLY", 150.00, 149.0, 1.0, 149.5, 200_000, 1_000_000, 0.2, None),
        }
        price_gated = confirm.apply_intraday_paper_buy_price_gates(signals, quotes, set(), 1_000.0)
        position_gated = confirm.apply_intraday_paper_position_gate(price_gated, {})
        self.assertEqual(position_gated["COSTLY"]["signals"], [])
        self.assertIn("no CANCEL BUY", position_gated["COSTLY"]["position_gate_blocker"])

    def test_unheld_intraday_sell_becomes_cancel_buy(self) -> None:
        signals = {
            "UNHELD": {"signals": [{"side": "SELL", "model": "VWAP rejection", "reason": "weakness"}]},
            "HELD": {"signals": [{"side": "SELL", "model": "VWAP rejection", "reason": "weakness"}]},
        }
        gated = confirm.apply_intraday_paper_position_gate(signals, {"HELD": {"shares": 1}})
        self.assertEqual(gated["UNHELD"]["signals"][0]["side"], "CANCEL_BUY")
        self.assertIn("no open position", gated["UNHELD"]["signals"][0]["reason"])
        self.assertEqual(gated["HELD"]["signals"][0]["side"], "SELL")

    def test_held_intraday_buy_is_suppressed(self) -> None:
        signals = {
            "HELD": {"signals": [{"side": "BUY", "model": "VWAP reclaim"}, {"side": "SELL", "model": "weakness"}]},
            "UNHELD": {"signals": [{"side": "BUY", "model": "VWAP reclaim"}]},
        }
        gated = confirm.apply_intraday_paper_existing_position_gate(signals, {"HELD": {"shares": 1}})
        self.assertEqual([signal["side"] for signal in gated["HELD"]["signals"]], ["SELL"])
        self.assertIn("already held", gated["HELD"]["buy_gate_blocker"])
        self.assertEqual(gated["UNHELD"]["signals"][0]["side"], "BUY")

    def test_cancel_buy_is_parsed_as_notification_action(self) -> None:
        with TemporaryDirectory() as tmpdir:
            report = Path(tmpdir) / "report.md"
            report.write_text(
                "valid_until: 2026-06-16T09:40:00-05:00\n\n## decisions\n"
                "- CANCEL BUY: AMD - intraday bearish model; research alert only\n",
                encoding="utf-8",
            )
            _, active = confirm.parse_report_recommendations(report)
        self.assertEqual(active["AMD"]["action"], "CANCEL_BUY")

    def test_channel_intraday_buy_includes_trailing_details_from_report_section(self) -> None:
        with TemporaryDirectory() as tmpdir:
            report = Path(tmpdir) / "report.md"
            report.write_text(
                "## decisions\n"
                "- INTRADAY PAPER BUY: AMD - score 11; models: VWAP reclaim; research alert only\n"
                "\n## intraday_paper_details\n"
                "### intraday paper AMD\n"
                "- pre-fill intraday trailing BUY: $0.51 (0.50%; trigger after rebound from trailing low)\n"
                "- protection: after-buy intraday trailing stop: 0.89% (suggested stop $13.06; recent swing low minus 0.3 ATR)\n",
                encoding="utf-8",
            )
            _, active = confirm.parse_report_recommendations(report)
            detail = confirm.notification_detail(report, "AMD", active["AMD"]["action"], active["AMD"]["line"])

        self.assertIn("pre-fill intraday trailing BUY: $0.51", detail)
        self.assertIn("protection: after-buy intraday trailing stop: 0.89%", detail)

    def test_cancel_buy_requires_a_previously_delivered_buy(self) -> None:
        with TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "market-recommendation-state.json"
            buy_report = Path(tmpdir) / "buy.md"
            cancel_report = Path(tmpdir) / "cancel.md"
            buy_report.write_text(
                "## decisions\n- INTRADAY PAPER BUY: AMD - models: test\n",
                encoding="utf-8",
            )
            cancel_report.write_text(
                "## decisions\n- CANCEL BUY: AMD - intraday bearish model; research alert only\n",
                encoding="utf-8",
            )
            with (
                patch.object(confirm, "RECOMMENDATION_STATE_FILE", state_path),
                patch.object(confirm, "read_positions", return_value={}),
            ):
                _, _, delivered = confirm.recommendation_state_change_details(buy_report, "buy")
                self.assertEqual(delivered, [("AMD", "PAPER_BUY")])
                confirm.acknowledge_recommendation_delivery(delivered)
                self.assertIn("AMD", confirm.intraday_buy_origins())
                message, status, delivered = confirm.recommendation_state_change_details(cancel_report, "cancel")

            self.assertEqual(status, "recommendation state changed")
            self.assertIn("CANCEL BUY: AMD", message or "")
            self.assertEqual(delivered, [("AMD", "CANCEL_BUY")])

    def test_cancel_buy_is_suppressed_without_a_delivered_buy(self) -> None:
        with TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "market-recommendation-state.json"
            buy_report = Path(tmpdir) / "buy.md"
            cancel_report = Path(tmpdir) / "cancel.md"
            buy_report.write_text(
                "## decisions\n- INTRADAY PAPER BUY: AMD - models: test\n",
                encoding="utf-8",
            )
            cancel_report.write_text(
                "## decisions\n- CANCEL BUY: AMD - intraday bearish model; research alert only\n",
                encoding="utf-8",
            )
            with (
                patch.object(confirm, "RECOMMENDATION_STATE_FILE", state_path),
                patch.object(confirm, "read_positions", return_value={}),
            ):
                _, _, delivered = confirm.recommendation_state_change_details(buy_report, "buy")
                self.assertEqual(delivered, [("AMD", "PAPER_BUY")])
                message, status, delivered = confirm.recommendation_state_change_details(cancel_report, "cancel")

            self.assertIsNone(message)
            self.assertEqual(status, "telegram notification skipped: no recommendation state changes")
            self.assertEqual(delivered, [])

    def test_cancel_buy_ignores_stale_prior_state_without_delivered_origin(self) -> None:
        with TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "market-recommendation-state.json"
            cancel_report = Path(tmpdir) / "cancel.md"
            state_path.write_text(
                json.dumps({
                    "active": {
                        "AMD": {"action": "PAPER_BUY", "notified": True}
                    }
                }),
                encoding="utf-8",
            )
            cancel_report.write_text(
                "## decisions\n- CANCEL BUY: AMD - intraday bearish model; research alert only\n",
                encoding="utf-8",
            )
            with (
                patch.object(confirm, "RECOMMENDATION_STATE_FILE", state_path),
                patch.object(confirm, "read_positions", return_value={}),
            ):
                message, status, delivered = confirm.recommendation_state_change_details(cancel_report, "cancel")

        self.assertIsNone(message)
        self.assertEqual(status, "telegram notification skipped: no recommendation state changes")
        self.assertEqual(delivered, [])

    def test_unheld_paper_sell_is_not_renotified_after_recurrence(self) -> None:
        with TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "market-recommendation-state.json"
            first = Path(tmpdir) / "first.md"
            empty = Path(tmpdir) / "empty.md"
            again = Path(tmpdir) / "again.md"
            first.write_text("## decisions\n- INTRADAY PAPER SELL: AMD - models: test\n", encoding="utf-8")
            empty.write_text("## decisions\n- NO BUY SIGNALS\n", encoding="utf-8")
            again.write_text("## decisions\n- INTRADAY PAPER SELL: AMD - models: test\n", encoding="utf-8")
            with (
                patch.object(confirm, "RECOMMENDATION_STATE_FILE", state_path),
                patch.object(confirm, "read_positions", return_value={}),
            ):
                _, _, delivered = confirm.recommendation_state_change_details(first, "paper")
                self.assertEqual(delivered, [("AMD", "PAPER_SELL")])
                confirm.acknowledge_recommendation_delivery(delivered)
                confirm.recommendation_state_change_details(empty, "paper")
                message, status, delivered = confirm.recommendation_state_change_details(again, "paper")
            self.assertIsNone(message)
            self.assertEqual(status, "telegram notification skipped: no recommendation state changes")
            self.assertEqual(delivered, [])

    def test_failed_telegram_send_keeps_alert_pending(self) -> None:
        with TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "market-recommendation-state.json"
            report_path = Path(tmpdir) / "report.md"
            report_path.write_text(
                "valid_until: 2026-06-16T09:40:00-05:00\n\n## decisions\n"
                "- INTRADAY PAPER BUY: AMD - models: test\n",
                encoding="utf-8",
            )
            with (
                patch.object(confirm, "RECOMMENDATION_STATE_FILE", state_path),
                patch.object(confirm, "read_positions", return_value={}),
                patch.object(confirm, "NOTIFY_TELEGRAM", True),
                patch.object(confirm, "TELEGRAM_TARGET", "test-target"),
                patch.object(confirm, "NOTIFY_DRY_RUN", False),
                patch.object(confirm.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="", stderr="failed")),
            ):
                status = confirm.notify_telegram("midday", report_path, "paper")
                active = confirm.read_recommendation_state()["active"]

            self.assertIn("telegram notification failed", status)
            self.assertFalse(active["AMD"]["notified"])


if __name__ == "__main__":
    unittest.main()
