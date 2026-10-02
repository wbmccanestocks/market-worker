#!/usr/bin/env python3
"""Deterministic market-worker packet generator for cron.

This script intentionally does not give trade advice. It fetches configured
public starting pages, extracts compact source snippets around watchlist terms,
optionally asks local Ollama to compress the evidence, and writes both a
structured JSON packet for automation and a Markdown handoff packet for review.
"""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path
from zoneinfo import ZoneInfo

import market_rss_listener


try:
    from lxml import html as lxml_html
except Exception:  # noqa: BLE001 - lxml is an optional cleanup dependency.
    lxml_html = None


ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get("MARKET_WORKER_DATA_DIR", str(ROOT.parent / "handoffs" / "market-worker")))
SOURCE_LIST = Path(os.environ.get("MARKET_WORKER_SOURCE_LIST", str(ROOT / "market-source-starting-points.md")))
PACKET_GLOB = "*-market-packet-*.md"
EMAIL_INBOX = Path(os.environ.get("MARKET_WORKER_EMAIL_INBOX", str(DATA_ROOT / "email-inbox")))
EMAIL_CORPUS_STATE = Path(os.environ.get("MARKET_WORKER_EMAIL_STATE", str(DATA_ROOT / "market-worker-email-corpus-state.json")))
ARTICLE_FETCH_STATE = Path(os.environ.get("MARKET_WORKER_ARTICLE_FETCH_STATE", str(DATA_ROOT / "market-article-fetch-state.json")))
EMAIL_MAX_PER_RUN = int(os.environ.get("MARKET_WORKER_EMAIL_MAX_PER_RUN", "8"))
ARTICLE_MAX_LINKS_PER_SOURCE = int(os.environ.get("MARKET_WORKER_ARTICLE_MAX_LINKS_PER_SOURCE", "4"))
ARTICLE_MAX_LINKS_PER_RUN = int(os.environ.get("MARKET_WORKER_ARTICLE_MAX_LINKS_PER_RUN", "12"))
PAYWALL_RETRY_DAYS = int(os.environ.get("MARKET_WORKER_PAYWALL_RETRY_DAYS", "7"))
EMAIL_BACKFILL = os.environ.get("MARKET_WORKER_EMAIL_BACKFILL", "0").lower() in {"1", "true", "yes"}
RSS_CONFIG = Path(os.environ.get("MARKET_WORKER_RSS_CONFIG", str(DATA_ROOT / "market-rss-feeds.json")))
RSS_STATE = Path(os.environ.get("MARKET_WORKER_RSS_STATE", str(DATA_ROOT / "market-rss-listener-state.json")))
RSS_BACKFILL = os.environ.get("MARKET_WORKER_RSS_BACKFILL", "0").lower() in {"1", "true", "yes"}
RSS_HANDOFF = Path(os.environ.get("MARKET_WORKER_RSS_HANDOFF", str(DATA_ROOT / "market-rss-handoff.jsonl")))
ARTICLE_HANDOFF = Path(os.environ.get("MARKET_WORKER_ARTICLE_HANDOFF", str(DATA_ROOT / "market-article-handoff.jsonl")))
ARTICLE_HANDOFF_CLAIM: Path | None = None
# Robinhood is retired.  Keep legacy helpers permanently unreachable even if an
# old environment variable is present.
ROBINHOOD_ENABLED = False
ROBINHOOD_CLIENT = Path(
    os.environ.get(
        "MARKET_WORKER_ROBINHOOD_CLIENT",
        str(ROOT.parent / "skills" / "robinhood-agentic" / "rh-client.mjs"),
    )
)
ROBINHOOD_TIMEOUT = int(os.environ.get("MARKET_WORKER_ROBINHOOD_TIMEOUT", "30"))
ROBINHOOD_MAX_SYMBOLS = int(os.environ.get("MARKET_WORKER_ROBINHOOD_MAX_SYMBOLS", "80"))
ROBINHOOD_SECTOR_LISTS = {
    name.strip().casefold()
    for name in os.environ.get("MARKET_WORKER_SECTOR_LISTS", "Pharma").split(",")
    if name.strip()
}
ROBINHOOD_POSITIONS_FILE = DATA_ROOT / "market-robinhood-positions.json"
LIQUIDITY_CACHE_FILE = DATA_ROOT / "market-liquidity-cache.json"
LIQUIDITY_ENABLED = os.environ.get("MARKET_WORKER_LIQUIDITY_FILTER", "1").lower() in {"1", "true", "yes"}
LIQUIDITY_MIN_PRICE = float(os.environ.get("MARKET_WORKER_MIN_PRICE", "2"))
LIQUIDITY_MIN_MARKET_CAP = float(os.environ.get("MARKET_WORKER_MIN_MARKET_CAP", "50000000"))
LIQUIDITY_MIN_VOLUME = int(os.environ.get("MARKET_WORKER_MIN_VOLUME", "100000"))
LIQUIDITY_MIN_DOLLAR_VOLUME = float(os.environ.get("MARKET_WORKER_MIN_DOLLAR_VOLUME", "1000000"))
NASDAQ_SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&offset=0&download=true"
TZ = ZoneInfo("America/New_York")
LUNA_MODEL = os.environ.get("MARKET_WORKER_LUNA_MODEL", "gpt-5.6-luna")
TERRA_MODEL = os.environ.get("MARKET_WORKER_TERRA_MODEL", "gpt-5.6-terra")
CODEX_TIMEOUT = int(os.environ.get("MARKET_WORKER_CODEX_TIMEOUT", "300"))
CODEX_ENABLED = os.environ.get("MARKET_WORKER_CODEX", "1").lower() in {"1", "true", "yes"}
LUNA_FILTER_ENABLED = os.environ.get("MARKET_WORKER_LUNA_FILTER", "1").lower() in {"1", "true", "yes"}
WATCHLIST = {
    "NVDA", "MSFT", "AAPL", "AMD", "AVGO", "GOOGL", "GOOG", "META", "TSLA",
    "AMZN", "LOW", "F", "FB", "QQQ", "SPY", "IWM", "XLK", "SMH", "XOM",
    "CVX", "COP", "OXY", "SLB", "LNG", "ENPH", "FSLR", "XLE", "XOP", "USO",
    "SOFI", "IONQ", "RIVN", "LCID", "PFE", "WBD", "SNAP", "OPEN", "TALK",
    "NVDL", "TSLL", "TSLS", "TSLZ", "SPCX", "LOFF", "LOFD", "YINN", "SQQQ", "SOXS", "LABD", "TZA",
}
TICKER_STOPWORDS = {
    "AI", "API", "BMO", "CEO", "CFO", "CT", "ETF", "ET", "EU", "FDA",
    "FOR", "GDP", "IPO", "LLC", "MSA", "OTC", "SEC", "TE", "USA", "USD", "WHO",
}
CATALYST_WORDS = {
    "earnings", "guidance", "revenue", "profit", "forecast", "analyst",
    "upgrade", "downgrade", "rating", "price target", "oil", "gas", "opec",
    "inventory", "fed", "inflation", "yield", "tariff", "chips", "ai",
    "semiconductor", "solar", "energy", "data center", "fda", "pdufa",
    "clinical trial", "phase 1", "phase 2", "phase 3", "endpoint", "study",
    "approval", "approved", "drug", "biotech", "pharma", "offering",
    "acquisition", "merger",
}
NOISE_PHRASES = {
    "advertise contribute",
    "analyst ratings calendar",
    "browse stocks stocks a-z",
    "calendars analyst ratings calendar",
    "celebrity tv movies music",
    "center personal finance back personal finance",
    "compare credit cards",
    "copyright ©",
    "data on the markets market data center",
    "finance my portfolio news latest originals",
    "financial adviser center newswires tools market data center",
    "free benzinga pro trial",
    "getting started how to buy stocks",
    "highest dividend small cap large cap",
    "industries automotive banking internet & online services",
    "learn & grow more education",
    "market data & tools back market data & tools",
    "my portfolio news latest originals premium news stock market",
    "nvda vs intc nvda vs amd goog vs amzn aapl vs msft",
    "oops, something went wrong skip to navigation",
    "personal finance back personal finance",
    "privacy dashboard",
    "real estate utilities treasury bonds futures currencies",
    "selected edition us english",
    "stocks tools free benzinga pro trial",
    "stocks most active day gainers day losers",
    "terms and privacy policy",
    "trusts futures american depository receipt",
}
DROP_TAGS = {
    "aside", "button", "canvas", "dialog", "fieldset", "footer", "form",
    "header", "iframe", "input", "label", "nav", "noscript", "script",
    "select", "style", "svg", "textarea",
}
DROP_ATTR_RE = re.compile(
    r"(?:^|[-_\\s])("
    r"ad|ads|advert|banner|carousel|cookie|footer|header|login|marketplace|"
    r"menu|modal|nav|newsletter|paywall|promo|recommend|related|share|"
    r"sidebar|signin|signup|social|sponsor|subscribe|trending|video"
    r")(?:$|[-_\\s])",
    re.I,
)
DROP_ROLE_VALUES = {"banner", "complementary", "contentinfo", "navigation", "search"}
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 OpenClawMarketWorker/1.0"
)


