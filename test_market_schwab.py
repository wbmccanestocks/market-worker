#!/usr/bin/env python3

import tempfile
import unittest
from unittest import mock

import market_schwab


class ParsePositionsTests(unittest.TestCase):
    def test_aggregates_long_positions_across_accounts(self) -> None:
        accounts = [
            {
                "securitiesAccount": {
                    "positions": [
                        {
                            "instrument": {"symbol": "AMD"},
                            "longQuantity": 2,
                            "shortQuantity": 0,
                            "averagePrice": 100,
                        }
                    ]
                }
            },
            {
                "securitiesAccount": {
                    "positions": [
                        {
                            "instrument": {"symbol": "AMD"},
                            "longQuantity": 3,
                            "shortQuantity": 0,
                            "averagePrice": 110,
                        },
                        {
                            "instrument": {"symbol": "TSLA"},
                            "longQuantity": 0,
                            "shortQuantity": 1,
                            "averagePrice": 200,
                        },
                    ]
                }
            },
        ]

        positions = market_schwab.parse_positions(accounts)

        self.assertEqual(set(positions), {"AMD"})
        self.assertEqual(positions["AMD"]["shares"], 5)
        self.assertEqual(positions["AMD"]["avg_price"], 106)
        self.assertEqual(positions["AMD"]["broker"], "schwab")


class TokenStatusTests(unittest.TestCase):
    def test_token_status_snapshot_excludes_secret_values(self) -> None:
        with mock.patch.object(
            market_schwab,
            "read_token",
            return_value={
                "access_token": "access-secret",
                "refresh_token": "refresh-secret",
                "expires_at": "2026-08-18T12:00:00+00:00",
            },
        ):
            snapshot = market_schwab.token_status_snapshot()

        self.assertTrue(snapshot["authorized"])
        self.assertTrue(snapshot["refresh_token_available"])
        self.assertEqual(snapshot["access_expires_at"], "2026-08-18T12:00:00+00:00")
        self.assertNotIn("access-secret", str(snapshot))
        self.assertNotIn("refresh-secret", str(snapshot))


class AuthNotificationTests(unittest.TestCase):
    def test_identifies_auth_token_errors(self) -> None:
        self.assertTrue(market_schwab.is_auth_token_error("Schwab token request failed: HTTP 400: invalid_grant"))
        self.assertTrue(market_schwab.is_auth_token_error("Schwab API request failed: HTTP 401: unauthorized"))
        self.assertFalse(market_schwab.is_auth_token_error("Schwab API request failed: HTTP 500: temporarily unavailable"))

    def test_notify_auth_token_error_sends_once_per_active_alert(self) -> None:
        completed = mock.Mock(returncode=0, stdout="sent", stderr="")
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.object(
            market_schwab, "AUTH_ALERT_STATE_FILE", market_schwab.Path(tmpdir) / "auth-alert.json"
        ), mock.patch.object(market_schwab, "TELEGRAM_TARGET", "123"), mock.patch.object(
            market_schwab, "NOTIFY_TELEGRAM", True
        ), mock.patch.object(
            market_schwab, "NOTIFY_DRY_RUN", True
        ), mock.patch.object(
            market_schwab,
            "read_token",
            return_value={
                "refresh_token": "refresh-secret",
                "expires_at": "2026-08-18T12:00:00+00:00",
            },
        ), mock.patch.object(
            market_schwab.subprocess, "run", return_value=completed
        ) as run:
            first = market_schwab.notify_auth_token_error("Schwab token request failed: HTTP 400: invalid_grant")
            second = market_schwab.notify_auth_token_error("Schwab token request failed: HTTP 400: invalid_grant")

        self.assertIn("telegram auth notification ok", first)
        self.assertEqual(second, "telegram auth notification skipped: duplicate active alert")
        run.assert_called_once()
        args = run.call_args.args[0]
        self.assertEqual(args[:8], [market_schwab.OPENCLAW_CLI, "message", "send", "--channel", "telegram", "--account", market_schwab.TELEGRAM_ACCOUNT, "--target"])
        self.assertIn("--dry-run", args)
        self.assertNotIn("refresh-secret", str(args))

    def test_clear_auth_token_alert_marks_state_cleared(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.object(
            market_schwab, "AUTH_ALERT_STATE_FILE", market_schwab.Path(tmpdir) / "auth-alert.json"
        ):
            market_schwab.write_public_json(
                market_schwab.AUTH_ALERT_STATE_FILE,
                {"active_alert_key": "abc", "notified_at": "2026-08-18T12:00:00+00:00"},
            )

            market_schwab.clear_auth_token_alert()
            state = market_schwab.read_json_file(market_schwab.AUTH_ALERT_STATE_FILE)

        self.assertIsNone(state["active_alert_key"])
        self.assertIn("cleared_at", state)


class ParseBalancesTests(unittest.TestCase):
    def test_extracts_sanitized_buying_power_without_account_number(self) -> None:
        accounts = [
            {
                "securitiesAccount": {
                    "accountNumber": "123456789",
                    "type": "MARGIN",
                    "isDayTrader": False,
                    "isIntradayMargin": True,
                    "projectedBalances": {
                        "stockBuyingPower": 500.25,
                        "buyingPower": 1000.5,
                        "availableFunds": 300.0,
                    },
                    "currentBalances": {
                        "cashBalance": 42.0,
                        "equity": 2500.0,
                    },
                    "initialBalances": {},
                }
            }
        ]

        balances = market_schwab.parse_balances(accounts)

        self.assertEqual(balances["account_count"], 1)
        self.assertEqual(balances["account_types"], ["MARGIN"])
        self.assertEqual(balances["max_stock_buying_power"], 500.25)
        self.assertEqual(balances["max_buying_power"], 1000.5)
        self.assertTrue(balances["has_margin_account"])
        self.assertNotIn("123456789", str(balances))
        self.assertTrue(str(balances["accounts"][0]["account_id"]).startswith("schwab-"))


if __name__ == "__main__":
    unittest.main()
