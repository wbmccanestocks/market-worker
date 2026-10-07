import json
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest import mock

import market_history_analysis as history
import market_protective_order_review as review
import market_schwab


class MarketProtectiveOrderReviewTest(unittest.TestCase):
    def test_intraday_origin_uses_structure_stop_and_preserves_profit(self) -> None:
        stop, limit, trailing_pct, reasons = review.suggested_intraday_stop(
            entry=100.0,
            price=103.0,
            plan={"status": "ok", "stop": 101.0, "reason": "recent three-bar swing low minus 0.3 five-minute ATR"},
        )
        self.assertEqual(stop, 101.0)
        self.assertLess(limit, stop)
        self.assertAlmostEqual(trailing_pct, (103.0 - 101.0) / 103.0 * 100)
        self.assertIn("intraday-origin", reasons[0])

    def test_normalize_open_orders_preserves_percent_trailing_offset(self) -> None:
        orders = market_schwab.normalize_open_orders(
            [{
                "orderId": "1",
                "status": "WORKING",
                "orderType": "TRAILING_STOP",
                "stopPriceOffset": 2.5,
                "stopPriceLinkType": "PERCENT",
                "orderLegCollection": [{
                    "instruction": "SELL",
                    "quantity": 3,
                    "instrument": {"symbol": "NKE"},
                }],
            }]
        )
        self.assertEqual(orders[0]["trailing_pct"], 2.5)
        self.assertEqual(orders[0]["trailing_offset_type"], "percent")

    def test_existing_trailing_order_suppresses_duplicate_protective_suggestion(self) -> None:
        trailing = [{"type": "trailing_stop", "trailing_pct": 2.5}]
        action = review.classify_order_review(
            ticker="NKE", current_stop=None, suggested=98.0,
            sell_score=1, sell_threshold=4, evidence=[], orders=trailing,
            order_eligible=True, existing_trailing_orders=trailing,
        )
        self.assertEqual(action, "leave_existing_order")
        self.assertEqual(review.trailing_order_text(trailing), "2.5%")

    def test_relative_trailing_offset_gap_over_ten_percent_requires_change(self) -> None:
        action = review.classify_order_review(
            ticker="TQQQ", current_stop=None, suggested=99.11,
            sell_score=1, sell_threshold=4, evidence=[], orders=[
                {"type": "trailing_stop", "trailing_pct": 1.0}
            ],
            order_eligible=True,
            existing_trailing_orders=[{"type": "trailing_stop", "trailing_pct": 1.0}],
            suggested_trailing_pct=0.89,
            existing_trailing_pct_value=1.0,
        )
        self.assertEqual(action, "adjust_trailing_stop")
        self.assertTrue(review.trailing_offset_mismatch(1.0, 0.89))
        self.assertFalse(review.trailing_offset_mismatch(1.0, 0.95))

    def test_trailing_offset_notification_states_exact_change(self) -> None:
        row = {
            "ticker": "NVD",
            "action": "adjust_trailing_stop",
            "existing_trailing_pct": 1.0,
            "suggested_trailing_pct": 0.89,
        }
        self.assertEqual(
            review.notification_line(row),
            "NVD: CHANGE trailing stop from 1.00% to 0.89%",
        )

    def test_action_instruction_is_plain_language(self) -> None:
        self.assertEqual(
            review.action_instruction({
                "ticker": "CRK",
                "action": "consider_raise_stop",
                "current_stop": 13.06,
                "suggested_stop": 13.10,
            }),
            "CRK: RAISE protective stop from $13.06 to $13.10",
        )
        self.assertEqual(
            review.action_instruction({
                "ticker": "NVD",
                "action": "consider_initial_protective_stop",
                "suggested_stop": 100.0,
                "suggested_trailing_pct": 0.89,
            }),
            "NVD: SET protective stop at $100.00 (trailing offset 0.89%)",
        )

    def test_profitable_sell_exit_becomes_ready_review_ticket(self) -> None:
        row = {
            "ticker": "CRK",
            "action": "manual_exit_review",
            "shares": 4.0,
            "price": 13.12,
            "entry": 12.50,
        }
        self.assertIn(
            "CRK: SELL TICKET READY — sell 4 shares at market; current $13.12 > cost basis $12.50; confirm before submitting",
            review.action_instruction(row),
        )
        with TemporaryDirectory() as tmpdir:
            with mock.patch.object(review, "SELL_TICKET_DIR", Path(tmpdir)):
                path = review.sell_ticket_path(row, Path("/tmp/protective-review.md"))
            ticket = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(ticket["ticker"], "CRK")
        self.assertEqual(ticket["quantity"], 4.0)
        self.assertEqual(ticket["order_type"], "MARKET")
        self.assertEqual(ticket["order_payload"]["orderType"], "MARKET")
        self.assertEqual(ticket["order_payload"]["orderLegCollection"][0]["instruction"], "SELL")
        self.assertEqual(ticket["order_payload"]["orderLegCollection"][0]["quantity"], 4.0)
        self.assertTrue(ticket["confirmation_required"])
        self.assertFalse(ticket["execution_submitted"])
        self.assertFalse(ticket["submission_enabled"])

    def test_market_sell_submission_is_disabled_without_opt_in(self) -> None:
        order = market_schwab.build_market_sell_order("CRK", 4)
        with mock.patch.dict(review.os.environ, {"MARKET_SCHWAB_ENABLE_ORDER_SUBMISSION": "0"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "order submission is disabled"):
                market_schwab.submit_market_sell({}, "account-hash", order, confirmed=True)

    def test_market_sell_requires_confirmation(self) -> None:
        order = market_schwab.build_market_sell_order("CRK", 4)
        with mock.patch.dict(review.os.environ, {"MARKET_SCHWAB_ENABLE_ORDER_SUBMISSION": "1"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "explicit confirmation"):
                market_schwab.submit_market_sell({}, "account-hash", order)

    def test_loss_sell_exit_keeps_existing_wording(self) -> None:
        self.assertEqual(
            review.action_instruction({
                "ticker": "CRK",
                "action": "manual_exit_review",
                "shares": 4.0,
                "price": 11.90,
                "entry": 12.50,
            }),
            "CRK: SELL/EXIT — sell evidence is active; review the position now",
        )

    def test_action_instruction_can_say_keep_existing_protection(self) -> None:
        self.assertEqual(
            review.action_instruction({
                "ticker": "TQQQ",
                "action": "leave_existing_order",
                "existing_trailing_text": "2%",
            }),
            "TQQQ: KEEP existing protection (2%)",
        )

    def test_trailing_stop_protects_some_profit_on_green_position(self) -> None:
        feature = history.HistoryFeature(
            symbol="LOW",
            latest_close=220.0,
            sma5=221.0,
            sma20=216.0,
            close_vs_sma20_pct=2.0,
            trend_20d_pct=3.0,
            volatility_20d_pct=2.0,
            range_position_60d=0.7,
            bars=60,
        )

        stop, limit, trailing_pct, reasons = review.suggested_stop(
            entry=200.0,
            price=220.0,
            gain_pct=10.0,
            history=feature,
            ticker="LOW",
        )

        self.assertIsNotNone(stop)
        self.assertIsNotNone(limit)
        self.assertIsNotNone(trailing_pct)
        assert stop is not None
        assert limit is not None
        assert trailing_pct is not None
        self.assertGreater(stop, 200.0)
        self.assertLess(stop, 220.0)
        self.assertLess(limit, stop)
        self.assertAlmostEqual(trailing_pct, (220.0 - stop) / 220.0 * 100)
        self.assertTrue(any("protect most" in reason for reason in reasons))

    def test_pct_value_matches_compact_trailing_stop_format(self) -> None:
        self.assertEqual(review.pct_value(2.04), "2%")
        self.assertEqual(review.pct_value(1.25), "1%")
        self.assertEqual(review.pct_value(1.5), "2%")
        self.assertEqual(review.pct_value(0.8), "1%")
        self.assertEqual(review.pct_value(None), "n/a")

    def test_notification_summary_includes_trailing_stop_details(self) -> None:
        row = {
            "ticker": "LOW",
            "action": "consider_raise_stop",
            "current_stop": 217.50,
            "suggested_trailing_pct": 1.84,
            "suggested_stop": 220.12,
            "suggested_stop_limit": 219.02,
        }

        summary = review.notification_summary([row], [row], force=False)

        self.assertIn("LOW: RAISE protective stop from $217.50 to $220.12", summary)

    def test_leave_existing_order_is_not_a_reportable_change(self) -> None:
        row = {
            "ticker": "HLN",
            "action": "leave_existing_order",
            "suggested_stop": 9.64,
            "suggested_stop_limit": 9.60,
            "suggested_trailing_pct": 2.0,
            "current_stop": 9.57,
            "order_eligible": True,
            "order_eligibility": "whole Robinhood shares: 2",
            "price": 9.84,
        }
        state = {"schema": "test", "positions": {}}

        changes = review.state_changes([row], state)

        self.assertEqual(changes, [])
        self.assertEqual(state["positions"]["HLN"]["action"], "leave_existing_order")

    def test_forced_notification_omits_leave_existing_order(self) -> None:
        row = {
            "ticker": "HLN",
            "action": "leave_existing_order",
            "suggested_trailing_pct": 2.0,
            "suggested_stop": 9.64,
            "suggested_stop_limit": 9.60,
        }

        summary = review.notification_summary([row], [], force=True)

        self.assertNotIn("HLN", summary)
        self.assertNotIn("leave_existing_order", summary)

    def test_ineligible_position_keeps_manual_internal_action(self) -> None:
        action = review.classify_order_review(
            ticker="LOW",
            current_stop=None,
            suggested=220.86,
            sell_score=1,
            sell_threshold=4,
            evidence=[],
            orders=[],
            order_eligible=False,
        )

        self.assertEqual(action, "manual_fractional_protective_price")

    def test_fractional_notification_summary_uses_trailing_stop_limit(self) -> None:
        row = {
            "ticker": "LOW",
            "action": "manual_fractional_protective_price",
            "suggested_trailing_pct": 1.7,
            "suggested_stop": 220.86,
            "suggested_stop_limit": 219.76,
        }

        summary = review.notification_summary([row], [row], force=False)

        self.assertIn("LOW: SET protective stop at $220.86 (trailing offset 1.70%)", summary)

    def test_fractional_position_is_trailing_stop_limit_eligible_for_review(self) -> None:
        eligible, reason = review.trailing_stop_limit_order_eligible(
            {"shares": 0.5, "broker": "schwab", "brokers": ["schwab"]}
        )

        self.assertTrue(eligible)
        self.assertIn("trailing stop-limit percent review", reason)

    def test_whole_share_position_keeps_trailing_stop_action(self) -> None:
        action = review.classify_order_review(
            ticker="HLN",
            current_stop=None,
            suggested=9.58,
            sell_score=1,
            sell_threshold=4,
            evidence=[],
            orders=[],
            order_eligible=True,
        )

        self.assertEqual(action, "consider_initial_protective_stop")

    def test_notify_telegram_sends_report_path(self) -> None:
        row = {
            "ticker": "LOW",
            "action": "consider_raise_stop",
            "current_stop": 218.0,
            "suggested_trailing_pct": 2.0,
            "suggested_stop": 220.0,
            "suggested_stop_limit": 218.9,
        }
        completed = mock.Mock(returncode=0, stdout="sent", stderr="")

        with mock.patch.object(review, "NOTIFY_TELEGRAM", True), mock.patch.object(
            review, "TELEGRAM_TARGET", "123"
        ), mock.patch.object(review.subprocess, "run", return_value=completed) as run:
            status = review.notify_telegram([row], [row], Path("/tmp/report.md"), "opening", force=False)

        self.assertIn("telegram notification ok", status)
        args = run.call_args.args[0]
        self.assertEqual(args[:8], [review.OPENCLAW_CLI, "message", "send", "--channel", "telegram", "--account", review.TELEGRAM_ACCOUNT, "--target"])
        message = args[args.index("--message") + 1]
        self.assertIn("Protective stop review (opening)", message)
        self.assertIn("/tmp/report.md", message)

    def test_notify_telegram_skips_unchanged_forced_report(self) -> None:
        row = {
            "ticker": "ABC",
            "action": "consider_initial_protective_stop",
            "suggested_trailing_pct": 2.0,
            "suggested_stop_limit": 98.0,
        }
        with mock.patch.object(review.subprocess, "run") as run:
            status = review.notify_telegram([row], [], Path("/tmp/report.md"), "opening", force=True)
        self.assertEqual(status, "telegram notification skipped: no protective-stop notification changes")
        run.assert_not_called()

    def test_write_report_lists_outstanding_buy_orders(self) -> None:
        buy_order = {
            "symbol": "AMD",
            "side": "buy",
            "state": "confirmed",
            "type": "limit",
            "quantity": "1.000000",
            "price": "125.00",
            "account_label": "brokerage agentic",
            "id": "order-123",
        }

        with TemporaryDirectory() as tmpdir, mock.patch.object(review, "OUT_DIR", Path(tmpdir)):
            report, summary = review.write_report(
                rows=[],
                changes=[],
                order_errors=[],
                open_buy_orders=[buy_order],
                window="opening",
                force=True,
            )
            self.assertIsNotNone(report)
            assert report is not None
            text = report.read_text(encoding="utf-8")

        self.assertIn("## outstanding_buy_orders", text)
        self.assertIn("AMD: limit; state confirmed; qty 1.000000", text)
        self.assertIn("outstanding BUY orders=1", summary)


if __name__ == "__main__":
    unittest.main()