def robinhood_call(tool_name: str, args: dict) -> dict:
    command = ["node", str(ROBINHOOD_CLIENT), "call", tool_name, json.dumps(args)]
    completed = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=ROBINHOOD_TIMEOUT,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
        raise RuntimeError(f"{tool_name} failed: {detail[:300]}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{tool_name} returned invalid JSON: {completed.stdout[:200]}") from exc


def rotating_symbols(symbols: set[str], limit: int, time_slot: int, salt: str) -> list[str]:
    ordered = sorted(symbols)
    if limit <= 0 or not ordered:
        return []
    if len(ordered) <= limit:
        return ordered
    salt_offset = int(hashlib.sha256(salt.encode()).hexdigest()[:8], 16)
    offset = ((time_slot * limit) + salt_offset) % len(ordered)
    return [ordered[(offset + index) % len(ordered)] for index in range(limit)]


def numeric(value: object) -> float | None:
    try:
        parsed = float(str(value).replace("$", "").replace(",", ""))
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def read_liquidity_cache() -> dict:
    try:
        data = json.loads(LIQUIDITY_CACHE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data.get("rows"), dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def fetch_nasdaq_liquidity(now: dt.datetime) -> dict:
    req = urllib.request.Request(
        NASDAQ_SCREENER_URL,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read(4_000_000).decode("utf-8", errors="replace"))
    rows = {}
    for row in ((payload.get("data") or {}).get("rows") or []):
        symbol = str(row.get("symbol") or "").upper().strip()
        if not re.fullmatch(r"[A-Z0-9.-]{1,15}", symbol):
            continue
        rows[symbol] = {
            "price": numeric(row.get("lastsale")),
            "volume": numeric(row.get("volume")),
            "market_cap": numeric(row.get("marketCap")),
        }
    return {
        "source": NASDAQ_SCREENER_URL,
        "fetched_at": now.isoformat(),
        "market_date": now.date().isoformat(),
        "volume_complete": now.time() >= dt.time(16, 0),
        "rows": rows,
    }


def liquidity_snapshot(now: dt.datetime) -> tuple[dict, list[str]]:
    if not LIQUIDITY_ENABLED:
        return {}, []
    cached = read_liquidity_cache()
    cached_date = str(cached.get("market_date") or "")
    cached_complete = bool(cached.get("volume_complete"))
    should_refresh = not cached
    if now.time() >= dt.time(16, 0) and (cached_date != now.date().isoformat() or not cached_complete):
        should_refresh = True
    if not should_refresh:
        return cached, []
    try:
        snapshot = fetch_nasdaq_liquidity(now)
        tmp = LIQUIDITY_CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(snapshot, separators=(",", ":")) + "\n", encoding="utf-8")
        tmp.replace(LIQUIDITY_CACHE_FILE)
        return snapshot, []
    except Exception as exc:  # noqa: BLE001 - filtering should fail open.
        if cached:
            return cached, [f"Nasdaq liquidity refresh failed; using cache: {type(exc).__name__}: {exc}"]
        return {}, [f"Nasdaq liquidity filter unavailable: {type(exc).__name__}: {exc}"]


def filter_curated_symbols(
    symbols: set[str],
    snapshot: dict,
    liquidity_exempt_symbols: set[str] | None = None,
) -> tuple[set[str], dict]:
    rows = snapshot.get("rows") or {}
    volume_complete = bool(snapshot.get("volume_complete"))
    liquidity_exempt_symbols = liquidity_exempt_symbols or set()
    eligible = set()
    reason_counts: dict[str, int] = {}
    excluded_sample = []
    for symbol in sorted(symbols):
        if symbol in liquidity_exempt_symbols:
            eligible.add(symbol)
            continue
        row = rows.get(symbol)
        if not isinstance(row, dict):
            eligible.add(symbol)
            continue
        price = numeric(row.get("price"))
        volume = numeric(row.get("volume"))
        market_cap = numeric(row.get("market_cap"))
        reasons = []
        if price is not None and price < LIQUIDITY_MIN_PRICE:
            reasons.append("low_price")
        if market_cap is not None and market_cap < LIQUIDITY_MIN_MARKET_CAP:
            reasons.append("low_market_cap")
        if volume_complete and volume is not None and volume < LIQUIDITY_MIN_VOLUME:
            reasons.append("low_volume")
        if (
            volume_complete
            and price is not None
            and volume is not None
            and price * volume < LIQUIDITY_MIN_DOLLAR_VOLUME
        ):
            reasons.append("low_dollar_volume")
        if not reasons:
            eligible.add(symbol)
            continue
        for reason in reasons:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        if len(excluded_sample) < 20:
            excluded_sample.append({"symbol": symbol, "reasons": reasons})
    return eligible, {
        "enabled": LIQUIDITY_ENABLED,
        "source": snapshot.get("source") or "unavailable",
        "snapshot_at": snapshot.get("fetched_at"),
        "volume_complete": volume_complete,
        "thresholds": {
            "min_price": LIQUIDITY_MIN_PRICE,
            "min_market_cap": LIQUIDITY_MIN_MARKET_CAP,
            "min_volume": LIQUIDITY_MIN_VOLUME,
            "min_dollar_volume": LIQUIDITY_MIN_DOLLAR_VOLUME,
        },
        "candidate_count": len(symbols),
        "liquidity_exempt_count": len(symbols & liquidity_exempt_symbols),
        "eligible_count": len(eligible),
        "excluded_count": len(symbols - eligible),
        "excluded_reason_counts": reason_counts,
        "excluded_sample": excluded_sample,
    }


