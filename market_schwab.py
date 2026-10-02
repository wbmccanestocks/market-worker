#!/usr/bin/env python3
"""Read-only Schwab OAuth client and market-worker position synchronizer."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import fcntl
import getpass
import gzip
import hashlib
import http.server
import json
import os
import secrets
import shlex
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get("MARKET_WORKER_DATA_DIR", str(ROOT.parent / "handoffs" / "market-worker")))
CONFIG_DIR = Path.home() / ".config" / "openclaw"
ENV_FILE = CONFIG_DIR / "schwab.env"
TOKEN_FILE = CONFIG_DIR / "schwab-token.json"
TOKEN_LOCK_FILE = CONFIG_DIR / "schwab-token.lock"
CERT_FILE = CONFIG_DIR / "schwab-localhost.crt"
KEY_FILE = CONFIG_DIR / "schwab-localhost.key"
POSITIONS_FILE = DATA_ROOT / "market-schwab-positions.json"
BALANCES_FILE = DATA_ROOT / "market-schwab-balances.json"
OPEN_ORDERS_FILE = DATA_ROOT / "market-schwab-open-orders.json"
TOKEN_STATUS_FILE = DATA_ROOT / "market-schwab-token-status.json"
WEEKEND_SNAPSHOT_FILE = DATA_ROOT / "market-schwab-weekend-snapshot.json"
AUTH_ALERT_STATE_FILE = Path(
    os.environ.get("MARKET_SCHWAB_AUTH_ALERT_STATE_FILE", str(DATA_ROOT / "market-schwab-auth-alert-state.json"))
)

AUTHORIZE_URL = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
TRADER_URL = "https://api.schwabapi.com/trader/v1"
MARKETDATA_URL = "https://api.schwabapi.com/marketdata/v1"
REFRESH_MARGIN_MINUTES = int(os.environ.get("SCHWAB_REFRESH_MARGIN_MINUTES", "5"))
OPENCLAW_CLI = os.environ.get("OPENCLAW_CLI", "/home/pi/.npm-global/bin/openclaw")
TELEGRAM_TARGET = os.environ.get(
    "MARKET_SCHWAB_TELEGRAM_TARGET", os.environ.get("MARKET_CONFIRM_TELEGRAM_TARGET", "8920769890")
)
TELEGRAM_ACCOUNT = os.environ.get("MARKET_TELEGRAM_ACCOUNT", "market_bot")


def env_bool(name: str, default: bool) -> bool:
    fallback = "1" if default else "0"
    return os.environ.get(name, fallback).lower() not in {"0", "false", "no"}


NOTIFY_TELEGRAM = env_bool("MARKET_SCHWAB_NOTIFY_TELEGRAM", True)
NOTIFY_DRY_RUN = env_bool("MARKET_SCHWAB_NOTIFY_DRY_RUN", False)


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def load_env(path: Path = ENV_FILE) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError(f"cannot read Schwab credentials: {path}: {exc}") from exc
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw = stripped.split("=", 1)
        key = key.strip()
        if not key.startswith("SCHWAB_"):
            continue
        try:
            parsed = shlex.split(raw, comments=True)
        except ValueError as exc:
            raise RuntimeError(f"invalid value for {key} in {path}: {exc}") from exc
        values[key] = parsed[0] if parsed else ""
    required = ("SCHWAB_APP_KEY", "SCHWAB_APP_SECRET", "SCHWAB_CALLBACK_URL")
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise RuntimeError(f"missing Schwab settings in {path}: {', '.join(missing)}")
    return values


def read_token() -> dict:
    try:
        return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read Schwab token file: {exc}") from exc


def write_private_json(path: Path, data: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    os.chmod(path, 0o600)


def write_public_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_json_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def is_auth_token_error(message: str) -> bool:
    lowered = message.lower()
    markers = (
        "not authorized",
        "access token is unavailable",
        "refresh token is unavailable",
        "invalid_grant",
        "refresh_token",
        "schwab token request failed",
        "expired",
        "unauthorized",
        "http 401",
    )
    return any(marker in lowered for marker in markers)


def notify_auth_token_error(message: str) -> str:
    if not NOTIFY_TELEGRAM:
        return "telegram auth notification disabled"
    if not TELEGRAM_TARGET:
        return "telegram auth notification skipped: MARKET_SCHWAB_TELEGRAM_TARGET is empty"

    snapshot = token_status_snapshot()
    key_source = json.dumps(
        {
            "message": message,
            "authorized": snapshot.get("authorized"),
            "access_expired": snapshot.get("access_expired"),
            "access_expires_at": snapshot.get("access_expires_at"),
            "refresh_token_available": snapshot.get("refresh_token_available"),
        },
        sort_keys=True,
    )
    alert_key = hashlib.sha256(key_source.encode("utf-8")).hexdigest()
    state = read_json_file(AUTH_ALERT_STATE_FILE)
    if state.get("active_alert_key") == alert_key and state.get("notified_at"):
        return "telegram auth notification skipped: duplicate active alert"

    message_text = (
        "Schwab authorization needs attention\n"
        f"Reason: {message[:700]}\n"
        f"Access expired: {snapshot.get('access_expired')}\n"
        f"Access expires at: {snapshot.get('access_expires_at') or 'unknown'}\n"
        "Reauth command: cd /home/pi/.openclaw/workspace/market-worker "
        "&& python3 market_schwab.py auth-manual"
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
        message_text,
    ]
    if NOTIFY_DRY_RUN:
        cmd.append("--dry-run")

    try:
        result = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=30, check=False)
    except Exception as exc:  # noqa: BLE001 - auth failure should still be reported to stderr.
        return f"telegram auth notification failed: {type(exc).__name__}: {exc}"

    output = (result.stdout + result.stderr).strip()
    if result.returncode == 0:
        write_public_json(
            AUTH_ALERT_STATE_FILE,
            {
                "schema": "openclaw.market_worker.schwab_auth_alert_state.v1",
                "active_alert_key": alert_key,
                "last_error": message[:1000],
                "notified_at": now_utc().isoformat(),
                "token_status": snapshot,
            },
        )
        return "telegram auth notification ok" + (f": {output[:300]}" if output else "")
    return f"telegram auth notification failed ({result.returncode}): {output[:500]}"


def clear_auth_token_alert() -> None:
    state = read_json_file(AUTH_ALERT_STATE_FILE)
    if not state.get("active_alert_key"):
        return
    state["active_alert_key"] = None
    state["cleared_at"] = now_utc().isoformat()
    write_public_json(AUTH_ALERT_STATE_FILE, state)


def basic_auth(config: dict[str, str]) -> str:
    raw = f"{config['SCHWAB_APP_KEY']}:{config['SCHWAB_APP_SECRET']}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def token_request(config: dict[str, str], fields: dict[str, str]) -> dict:
    request = urllib.request.Request(
        TOKEN_URL,
        data=urllib.parse.urlencode(fields).encode(),
        headers={
            "Authorization": basic_auth(config),
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            token = json.load(response)
    except urllib.error.HTTPError as exc:
        body_bytes = exc.read()
        if exc.headers.get("Content-Encoding", "").lower() == "gzip":
            try:
                body_bytes = gzip.decompress(body_bytes)
            except OSError:
                pass
        body = body_bytes.decode(errors="replace")[:1000]
        raise RuntimeError(f"Schwab token request failed: HTTP {exc.code}: {body}") from exc
    issued = now_utc()
    token["issued_at"] = issued.isoformat()
    token["expires_at"] = (issued + dt.timedelta(seconds=int(token.get("expires_in", 0)))).isoformat()
    return token


def refresh_access_token(config: dict[str, str], token: dict) -> dict:
    refresh_token = token.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("Schwab refresh token is unavailable; run `market_schwab.py auth`")
    fresh = token_request(config, {"grant_type": "refresh_token", "refresh_token": refresh_token})
    if not fresh.get("refresh_token"):
        fresh["refresh_token"] = refresh_token
    write_private_json(TOKEN_FILE, fresh)
    return fresh


def valid_access_token(config: dict[str, str]) -> str:
    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    with TOKEN_LOCK_FILE.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        token = read_token()
        if not token:
            raise RuntimeError("Schwab is not authorized; run `market_schwab.py auth`")
        try:
            expires_at = dt.datetime.fromisoformat(token["expires_at"])
        except (KeyError, TypeError, ValueError):
            expires_at = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        if now_utc() >= expires_at - dt.timedelta(minutes=REFRESH_MARGIN_MINUTES):
            token = refresh_access_token(config, token)
        access_token = token.get("access_token")
        if not access_token:
            raise RuntimeError("Schwab access token is unavailable; run `market_schwab.py auth`")
        return str(access_token)


def api_get(config: dict[str, str], url: str) -> object:
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {valid_access_token(config)}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:1000]
        raise RuntimeError(f"Schwab API request failed: HTTP {exc.code}: {body}") from exc


def ensure_local_certificate() -> None:
    if CERT_FILE.exists() and KEY_FILE.exists():
        return
    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "3650",
            "-keyout",
            str(KEY_FILE),
            "-out",
            str(CERT_FILE),
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1,DNS:localhost",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.chmod(CERT_FILE, 0o600)
    os.chmod(KEY_FILE, 0o600)


def launch_authorization_browser(config: dict[str, str], auth_url: str) -> None:
    """Open Schwab authorization in the configured local Chromium browser.

    A concrete browser command avoids relying on the desktop's generic URL
    handler, which may route OAuth into an unrelated browser/profile. The
    command is configurable for a dedicated market-worker profile when one is
    introduced; its final argument is always the generated authorization URL.
    """
    raw_command = config.get("SCHWAB_BROWSER_COMMAND", "chromium-browser")
    try:
        command = shlex.split(raw_command)
    except ValueError as exc:
        raise RuntimeError(f"invalid SCHWAB_BROWSER_COMMAND: {exc}") from exc
    if not command:
        raise RuntimeError("SCHWAB_BROWSER_COMMAND must name a browser executable")
    try:
        subprocess.Popen([*command, "--new-window", auth_url], start_new_session=True)
    except OSError as exc:
        raise RuntimeError(f"cannot launch Schwab authorization browser: {exc}") from exc


def authorize(config: dict[str, str], no_browser: bool = False) -> None:
    callback = urllib.parse.urlparse(config["SCHWAB_CALLBACK_URL"])
    if callback.scheme != "https" or callback.hostname not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("this connector requires a local HTTPS callback at 127.0.0.1 or localhost")
    ensure_local_certificate()
    state = secrets.token_urlsafe(24)
    params = {
        "client_id": config["SCHWAB_APP_KEY"],
        "redirect_uri": config["SCHWAB_CALLBACK_URL"],
        "response_type": "code",
        "state": state,
    }
    auth_url = AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)
    result: dict[str, str] = {}

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result["state"] = query.get("state", [""])[0]
            result["code"] = query.get("code", [""])[0]
            result["error"] = query.get("error", [""])[0]
            body = b"Schwab authorization received. You may close this tab."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    port = callback.port or 443
    server = http.server.HTTPServer((callback.hostname, port), CallbackHandler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(CERT_FILE, KEY_FILE)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.timeout = 300

    print("Authorize Schwab using this URL:")
    print(auth_url)
    print(f"\nWaiting up to 5 minutes for callback on {config['SCHWAB_CALLBACK_URL']} ...")
    if not no_browser:
        launch_authorization_browser(config, auth_url)
    server.handle_request()
    server.server_close()

    if result.get("error"):
        raise RuntimeError(f"Schwab authorization failed: {result['error']}")
    if result.get("state") != state or not result.get("code"):
        raise RuntimeError("Schwab authorization callback was missing or invalid")
    token = token_request(
        config,
        {
            "grant_type": "authorization_code",
            "code": result["code"],
            "redirect_uri": config["SCHWAB_CALLBACK_URL"],
        },
    )
    write_private_json(TOKEN_FILE, token)
    print("Schwab authorization saved.")


def authorize_manual(config: dict[str, str]) -> None:
    state = secrets.token_urlsafe(24)
    params = {
        "client_id": config["SCHWAB_APP_KEY"],
        "redirect_uri": config["SCHWAB_CALLBACK_URL"],
        "response_type": "code",
        "state": state,
    }
    print("Open this URL in your normal browser and approve Schwab access:")
    print(AUTHORIZE_URL + "?" + urllib.parse.urlencode(params))
    print("\nThe final 127.0.0.1 page may fail to load; that is expected.")
    redirected = getpass.getpass("Paste the complete final address-bar URL here (input hidden): ")
    query = urllib.parse.parse_qs(urllib.parse.urlparse(redirected.strip()).query)
    returned_state = query.get("state", [""])[0]
    code = query.get("code", [""])[0]
    error = query.get("error", [""])[0]
    if error:
        raise RuntimeError(f"Schwab authorization failed: {error}")
    if returned_state != state or not code:
        raise RuntimeError("Schwab authorization response was missing or invalid")
    token = token_request(
        config,
        {"grant_type": "authorization_code", "code": code, "redirect_uri": config["SCHWAB_CALLBACK_URL"]},
    )
    write_private_json(TOKEN_FILE, token)
    print("Schwab authorization saved.")


def parse_positions(accounts: object) -> dict:
    aggregated: dict[str, dict] = {}
    if not isinstance(accounts, list):
        return aggregated
    for account in accounts:
        securities = account.get("securitiesAccount", {}) if isinstance(account, dict) else {}
        for position in securities.get("positions", []) or []:
            instrument = position.get("instrument", {})
            symbol = str(instrument.get("symbol") or "").upper().strip()
            quantity = float(position.get("longQuantity") or 0) - float(position.get("shortQuantity") or 0)
            average_price = position.get("averagePrice")
            if not symbol or quantity <= 0:
                continue
            current = aggregated.setdefault(
                symbol,
                {"status": "open", "ticker": symbol, "shares": 0.0, "avg_price": None, "broker": "schwab"},
            )
            old_quantity = float(current["shares"])
            new_quantity = old_quantity + quantity
            if isinstance(average_price, (int, float)):
                if isinstance(current["avg_price"], (int, float)) and old_quantity:
                    current["avg_price"] = (
                        (float(current["avg_price"]) * old_quantity) + (float(average_price) * quantity)
                    ) / new_quantity
                else:
                    current["avg_price"] = float(average_price)
            current["shares"] = new_quantity
    return aggregated


def number(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").replace("$", "").strip())
        except ValueError:
            return None
    return None


def sanitized_account_id(account_number: object) -> str | None:
    text = str(account_number or "").strip()
    if not text:
        return None
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"schwab-{digest[:12]}"


def balance_value(*sections: dict, key: str) -> float | None:
    for section in sections:
        value = number(section.get(key))
        if value is not None:
            return value
    return None


def parse_balances(accounts: object) -> dict:
    rows: list[dict] = []
    if isinstance(accounts, list):
        for account in accounts:
            securities = account.get("securitiesAccount", {}) if isinstance(account, dict) else {}
            if not isinstance(securities, dict):
                continue
            current = securities.get("currentBalances") or {}
            projected = securities.get("projectedBalances") or {}
            initial = securities.get("initialBalances") or {}
            if not all(isinstance(section, dict) for section in (current, projected, initial)):
                continue
            rows.append(
                {
                    "account_id": sanitized_account_id(securities.get("accountNumber")),
                    "type": securities.get("type"),
                    "is_day_trader": securities.get("isDayTrader"),
                    "is_intraday_margin": securities.get("isIntradayMargin"),
                    "is_portfolio_margin": securities.get("isPortfolioMargin"),
                    "is_closing_only_restricted": securities.get("isClosingOnlyRestricted"),
                    "round_trips": securities.get("roundTrips"),
                    "buying_power": balance_value(projected, current, initial, key="buyingPower"),
                    "stock_buying_power": balance_value(projected, current, initial, key="stockBuyingPower"),
                    "available_funds": balance_value(projected, current, initial, key="availableFunds"),
                    "available_funds_non_marginable_trade": balance_value(
                        projected, current, initial, key="availableFundsNonMarginableTrade"
                    ),
                    "cash_available_for_trading": balance_value(projected, current, initial, key="cashAvailableForTrading"),
                    "day_trading_buying_power": balance_value(projected, current, initial, key="dayTradingBuyingPower"),
                    "intraday_buying_power": balance_value(projected, current, initial, key="intradayBuyingPowerAmount"),
                    "cash_balance": balance_value(current, initial, key="cashBalance"),
                    "liquidation_value": balance_value(current, initial, key="liquidationValue"),
                    "equity": balance_value(current, initial, key="equity"),
                    "is_in_call": projected.get("isInCall", initial.get("isInCall")),
                    "reg_t_call": balance_value(projected, current, initial, key="regTCall"),
                    "maintenance_call": balance_value(projected, current, initial, key="maintenanceCall"),
                }
            )

    def values_for(key: str) -> list[float]:
        return [value for row in rows if (value := number(row.get(key))) is not None]

    return {
        "account_count": len(rows),
        "account_types": sorted({str(row.get("type")) for row in rows if row.get("type")}),
        "accounts": rows,
        "max_buying_power": max(values_for("buying_power"), default=None),
        "max_stock_buying_power": max(values_for("stock_buying_power"), default=None),
        "max_available_funds": max(values_for("available_funds"), default=None),
        "max_available_funds_non_marginable_trade": max(values_for("available_funds_non_marginable_trade"), default=None),
        "max_cash_available_for_trading": max(values_for("cash_available_for_trading"), default=None),
        "max_day_trading_buying_power": max(values_for("day_trading_buying_power"), default=None),
        "max_intraday_buying_power": max(values_for("intraday_buying_power"), default=None),
        "total_cash_balance": sum(values_for("cash_balance")) if values_for("cash_balance") else None,
        "total_equity": sum(values_for("equity")) if values_for("equity") else None,
        "has_margin_account": any(str(row.get("type") or "").upper() == "MARGIN" for row in rows),
        "has_restriction": any(
            bool(row.get("is_closing_only_restricted")) or bool(row.get("is_in_call")) for row in rows
        ),
    }


def write_balance_snapshot(accounts: object) -> dict:
    snapshot = {
        "schema": "openclaw.market_worker.schwab_balances.v1",
        "generated_at": now_utc().isoformat(),
        **parse_balances(accounts),
    }
    write_private_json(BALANCES_FILE, snapshot)
    return snapshot


def sync_account_snapshot(config: dict[str, str]) -> tuple[dict, dict]:
    accounts = api_get(config, f"{TRADER_URL}/accounts?fields=positions")
    positions = parse_positions(accounts)
    positions_snapshot = {
        "schema": "openclaw.market_worker.schwab_positions.v1",
        "generated_at": now_utc().isoformat(),
        "positions": positions,
    }
    write_private_json(POSITIONS_FILE, positions_snapshot)
    return positions_snapshot, write_balance_snapshot(accounts)


def sync_positions(config: dict[str, str]) -> dict:
    positions_snapshot, _balances_snapshot = sync_account_snapshot(config)
    return positions_snapshot


TERMINAL_ORDER_STATUSES = {"filled", "canceled", "rejected", "replaced", "expired"}


def normalized_order_status(value: object) -> str:
    return str(value or "unknown").strip().lower().replace(" ", "_")


def trailing_offset_fields(order: dict) -> tuple[float | None, str | None]:
    """Return a Schwab trailing offset and its unit without losing broker data."""
    raw_offset = order.get("stopPriceOffset")
    if raw_offset is None:
        raw_offset = order.get("stopPriceOffsetPercentage")
    offset = number(raw_offset)
    raw_kind = (
        order.get("stopPriceLinkType")
        or order.get("stopPriceOffsetType")
        or order.get("trailingOffsetType")
    )
    kind = str(raw_kind).strip().lower() if raw_kind is not None else None
    if kind and "percent" in kind:
        kind = "percent"
    elif kind and ("value" in kind or "dollar" in kind or kind == "amount"):
        kind = "value"
    elif order.get("stopPriceOffsetPercentage") is not None:
        kind = "percent"
    return offset, kind


def normalize_open_orders(payload: object, account_label: str = "schwab") -> list[dict]:
    """Reduce Schwab order responses to non-sensitive fields needed for stop review."""
    source_rows = payload if isinstance(payload, list) else []
    normalized: list[dict] = []
    for order in source_rows:
        if not isinstance(order, dict):
            continue
        status = normalized_order_status(order.get("status"))
        if status in TERMINAL_ORDER_STATUSES:
            continue
        legs = order.get("orderLegCollection") or []
        if not isinstance(legs, list):
            legs = []
        for leg in legs:
            if not isinstance(leg, dict):
                continue
            instrument = leg.get("instrument") or {}
            symbol = str(instrument.get("symbol") or "").upper().strip() if isinstance(instrument, dict) else ""
            instruction = str(leg.get("instruction") or "").upper().strip()
            if not symbol or not instruction:
                continue
            trailing_offset, trailing_offset_type = trailing_offset_fields(order)
            normalized.append(
                {
                    "id": str(order.get("orderId") or "")[:80] or None,
                    "symbol": symbol,
                    "side": "sell" if instruction.startswith("SELL") else "buy" if instruction.startswith("BUY") else instruction.lower(),
                    "state": status,
                    "type": str(order.get("orderType") or "unknown").lower(),
                    "trigger": "stop" if number(order.get("stopPrice")) is not None else None,
                    "quantity": number(leg.get("quantity") or order.get("quantity")),
                    "cumulative_quantity": number(order.get("filledQuantity")),
                    "price": number(order.get("price")),
                    "stop_price": number(order.get("stopPrice")),
                    "trailing_offset": trailing_offset,
                    "trailing_offset_type": trailing_offset_type,
                    "trailing_pct": trailing_offset if trailing_offset_type == "percent" else None,
                    "entered_at": order.get("enteredTime"),
                    "account_label": sanitized_account_id(order.get("accountNumber")) or account_label,
                }
            )
    return normalized


def sync_open_orders(config: dict[str, str]) -> dict:
    """Fetch linked-account orders read-only and retain only currently open rows."""
    try:
        linked = api_get(config, f"{TRADER_URL}/accounts/accountNumbers")
        if not isinstance(linked, list):
            raise RuntimeError("Schwab account-number lookup returned an unexpected response")
        end = now_utc() + dt.timedelta(days=1)
        start = now_utc() - dt.timedelta(days=59)
        params = urllib.parse.urlencode(
            {
                "fromEnteredTime": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "toEnteredTime": end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            }
        )
        orders: list[dict] = []
        for account in linked:
            if not isinstance(account, dict):
                continue
            account_hash = str(account.get("hashValue") or "").strip()
            if not account_hash:
                continue
            payload = api_get(config, f"{TRADER_URL}/accounts/{account_hash}/orders?{params}")
            orders.extend(normalize_open_orders(payload, sanitized_account_id(account.get("accountNumber")) or "schwab"))
        snapshot = {
            "schema": "openclaw.market_worker.schwab_open_orders.v1",
            "generated_at": now_utc().isoformat(),
            "status": "ok",
            "orders": orders,
        }
    except RuntimeError as exc:
        snapshot = {
            "schema": "openclaw.market_worker.schwab_open_orders.v1",
            "generated_at": now_utc().isoformat(),
            "status": "error",
            "orders": [],
            "error": str(exc)[:1000],
        }
    write_private_json(OPEN_ORDERS_FILE, snapshot)
    return snapshot


def fetch_quotes(config: dict[str, str], symbols: list[str]) -> dict:
    normalized = sorted({symbol.upper().strip() for symbol in symbols if symbol.strip()})
    if not normalized:
        return {}
    out: dict[str, object] = {}
    for start in range(0, len(normalized), 50):
        chunk = normalized[start : start + 50]
        data = api_get(
            config,
            f"{MARKETDATA_URL}/quotes?" + urllib.parse.urlencode({"symbols": ",".join(chunk)}),
        )
        if isinstance(data, dict):
            out.update(data)
    return out


def fetch_price_history(config: dict[str, str], symbol: str, days: int = 120) -> dict:
    params = {
        "symbol": symbol.upper().strip(),
        "periodType": "year" if days > 180 else "month",
        "period": "1" if days > 180 else "6",
        "frequencyType": "daily",
        "frequency": "1",
        "needExtendedHoursData": "false",
        "needPreviousClose": "true",
    }
    return api_get(config, f"{MARKETDATA_URL}/pricehistory?" + urllib.parse.urlencode(params))


def fetch_intraday_history(config: dict[str, str], symbol: str, days: int = 30, frequency: int = 5) -> dict:
    """Return regular-session minute candles for a bounded historical range."""
    if frequency not in {1, 5, 10, 15, 30}:
        raise ValueError("Schwab intraday frequency must be 1, 5, 10, 15, or 30 minutes")
    end_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    start_ms = end_ms - max(1, min(days, 30)) * 86_400_000
    params = {
        "symbol": symbol.upper().strip(),
        "startDate": str(start_ms),
        "endDate": str(end_ms),
        "frequencyType": "minute",
        "frequency": str(frequency),
        "needExtendedHoursData": "false",
    }
    return api_get(config, f"{MARKETDATA_URL}/pricehistory?" + urllib.parse.urlencode(params))


def token_status_snapshot() -> dict:
    token = read_token()
    expires_at_raw = token.get("expires_at")
    try:
        expires_at = dt.datetime.fromisoformat(expires_at_raw) if expires_at_raw else None
    except (TypeError, ValueError):
        expires_at = None
    return {
        "schema": "openclaw.market_worker.schwab_token_status.v1",
        "generated_at": now_utc().isoformat(),
        "authorized": bool(token.get("refresh_token")),
        "access_expired": expires_at is None or now_utc() >= expires_at,
        "access_expires_at": expires_at_raw,
        "refresh_token_available": bool(token.get("refresh_token")),
        "token_file": str(TOKEN_FILE),
    }


def keepalive(config: dict[str, str]) -> dict:
    valid_access_token(config)
    snapshot = token_status_snapshot()
    write_private_json(TOKEN_STATUS_FILE, snapshot)
    return snapshot


def market_prep_snapshot(config: dict[str, str]) -> dict:
    positions_snapshot, balances_snapshot = sync_account_snapshot(config)
    position_symbols = sorted(positions_snapshot.get("positions", {}))
    quote_symbols = sorted(set(position_symbols + ["DIA", "IWM", "QQQ", "SPY", "VTI"]))
    quotes = api_get(
        config,
        f"{MARKETDATA_URL}/quotes?" + urllib.parse.urlencode({"symbols": ",".join(quote_symbols)}),
    )
    snapshot = {
        "schema": "openclaw.market_worker.schwab_market_prep.v1",
        "generated_at": now_utc().isoformat(),
        "balances": balances_snapshot,
        "positions": positions_snapshot.get("positions", {}),
        "quote_symbols": quote_symbols,
        "quotes": quotes,
    }
    write_private_json(WEEKEND_SNAPSHOT_FILE, snapshot)
    return snapshot


def print_status(config: dict[str, str]) -> None:
    status = token_status_snapshot()
    status["configured"] = True
    status["positions_file"] = str(POSITIONS_FILE)
    status["balances_file"] = str(BALANCES_FILE)
    status["open_orders_file"] = str(OPEN_ORDERS_FILE)
    status["token_status_file"] = str(TOKEN_STATUS_FILE)
    status["weekend_snapshot_file"] = str(WEEKEND_SNAPSHOT_FILE)
    print(json.dumps(status, indent=2, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    auth = sub.add_parser("auth", help="complete one-time Schwab browser authorization")
    auth.add_argument("--no-browser", action="store_true")
    sub.add_parser("auth-manual", help="authorize from a browser on another computer")
    sub.add_parser("status", help="show configuration and token status")
    sub.add_parser("keepalive", help="refresh Schwab access token if needed and write non-secret token status")
    sub.add_parser("market-prep", help="refresh Schwab token, positions, and Monday-useful benchmark quotes")
    sub.add_parser("sync", help="refresh the read-only Schwab position snapshot")
    sub.add_parser("orders", help="refresh and print the read-only Schwab open-order snapshot")
    quote = sub.add_parser("quotes", help="fetch read-only quotes")
    quote.add_argument("symbols", nargs="+")
    args = parser.parse_args()

    try:
        config = load_env()
        if args.command == "auth":
            authorize(config, no_browser=args.no_browser)
            clear_auth_token_alert()
        elif args.command == "auth-manual":
            authorize_manual(config)
            clear_auth_token_alert()
        elif args.command == "status":
            print_status(config)
        elif args.command == "keepalive":
            snapshot = keepalive(config)
            clear_auth_token_alert()
            print(f"Schwab token ok; access expires at {snapshot.get('access_expires_at')}")
        elif args.command == "market-prep":
            snapshot = market_prep_snapshot(config)
            clear_auth_token_alert()
            print(
                "Schwab market prep saved "
                f"{len(snapshot['positions'])} positions and {len(snapshot['quote_symbols'])} quote symbols"
            )
        elif args.command == "sync":
            positions_snapshot, balances_snapshot = sync_account_snapshot(config)
            orders_snapshot = sync_open_orders(config)
            clear_auth_token_alert()
            buying_power = balances_snapshot.get("max_stock_buying_power") or balances_snapshot.get("max_buying_power")
            suffix = f"; max stock/buying power {buying_power:.2f}" if isinstance(buying_power, (int, float)) else ""
            order_suffix = (
                f"; {len(orders_snapshot['orders'])} open order(s)"
                if orders_snapshot.get("status") == "ok"
                else "; open-order snapshot unavailable"
            )
            print(f"synced {len(positions_snapshot['positions'])} Schwab positions and {balances_snapshot['account_count']} balance account(s){suffix}{order_suffix}")
        elif args.command == "orders":
            snapshot = sync_open_orders(config)
            clear_auth_token_alert()
            print(json.dumps(snapshot, indent=2, sort_keys=True))
        elif args.command == "quotes":
            data = fetch_quotes(config, args.symbols)
            clear_auth_token_alert()
            print(json.dumps(data, indent=2, sort_keys=True))
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if is_auth_token_error(str(exc)):
            print(notify_auth_token_error(str(exc)), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