def robinhood_watchlist_context(now: dt.datetime | None = None) -> tuple[dict, list[str]]:
    now = now or dt.datetime.now(TZ)
    context = {
        "enabled": ROBINHOOD_ENABLED,
        "status": "disabled" if not ROBINHOOD_ENABLED else "unavailable",
        "client": str(ROBINHOOD_CLIENT),
        "tools_used": [],
        "lists": [],
        "symbols": [],
        "held_symbols": [],
        "excluded_sector_symbols": [],
        "candidate_symbol_count": 0,
        "selection_policy": "held positions, then custom lists, then non-sector curated lists; sector watchlists excluded because sector-news pages drive discovery",
        "liquidity_filter": {},
    }
    if not ROBINHOOD_ENABLED:
        return context, []
    if not ROBINHOOD_CLIENT.exists():
        return context, [f"Robinhood context unavailable: missing client {ROBINHOOD_CLIENT}"]

    try:
        watchlist_response = robinhood_call("get_watchlists", {})
        context["tools_used"].append("get_watchlists")
        watchlists = ((watchlist_response.get("data") or {}).get("watchlists") or [])
        list_candidates: list[dict] = []
        gaps: list[str] = []
        for watchlist in watchlists:
            list_id = str(watchlist.get("id") or "")
            if not list_id:
                continue
            allowed_types = set(watchlist.get("allowed_object_types") or [])
            if allowed_types and allowed_types <= {"option_strategy"}:
                continue
            try:
                item_response = robinhood_call("get_watchlist_items", {"list_id": list_id})
            except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
                gaps.append(
                    f"Robinhood watchlist {watchlist.get('display_name') or list_id} unavailable: "
                    f"{type(exc).__name__}: {exc}"
                )
                continue
            context["tools_used"].append("get_watchlist_items")
            items = ((item_response.get("data") or {}).get("items") or [])
            list_symbols = []
            for item in items:
                if item.get("object_type") not in {"instrument", "currency_pair"}:
                    continue
                symbol = str(item.get("symbol") or "").upper().strip()
                if not re.fullmatch(r"[A-Z0-9.-]{1,15}", symbol):
                    continue
                list_symbols.append(symbol)
            list_candidates.append(
                {
                    "display_name": str(watchlist.get("display_name") or "(unnamed)")[:100],
                    "owner_type": str(watchlist.get("owner_type") or "unknown"),
                    "is_sector": str(watchlist.get("display_name") or "").strip().casefold() in ROBINHOOD_SECTOR_LISTS,
                    "candidate_symbols": set(list_symbols),
                }
            )
        held_symbols: set[str] = set()
        if ROBINHOOD_POSITIONS_FILE.exists():
            try:
                position_state = json.loads(ROBINHOOD_POSITIONS_FILE.read_text(encoding="utf-8"))
                held_symbols = {
                    str(ticker).upper()
                    for ticker, details in (position_state.get("positions") or {}).items()
                    if isinstance(details, dict) and details.get("status") == "open"
                }
                context["held_symbols"] = sorted(held_symbols)
            except (OSError, json.JSONDecodeError):
                gaps.append("Robinhood held-position snapshot unreadable; using watchlists only")

        custom_symbols = {
            symbol
            for watchlist in list_candidates
            if watchlist["owner_type"] == "custom" and not watchlist["is_sector"]
            for symbol in watchlist["candidate_symbols"]
        }
        curated_symbols = {
            symbol
            for watchlist in list_candidates
            if watchlist["owner_type"] != "custom" and not watchlist["is_sector"]
            for symbol in watchlist["candidate_symbols"]
        }
        sector_symbols = {
            symbol
            for watchlist in list_candidates
            if watchlist["is_sector"]
            for symbol in watchlist["candidate_symbols"]
        }
        context["excluded_sector_symbols"] = sorted(sector_symbols)
        non_sector_curated_symbols = {
            symbol
            for watchlist in list_candidates
            if watchlist["owner_type"] != "custom" and not watchlist["is_sector"]
            for symbol in watchlist["candidate_symbols"]
        }
        snapshot, liquidity_gaps = liquidity_snapshot(now)
        gaps.extend(liquidity_gaps)
        eligible_curated, liquidity_context = filter_curated_symbols(
            curated_symbols,
            snapshot,
            liquidity_exempt_symbols=non_sector_curated_symbols,
        )
        context["liquidity_filter"] = liquidity_context
        all_candidates = held_symbols | custom_symbols | curated_symbols
        time_slot = int(now.timestamp() // 1800)

        selected = set(rotating_symbols(held_symbols, ROBINHOOD_MAX_SYMBOLS, time_slot, "held"))
        remaining = max(0, ROBINHOOD_MAX_SYMBOLS - len(selected))
        selected.update(rotating_symbols(custom_symbols - selected, remaining, time_slot, "custom"))
        remaining = max(0, ROBINHOOD_MAX_SYMBOLS - len(selected))
        selected.update(rotating_symbols(eligible_curated - selected, remaining, time_slot, "curated"))

        context["candidate_symbol_count"] = len(all_candidates)
        context["symbols"] = sorted(selected)
        for watchlist in list_candidates:
            candidates = watchlist["candidate_symbols"]
            selected_from_list = set() if watchlist["is_sector"] else candidates & selected
            context["lists"].append(
                {
                    "display_name": watchlist["display_name"],
                    "owner_type": watchlist["owner_type"],
                    "is_sector": watchlist["is_sector"],
                    "candidate_symbol_count": len(candidates),
                    "eligible_symbol_count": (
                        0
                        if watchlist["is_sector"]
                        else len(candidates)
                        if watchlist["owner_type"] == "custom"
                        else len(candidates & eligible_curated)
                    ),
                    "symbols": sorted(selected_from_list),
                }
            )
        context["tools_used"] = sorted(set(context["tools_used"]))
        context["status"] = "partial" if gaps else "used"
        return context, gaps
    except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
        return context, [f"Robinhood watchlist context unavailable: {type(exc).__name__}: {exc}"]


def read_sources() -> list[tuple[str, str, str]]:
    if not SOURCE_LIST.exists():
        raise SystemExit(f"missing source list: {SOURCE_LIST}")
    category = "Uncategorized"
    sources: list[tuple[str, str, str]] = []
    for line in SOURCE_LIST.read_text(encoding="utf-8").splitlines():
        if line.startswith("### "):
            category = line[4:].strip()
            continue
        match = re.match(r"^- ([^:]+): (https?://\S+)", line.strip())
        if match:
            sources.append((category, match.group(1).strip(), match.group(2).strip()))
    return sources


def recent_urls(limit: int = 8) -> set[str]:
    urls: set[str] = set()
    packets = sorted(DATA_ROOT.glob(PACKET_GLOB), key=lambda p: p.stat().st_mtime, reverse=True)
    for packet in packets[:limit]:
        try:
            text = packet.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        urls.update(re.findall(r"https?://[^\s)>\]]+", text))
    return urls


def load_email_corpus_state() -> dict:
    if not EMAIL_CORPUS_STATE.exists():
        return {"version": 1, "processed_ids": [], "initialized": False}
    try:
        state = json.loads(EMAIL_CORPUS_STATE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        backup = EMAIL_CORPUS_STATE.with_suffix(f".bad-{dt.datetime.now(TZ).strftime('%Y%m%dT%H%M%S')}.json")
        EMAIL_CORPUS_STATE.replace(backup)
        return {"version": 1, "processed_ids": [], "initialized": False}
    state.setdefault("version", 1)
    state.setdefault("processed_ids", [])
    state.setdefault("initialized", True)
    return state


def save_email_corpus_state(state: dict) -> None:
    EMAIL_CORPUS_STATE.parent.mkdir(parents=True, exist_ok=True)
    state["processed_ids"] = list(dict.fromkeys(state.get("processed_ids", [])))[-5000:]
    tmp = EMAIL_CORPUS_STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(EMAIL_CORPUS_STATE)


def email_json_files() -> list[Path]:
    if not EMAIL_INBOX.exists():
        return []
    return sorted(
        (p for p in EMAIL_INBOX.glob("*.json") if p.name != "last-triage.json"),
        key=lambda p: p.stat().st_mtime,
    )


def first_email_message(data: object) -> dict | None:
    if isinstance(data, dict):
        if "body_text" in data and ("message_id" in data or "thread_id" in data):
            return {"_gws_read": data}
        if "payload" in data and ("id" in data or "threadId" in data):
            return data
        for key in ("message", "gmailMessage", "data"):
            found = first_email_message(data.get(key))
            if found:
                return found
        for value in data.values():
            found = first_email_message(value)
            if found:
                return found
    if isinstance(data, list):
        for item in data:
            found = first_email_message(item)
            if found:
                return found
    return None


def email_headers(message: dict) -> dict[str, str]:
    return {
        str(header.get("name", "")).lower(): str(header.get("value", ""))
        for header in message.get("payload", {}).get("headers", []) or []
        if header.get("name")
    }


def decode_email_body_data(value: str) -> str:
    if not value:
        return ""
    padded = value + "=" * (-len(value) % 4)
    try:
        import base64

        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")
    except Exception:
        return ""


def gmail_payload_html(payload: dict) -> str:
    chunks: list[str] = []
    mime_type = str(payload.get("mimeType", ""))
    data = decode_email_body_data(str((payload.get("body") or {}).get("data", "")))
    if data:
        chunks.append(data if "html" in mime_type else html.escape(data))
    for part in payload.get("parts", []) or []:
        if isinstance(part, dict):
            nested = gmail_payload_html(part)
            if nested:
                chunks.append(nested)
    return "\n".join(chunks)


def article_links_from_html(raw_html: str) -> list[str]:
    """Return deduplicated HTTP(S) article links, excluding mail/navigation links."""
    links: list[str] = []
    for href in re.findall(r'''(?is)<a\b[^>]*?\bhref\s*=\s*[\"']([^\"']+)[\"']''', raw_html or ""):
        url = html.unescape(href).strip()
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        if any(token in url.lower() for token in ("unsubscribe", "manage-preferences", "mailto:")):
            continue
        links.append(url)
    return list(dict.fromkeys(links))


def email_source_from_file(path: Path) -> tuple[dict | None, str | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - bad inbox files should not stop the market packet.
        return None, f"{path.name}: unreadable email JSON: {type(exc).__name__}: {exc}"
    message = first_email_message(data)
    if not message:
        return None, f"{path.name}: no Gmail message object found"

    if "_gws_read" in message:
        raw = message["_gws_read"]
        message_id = str(raw.get("thread_id") or raw.get("message_id") or path.stem)
        subject = str(raw.get("subject") or "(no subject)")
        sender_obj = raw.get("from")
        if isinstance(sender_obj, dict):
            sender = f"{sender_obj.get('name') or ''} <{sender_obj.get('email') or ''}>".strip()
        else:
            sender = str(sender_obj or "unknown")
        received = str(raw.get("date") or "")
        body_html = str(raw.get("body_html") or "")
        body_text = str(raw.get("body_text") or "")
        raw_body = body_html or html.escape(body_text)
    else:
        message_id = str(message.get("id") or message.get("threadId") or path.stem)
        headers = email_headers(message)
        subject = headers.get("subject", "(no subject)")
        sender = headers.get("from", "unknown")
        received = headers.get("date", "")
        raw_body = gmail_payload_html(message.get("payload", {}) or {})

    clean = strip_html(raw_body)
    clean = "\n".join(
        part
        for part in (
            f"Email subject: {subject}",
            f"Email from: {sender}",
            f"Email received: {received}",
            clean,
        )
        if part.strip()
    )
    if is_mostly_binary(clean):
        return None, f"{path.name}: unreadable email body"
    source_snippets = snippets(clean)
    return (
        {
            "id": f"email:{message_id}",
            "category": "Email newsletters",
            "label": f"Email: {subject[:80]}",
            "url": f"gmail:{message_id}",
            "final_url": str(path.relative_to(DATA_ROOT)) if path.is_relative_to(DATA_ROOT) else str(path),
            "status": 200,
            "title": subject[:180],
            "tickers": tickers_in(" ".join(source_snippets)),
            "snippets": source_snippets,
            "repeat": False,
            "source_type": "email_html",
            "message_id": message_id,
            "from": sender,
            "received_at": received,
            "article_urls": article_links_from_html(raw_body),
        },
        None,
    )


def new_email_sources(defer_commit: bool = False):
    """Return new email sources; optionally defer state commit until handoff write."""
    files = email_json_files()
    state = load_email_corpus_state()
    processed = set(state.get("processed_ids", []))
    gaps: list[str] = []

    if not state.get("initialized") and not EMAIL_BACKFILL:
        for path in files:
            processed.add(path.stem)
        state["processed_ids"] = list(processed)
        state["initialized"] = True
        state["last_scan_at"] = dt.datetime.now(TZ).isoformat()
        save_email_corpus_state(state)
        result = ([], [f"email corpus initialized at current inbox baseline; set MARKET_WORKER_EMAIL_BACKFILL=1 to ingest existing {len(files)} email files"])
        return (*result, None) if defer_commit else result

    sources: list[dict] = []
    scanned = 0
    for path in files:
        file_key = path.stem
        if file_key in processed:
            continue
        item, gap = email_source_from_file(path)
        scanned += 1
        if item:
            message_key = str(item.get("message_id") or file_key)
            if message_key != file_key and message_key in processed:
                gaps.append(f"{path.name}: skipped duplicate Gmail message id {message_key}")
            else:
                processed.add(message_key)
                sources.append(item)
        processed.add(file_key)
        if gap:
            gaps.append(gap)
        if scanned >= EMAIL_MAX_PER_RUN:
            break

    state["processed_ids"] = list(processed)
    state["initialized"] = True
    state["last_scan_at"] = dt.datetime.now(TZ).isoformat()
    state["last_added"] = [item["final_url"] for item in sources]
    if not defer_commit:
        save_email_corpus_state(state)
        return sources, gaps
    return sources, gaps, state


def rss_sources_from_records(records: list[dict], valid_symbols: set[str]) -> list[dict]:
    """Convert normalized collector records into article-enrichment inputs."""
    sources = []
    for record in records:
        clean = record["clean_text"]
        sources.append(
            {
                "id": record["id"],
                "category": record["category"],
                "label": f"RSS: {record['label']}",
                "url": record["feed_url"],
                "final_url": record["url"],
                "status": 200,
                "title": record["title"][:180],
                # Preserve the full normalized entry for Luna.  It decides whether
                # an otherwise irrelevant macro/political item should be dropped.
                "snippets": [clean[:3000]],
                "tickers": discovered_tickers(clean, valid_symbols),
                "repeat": False,
                "source_type": "rss_atom",
                "published": record.get("published", ""),
                "article_urls": [record["url"]],
            }
        )
    return sources


def new_enriched_sources() -> tuple[list[dict], list[str]]:
    """Claim article-enriched records for packet extraction; retry after crashes."""
    global ARTICLE_HANDOFF_CLAIM
    records, ARTICLE_HANDOFF_CLAIM = market_rss_listener.claim_handoff(ARTICLE_HANDOFF)
    unique = list({str(item["id"]): item for item in records}.values())
    return unique, []


def fetch_url(url: str, timeout: int = 18) -> tuple[int | None, str | None, str]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, identity",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(450_000)
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.status, resp.geturl(), decode_body(raw, resp.headers.get("Content-Encoding"), charset)
    except urllib.error.HTTPError as exc:
        raw = exc.read(80_000)
        charset = exc.headers.get_content_charset() or "utf-8"
        return exc.code, url, decode_body(raw, exc.headers.get("Content-Encoding"), charset)
    except Exception as exc:  # noqa: BLE001 - packet should record network errors.
        return None, url, f"{type(exc).__name__}: {exc}"


def decode_body(raw: bytes, encoding: str | None, charset: str) -> str:
    encoding = (encoding or "").lower()
    try:
        if encoding == "gzip" or raw.startswith(b"\x1f\x8b"):
            raw = gzip.decompress(raw)
        elif encoding == "deflate":
            raw = zlib.decompress(raw)
    except Exception:
        pass
    text = raw.decode(charset, errors="replace")
    text = text.replace("\x00", "")
    return text


PAYWALL_PATTERNS = (
    r"\bsubscribe to continue\b", r"\bsubscribe to read\b", r"\bsubscription required\b",
    r"\balready a subscriber\b", r"\bto continue reading\b", r"\bthis content is for subscribers\b",
    r"\bunlock (?:this |the )?article\b", r"\bpaywall\b",
)


def load_article_fetch_state(path: Path = ARTICLE_FETCH_STATE) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {"paywalled_domains": {}}
    except (OSError, json.JSONDecodeError):
        return {"paywalled_domains": {}}


def save_article_fetch_state(state: dict, path: Path = ARTICLE_FETCH_STATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)


def paywall_reason(status: int | None, clean: str) -> str | None:
    if status in {401, 402, 451}:
        return f"HTTP {status} access restriction"
    if status == 403 and any(re.search(pattern, clean, re.I) for pattern in PAYWALL_PATTERNS):
        return "subscriber-only access"
    if any(re.search(pattern, clean, re.I) for pattern in PAYWALL_PATTERNS):
        return "subscriber-only page text"
    return None


def strip_html(text: str) -> str:
    parsed = readable_text_from_html(text)
    if parsed:
        return parsed

    text = re.sub(r"(?is)<(script|style|noscript|svg).*?</\1>", " ", text)
    title_match = re.search(r"(?is)<title[^>]*>(.*?)</title>", text)
    title = html.unescape(re.sub(r"\s+", " ", title_match.group(1)).strip()) if title_match else ""
    body = re.sub(r"(?s)<[^>]+>", " ", text)
    body = html.unescape(body)
    body = re.sub(r"\s+", " ", body).strip()
    return (title + "\n" + body).strip()


def readable_text_from_html(raw_html: str) -> str:
    if lxml_html is None or "<" not in raw_html:
        return ""
    try:
        doc = lxml_html.fromstring(raw_html)
    except Exception:
        return ""

    title = ""
    try:
        title = " ".join(doc.xpath("//title/text()")).strip()
    except Exception:
        title = ""

    for elem in list(doc.iter()):
        tag = (elem.tag or "").lower() if isinstance(elem.tag, str) else ""
        if tag in DROP_TAGS or should_drop_element(elem):
            parent = elem.getparent()
            if parent is not None:
                parent.remove(elem)

    containers = find_content_containers(doc)
    body = "\n".join(c.text_content() for c in containers)
    body = html.unescape(re.sub(r"\s+", " ", body)).strip()
    title = html.unescape(re.sub(r"\s+", " ", title)).strip()
    return (title + "\n" + body).strip()


def should_drop_element(elem) -> bool:
    role = (elem.get("role") or "").strip().lower()
    if role in DROP_ROLE_VALUES:
        return True

    attr_text = " ".join(
        elem.get(name) or ""
        for name in ("id", "class", "aria-label", "data-testid", "data-module", "data-track")
    )
    return bool(DROP_ATTR_RE.search(attr_text))


def find_content_containers(doc) -> list:
    body = doc.find("body")
    if body is None:
        body = doc
    candidates = []
    for xpath in ("//article", "//main", "//*[@role='main']", "//*[@id='main']", "//*[@class='main']"):
        try:
            candidates.extend(doc.xpath(xpath))
        except Exception:
            continue

    useful = []
    for candidate in candidates:
        text = candidate.text_content()
        if len(text.split()) >= 40 and content_signal_count(text) >= 2:
            useful.append(candidate)
    return useful or [body]


def content_signal_count(text: str) -> int:
    lower = text.lower()
    ticker_hits = len(tickers_in(text))
    catalyst_hits = sum(1 for word in CATALYST_WORDS if re.search(rf"\b{re.escape(word)}\b", lower))
    return ticker_hits + catalyst_hits


def snippets(clean: str, max_snippets: int = 4) -> list[str]:
    if not clean:
        return []
    terms = sorted(WATCHLIST | CATALYST_WORDS, key=len, reverse=True)
    found: list[str] = []
    seen = set()
    for term in terms:
        for match in re.finditer(rf"(?i)\b{re.escape(term)}\b", clean):
            start = max(0, match.start() - 180)
            end = min(len(clean), match.end() + 260)
            snippet = clean[start:end].strip()
            snippet = re.sub(r"\s+", " ", snippet)
            if is_mostly_binary(snippet):
                continue
            if is_noise_snippet(snippet):
                continue
            key = snippet[:120].lower()
            if key in seen:
                continue
            seen.add(key)
            found.append(snippet)
            if len(found) >= max_snippets:
                return found
    return found


def is_mostly_binary(text: str) -> bool:
    if not text:
        return False
    sample = text[:500]
    bad = sum(1 for ch in sample if ch == "\ufffd" or (ord(ch) < 32 and ch not in "\n\t\r"))
    return bad / max(len(sample), 1) > 0.03


def tickers_in(text: str) -> list[str]:
    upper = text.upper()
    return sorted(t for t in WATCHLIST if re.search(rf"\b{re.escape(t)}\b", upper))


def discovered_tickers(text: str, valid_symbols: set[str]) -> list[str]:
    found = set(tickers_in(text))
    patterns = [
        r"\b(?:NASDAQ|NYSE|AMEX|OTC)\s*:\s*([A-Z][A-Z0-9.-]{1,9})\b",
        r"\b([A-Z][A-Z0-9.-]{1,9})\s*:",
        r"\b([A-Z][A-Z0-9.-]{1,9})\s+[+-]?\d+(?:\.\d+)?%",
        r"\(([A-Z][A-Z0-9.-]{1,9})\)",
    ]
    for pattern in patterns:
        for symbol in re.findall(pattern, text.upper()):
            if symbol not in TICKER_STOPWORDS and symbol in valid_symbols:
                found.add(symbol)
    return sorted(found)


def has_catalyst(text: str) -> bool:
    lower = text.lower()
    return any(re.search(rf"\b{re.escape(word)}\b", lower) for word in CATALYST_WORDS)


def is_noise_snippet(text: str) -> bool:
    lower = text.lower()
    if any(phrase in lower for phrase in NOISE_PHRASES):
        return True

    # Ticker comparison widgets and market data tables are common false positives.
    if re.search(r"\b[A-Z]{2,5}\s+vs\s+[A-Z]{2,5}\b", text) and not has_catalyst(text):
        return True

    ticker_count = len(tickers_in(text))
    if ticker_count >= 5 and not has_catalyst(text):
        return True

    menu_words = re.findall(
        r"\b(calendar|screeners?|newsletter|premium|login|register|portfolio|"
        r"credit cards?|watchlist|stock screener|market screener|tools?)\b",
        lower,
    )
    if len(menu_words) >= 4 and not has_catalyst(text):
        return True

    return False


def enrich_linked_articles(sources: list[dict], gaps: list[str], now: dt.datetime, fetcher=fetch_url, state_path: Path = ARTICLE_FETCH_STATE) -> None:
    """Append readable linked-article evidence; paywalled domains are retried weekly."""
    state = load_article_fetch_state(state_path)
    paywalled = state.setdefault("paywalled_domains", {})
    attempted = 0
    retry_after = dt.timedelta(days=PAYWALL_RETRY_DAYS)
    for item in sources:
        if item.get("source_type") not in {"rss_atom", "email_html"}:
            continue
        item.setdefault("article_fetches", [])
        for url in list(dict.fromkeys(item.get("article_urls") or []))[:ARTICLE_MAX_LINKS_PER_SOURCE]:
            if attempted >= ARTICLE_MAX_LINKS_PER_RUN:
                break
            domain = (urllib.parse.urlsplit(url).hostname or "").lower()
            prior = paywalled.get(domain, {})
            try:
                last_paywall = dt.datetime.fromisoformat(str(prior.get("detected_at", "")))
            except ValueError:
                last_paywall = None
            if last_paywall and now.astimezone(dt.timezone.utc) - last_paywall.astimezone(dt.timezone.utc) < retry_after:
                item["article_fetches"].append({"url": url, "status": "skipped", "reason": "known paywall; weekly retry pending"})
                continue
            attempted += 1
            status, final_url, raw = fetcher(url)
            clean = strip_html(raw)
            reason = paywall_reason(status, clean)
            if reason:
                paywalled[domain] = {"detected_at": now.astimezone(dt.timezone.utc).isoformat(), "reason": reason, "url": final_url or url}
                item["article_fetches"].append({"url": final_url or url, "status": status, "reason": reason})
                gaps.append(f"{item['label']}: ignored paywalled article {domain}: {reason}")
                continue
            if status is None or status >= 400:
                item["article_fetches"].append({"url": final_url or url, "status": status, "reason": "fetch failed"})
                gaps.append(f"{item['label']}: linked article fetch status {status}: {(clean or raw)[:140]}")
                continue
            article_snippets = snippets(clean)
            item["article_fetches"].append({"url": final_url or url, "status": status, "reason": "fetched", "title": clean.splitlines()[0][:180] if clean else "article"})
            retained = []
            for snippet in article_snippets:
                tagged = f"Linked article ({final_url or url}): {snippet}"
                if tagged not in item["snippets"] and tagged not in retained:
                    retained.append(tagged)
            # Article evidence is more informative than an RSS synopsis or
            # newsletter link list, and source_packet keeps only three snippets.
            item["snippets"] = retained + item["snippets"]
            item["tickers"] = sorted(set(item.get("tickers", [])) | set(tickers_in(clean)))
    state["last_run_at"] = now.astimezone(dt.timezone.utc).isoformat()
    save_article_fetch_state(state, state_path)


def extraction_prompt(source_packet: str) -> str:
    return (
        "Use only the source packet below. Do not invent facts. Return concise "
        "extracted evidence, affected tickers, catalyst type, time-horizon "
        "relevance, risks, ranking notes, and source URLs from the packet.\n\n"
        + source_packet[:18_000]
    )


def codex_complete(prompt: str, model: str) -> tuple[str, str]:
    with tempfile.NamedTemporaryFile(
        "w+", encoding="utf-8", prefix="market-codex-extract-", suffix=".txt", delete=True
    ) as out:
        cmd = [
            "codex",
            "-s",
            "read-only",
            "-a",
            "never",
            "exec",
            "-m",
            model,
            "-C",
            str(ROOT),
            "--skip-git-repo-check",
            "--output-last-message",
            out.name,
            "-",
        ]
        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=CODEX_TIMEOUT,
                check=False,
            )
            out.seek(0)
            response = out.read().strip()
        except Exception as exc:  # noqa: BLE001
            return "unavailable", f"{type(exc).__name__}: {exc}"

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return "unavailable", f"codex exit {proc.returncode}: {' | '.join(detail[-4:])[:500]}"
    if not response:
        return "unavailable", "codex returned empty extraction"
    return "used", response


def codex_extract(source_packet: str, model: str) -> tuple[str, str]:
    return codex_complete(extraction_prompt(source_packet), model)


def luna_filter(source_packet: str) -> tuple[str, str]:
    """Return a compact Terra input, failing open if Luna is unavailable/invalid."""
    prompt = (
        "You are a market-news triage filter. Keep only IDs that are actionable, "
        "company-specific technology or energy news: earnings/results/guidance, "
        "contracts, product or regulatory events, analyst actions, material supply "
        "or demand events, or company-specific dislocations. Discard general macro "
        "opinion, politics, celebrity content, broad market commentary, unrelated "
        "sectors, menus, and duplicate/noise. Use only the records below. Return "
        "exactly JSON: {\"keep_ids\":[integer,...]}.\n\n"
        + source_packet[:18_000]
    )
    status, response = codex_complete(prompt, LUNA_MODEL)
    if status != "used":
        return "unavailable", response
    match = re.search(r"\{\s*\"keep_ids\"\s*:\s*\[([^\]]*)\]", response)
    if not match:
        return "unavailable", "Luna returned no parseable keep_ids"
    ids = {int(value) for value in re.findall(r"\d+", match.group(1))}
    selected = [line for line in source_packet.splitlines() if re.match(r"\[(\d+)\]", line) and int(re.match(r"\[(\d+)\]", line).group(1)) in ids]
    return "used", "\n".join(selected)


def ollama_extract(source_packet: str) -> tuple[str, str]:
    endpoint = os.environ.get("OLLAMA_ENDPOINT", "http://brian-pc.local:11434/api/generate")
    model = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")
    keep_alive = os.environ.get("OLLAMA_KEEP_ALIVE", "2h")
    timeout = int(os.environ.get("OLLAMA_TIMEOUT", "180"))
    prompt = extraction_prompt(source_packet)
    payload = json.dumps(
        {"model": model, "prompt": prompt, "stream": False, "keep_alive": keep_alive}
    ).encode()
    req = urllib.request.Request(endpoint, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        return "used", str(data.get("response", "")).strip()
    except Exception as exc:  # noqa: BLE001
        return "unavailable", f"{type(exc).__name__}: {exc}"


def model_extract(source_packet: str) -> tuple[str, str, str, str, list[str]]:
    diagnostics: list[str] = []
    terra_packet = source_packet
    if CODEX_ENABLED:
        if LUNA_FILTER_ENABLED:
            status, filtered = luna_filter(source_packet)
            if status == "used":
                if filtered:
                    terra_packet = filtered
                    diagnostics.append(
                        f"luna/{LUNA_MODEL}: selected {len(filtered.splitlines())}/{len(source_packet.splitlines())} records"
                    )
                else:
                    diagnostics.append(f"luna/{LUNA_MODEL}: selected 0 records; Terra skipped")
                    return "used", "No high-priority corporate tech/energy items passed Luna triage.", "codex", LUNA_MODEL, diagnostics
            else:
                diagnostics.append(f"luna/{LUNA_MODEL}: {filtered}; fail-open to Terra")
        status, text = codex_extract(terra_packet, TERRA_MODEL)
        if status == "used":
            return status, text, "codex", TERRA_MODEL, diagnostics
        diagnostics.append(f"terra/{TERRA_MODEL}: {text}")

    status, text = ollama_extract(source_packet)
    ollama_model = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")
    if status == "used":
        return status, text, "ollama", ollama_model, diagnostics
    diagnostics.append(f"ollama/{ollama_model}: {text}")
    return "unavailable", "; ".join(diagnostics), "none", "none", diagnostics


def md_escape(text: str) -> str:
    return text.replace("\r", "").strip()


def refresh_robinhood_research(symbols: list[str]) -> tuple[dict, list[str]]:
    if not ROBINHOOD_ENABLED:
        return {}, []
    try:
        snapshot = market_robinhood_research.collect(symbols)
        market_robinhood_research.write_snapshot(snapshot)
        return snapshot, []
    except Exception as exc:  # noqa: BLE001 - research should not block packet creation.
        cached = {}
        if market_robinhood_research.OUT_FILE.exists():
            try:
                cached = json.loads(market_robinhood_research.OUT_FILE.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                cached = {}
        return cached, [f"Robinhood research refresh unavailable: {type(exc).__name__}: {exc}"]


def research_candidate_symbols(snapshot: dict) -> set[str]:
    candidates = ((snapshot.get("scanners") or {}).get("candidates") or {})
    return {
        str(ticker).upper()
        for ticker in candidates
        if re.fullmatch(r"[A-Z0-9.-]{1,15}", str(ticker).upper())
    }


def main() -> int:
    now = dt.datetime.now(TZ)
    stamp = now.strftime("%Y-%m-%dT%H%M")
    liquidity_rows = (read_liquidity_cache().get("rows") or {})
    valid_discovery_symbols = set(liquidity_rows) | WATCHLIST
    sources = read_sources()
    previous_urls = recent_urls()
    fetched = []
    data_gaps = []

    for category, label, url in sources:
        status, final_url, raw = fetch_url(url)
        clean = strip_html(raw)
        if is_mostly_binary(clean):
            clean = f"unreadable response body from {url}"
        title = clean.splitlines()[0] if clean else label
        source_snippets = snippets(clean)
        source_tickers = discovered_tickers(" ".join(source_snippets), valid_discovery_symbols)
        fetched.append(
            {
                "category": category,
                "label": label,
                "url": url,
                "final_url": final_url or url,
                "status": status,
                "title": title[:180],
                "tickers": source_tickers,
                "snippets": source_snippets,
                "repeat": url in previous_urls or (final_url or url) in previous_urls,
            }
        )
        if status is None or (status >= 400):
            data_gaps.append(f"{label}: fetch status {status}; {clean[:180]}")
        elif not source_snippets:
            data_gaps.append(f"{label}: fetched but no watchlist/catalyst snippet found")

    enriched_items, enrichment_gaps = new_enriched_sources()
    fetched.extend(enriched_items)
    data_gaps.extend(enrichment_gaps)

    source_packet_lines = []
    record_id = 0
    for item in fetched:
        if not item["snippets"] and item["status"] and item["status"] < 400:
            continue
        source_packet_lines.append(f"[{record_id}] {item['label']} ({item['status']}): {item['final_url']}")
        record_id += 1
        for snip in item["snippets"][:3]:
            source_packet_lines.append(f"[{record_id}] {item['label']} | {item['final_url']} | {snip}")
            record_id += 1
    source_packet = "\n".join(source_packet_lines)
    extract_status, extract_text, extract_provider, extract_model, extract_diagnostics = model_extract(source_packet)

    packet_data = {
        "schema": "openclaw.market_worker.packet.v1",
        "source": "market-worker",
        "status": "proposed",
        "target": "market-scout",
        "run_objective": "hourly public-source market context",
        "generated_at": now.isoformat(),
        "model": (
            f"deterministic cron fetcher + Luna triage codex/{LUNA_MODEL} + Terra extraction codex/{TERRA_MODEL} "
            f"+ fallback ollama/{os.environ.get('OLLAMA_MODEL', 'llama3.1:8b')}"
        ),
        "news_discovered_symbols": sorted({ticker for item in fetched for ticker in item["tickers"]}),
        "source_starting_points": fetched,
        "extracted_facts": {
            "extraction_status": extract_status,
            "extraction_provider": extract_provider,
            "extraction_model": extract_model,
            "extraction_text": extract_text,
            "extraction_diagnostics": extract_diagnostics,
            "ollama_status": extract_status if extract_provider == "ollama" else "not_used",
            "ollama_text": extract_text,
            "fallback_facts": [
                {
                    "affected_tickers": tickers_in(snip) or ["watchlist/sector"],
                    "catalyst_type": "email/html mention" if item.get("source_type") == "email_html" else "source mention",
                    "time_horizon_relevance": "needs scout verification",
                    "risk_note": "verify from URL before use",
                    "source": item["final_url"],
                    "fact": snip[:500],
                }
                for item in fetched
                for snip in item["snippets"][:2]
            ],
        },
        "ranking_notes": [
            "Prioritize fetched items with explicit watchlist tickers plus earnings, guidance, analyst, oil/gas, rates, or sector-move language.",
            "Ignore snippets from blocked or low-signal pages until verified by the main market scout.",
        ],
        "source_list_recommendations": [
            "No automatic source-list edits made by cron. Review repeated blocked or low-signal pages in data_gaps."
        ],
        "data_gaps": data_gaps[:20],
        "skipped_repeats": [item["final_url"] for item in fetched if item["repeat"]],
    }

    packet = [
        "source: market-worker",
        "status: proposed",
        "target: market-scout",
        "run_objective: hourly public-source market context",
        f"generated_at: {now.isoformat()}",
        f"model: deterministic cron fetcher + Luna triage codex/{LUNA_MODEL} + Terra extraction codex/{TERRA_MODEL} + fallback ollama/{os.environ.get('OLLAMA_MODEL', 'llama3.1:8b')}",
        "",
    ]
    packet.extend(
        [
            "",
            "## news_discovered_symbols",
            f"- symbols: {', '.join(packet_data['news_discovered_symbols']) or 'none'}",
            "",
            "## source_starting_points",
        ]
    )
    for item in fetched:
        outcome = f"HTTP {item['status']}" if item["status"] is not None else "fetch failed"
        repeat = "; seen in prior packet" if item["repeat"] else ""
        packet.append(f"- {item['label']} ({item['category']}): {outcome}{repeat} - {item['final_url']}")

    packet.extend(["", "## sources"])
    for item in fetched:
        if not item["snippets"] and item["status"] and item["status"] < 400:
            continue
        packet.append(f"- title: {md_escape(item['title'])}")
        packet.append(f"  url: {item['final_url']}")
        packet.append(f"  fetched_status: {item['status']}")
        packet.append(f"  affected_tickers: {', '.join(item['tickers']) if item['tickers'] else 'not detected'}")
        for snip in item["snippets"][:3]:
            packet.append(f"  snippet: {md_escape(snip[:700])}")

    packet.extend(["", "## extracted_facts"])
    if extract_status == "used" and extract_text:
        packet.append(extract_text)
    else:
        for item in fetched:
            for snip in item["snippets"][:2]:
                tickers = ", ".join(tickers_in(snip)) or "watchlist/sector"
                catalyst_type = "email/html mention" if item.get("source_type") == "email_html" else "source mention"
                packet.append(
                    f"- affected_tickers: {tickers}; catalyst_type: {catalyst_type}; "
                    f"time_horizon_relevance: needs scout verification; risk_note: verify from URL before use; "
                    f"source: {item['final_url']}; fact: {md_escape(snip[:500])}"
                )

    packet.extend(
        [
            "",
            "## ranking_notes",
            "- Prioritize fetched items with explicit watchlist tickers plus earnings, guidance, analyst, oil/gas, rates, or sector-move language.",
            "- Ignore snippets from blocked or low-signal pages until verified by the main market scout.",
            "",
            "## source_list_recommendations",
            "- No automatic source-list edits made by cron. Review repeated blocked or low-signal pages in data_gaps.",
            "",
            "## data_gaps",
            (
                f"- model_extraction: {extract_status}; provider={extract_provider}; "
                f"model={extract_model}; {md_escape(extract_text[:500]) if extract_status != 'used' else 'completed'}"
            ),
        ]
    )
    packet.extend(f"- {md_escape(gap)}" for gap in data_gaps[:20])
    packet.extend(["", "## skipped_repeats"])
    repeats = [item for item in fetched if item["repeat"]]
    if repeats:
        packet.extend(f"- {item['final_url']}" for item in repeats)
    else:
        packet.append("- none detected from recent packet files")

    md_out = DATA_ROOT / f"{stamp}-market-packet-hourly.md"
    json_out = DATA_ROOT / f"{stamp}-market-packet-hourly.json"

    json_tmp = json_out.with_suffix(".tmp")
    json_tmp.write_text(json.dumps(packet_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    json_tmp.replace(json_out)

    md_tmp = md_out.with_suffix(".tmp")
    md_tmp.write_text("\n".join(packet).rstrip() + "\n", encoding="utf-8")
    md_tmp.replace(md_out)

    market_rss_listener.acknowledge_handoff(ARTICLE_HANDOFF_CLAIM)
    print(md_out)
    print(json_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
