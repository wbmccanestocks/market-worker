#!/usr/bin/env python3
"""Deterministic RSS/Atom poller for market-worker."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get("MARKET_WORKER_DATA_DIR", str(ROOT.parent / "handoffs" / "market-worker")))
DEFAULT_CONFIG = DATA_ROOT / "market-rss-feeds.json"
DEFAULT_STATE = DATA_ROOT / "market-rss-listener-state.json"
DEFAULT_HANDOFF = DATA_ROOT / "market-rss-handoff.jsonl"
USER_AGENT = "OpenClawMarketWorkerRSS/1.0 (+public-market-research)"
DROP_TAGS = ("script", "style", "noscript", "svg", "iframe", "form", "nav", "footer")
HYPE_PATTERNS = (r"\bget rich quick\b", r"\bguaranteed returns?\b", r"\bdouble your money\b", r"\bact now\b", r"\blimited time\b", r"\bclick here\b", r"\bsubscribe now\b", r"\bjoin (?:my|our) (?:premium|discord|telegram|mastermind)\b", r"\bmust[- ]buy\b", r"\bcan't miss(?: stock)?\b")
SPONSORED_PATTERN = re.compile(r"\b(?:sponsored content|advertisement|paid promotion)\b", re.I)
RATE_LIMIT_FALLBACK_SECONDS = 3600


def clean_text(markup: str) -> str:
    """Convert HTML/XHTML feed fields to compact readable text."""
    soup = BeautifulSoup(markup or "", "html.parser")
    for tag in soup.find_all(DROP_TAGS):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()


def hype_reason(text: str) -> str | None:
    """Drop only unmistakable promotions; uncertain content goes to Luna."""
    hits = sum(bool(re.search(pattern, text, re.I)) for pattern in HYPE_PATTERNS)
    if hits >= 2 or (hits >= 1 and SPONSORED_PATTERN.search(text)):
        return "obvious promotional call-to-action"
    return None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _field(element: ET.Element, *names: str) -> str:
    wanted = {name.lower() for name in names}
    for child in element.iter():
        text = "".join(child.itertext()).strip()
        if _local_name(child.tag) in wanted and text:
            return text
    return ""


def _link(element: ET.Element) -> str:
    for child in element.iter():
        if _local_name(child.tag) != "link":
            continue
        href = (child.attrib.get("href") or "").strip()
        if href and child.attrib.get("rel", "alternate") in {"alternate", ""}:
            return href
        if (child.text or "").strip():
            return child.text.strip()
    return ""


def parse_feed(xml_text: str, feed_url: str) -> list[dict]:
    """Return normalized RSS 2.0 or Atom entries; malformed feeds return []."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    records = []
    for element in root.iter():
        if _local_name(element.tag) not in {"item", "entry"}:
            continue
        title = clean_text(_field(element, "title"))
        description = clean_text(_field(element, "description", "summary", "content", "encoded"))
        url = _link(element) or feed_url
        published = _field(element, "pubDate", "published", "updated", "date")
        guid = _field(element, "guid", "id") or url or (title + published)
        text = clean_text(" ".join(part for part in (title, description) if part))
        if not text:
            continue
        key = hashlib.sha256((feed_url + "\n" + guid).encode("utf-8", errors="replace")).hexdigest()
        records.append({"id": key, "title": title or "RSS/Atom entry", "url": url, "published": published, "clean_text": text})
    return records


def load_config(path: Path = DEFAULT_CONFIG) -> list[dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    feeds = payload.get("feeds", payload) if isinstance(payload, dict) else payload
    return [item for item in feeds if isinstance(item, dict) and str(item.get("url") or "").startswith(("http://", "https://"))] if isinstance(feeds, list) else []


def _load_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(state, dict) and isinstance(state.get("seen_ids"), list):
            state.setdefault("feeds", {})
            return state
        return {"seen_ids": [], "initialized": False, "feeds": {}}
    except (OSError, json.JSONDecodeError):
        return {"seen_ids": [], "initialized": False, "feeds": {}}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["seen_ids"] = list(dict.fromkeys(state.get("seen_ids", [])))[-10000:]
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def fetch(url: str, timeout: int = 20) -> tuple[int | None, str, str]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.geturl(), response.read(1_500_000).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, url, exc.read(80_000).decode("utf-8", errors="replace")
    except Exception as exc:
        return None, url, f"{type(exc).__name__}: {exc}"


def _fetch_conditional(url: str, cached: dict, timeout: int = 20) -> tuple[int | None, str, str, dict[str, str]]:
    """Fetch a feed using validators saved from its previous successful poll."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9",
    }
    if cached.get("etag"):
        headers["If-None-Match"] = str(cached["etag"])
    if cached.get("last_modified"):
        headers["If-Modified-Since"] = str(cached["last_modified"])
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.geturl(), response.read(1_500_000).decode("utf-8", errors="replace"), dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        return exc.code, url, exc.read(80_000).decode("utf-8", errors="replace"), dict(exc.headers.items()) if exc.headers else {}
    except Exception as exc:
        return None, url, f"{type(exc).__name__}: {exc}", {}


def _retry_deadline(headers: dict[str, str], now: dt.datetime) -> str | None:
    raw = next((value for key, value in headers.items() if key.lower() == "retry-after"), "").strip()
    if not raw:
        return None
    try:
        return (now + dt.timedelta(seconds=max(0, int(raw)))).isoformat()
    except ValueError:
        try:
            return dt.datetime.strptime(raw, "%a, %d %b %Y %H:%M:%S GMT").replace(tzinfo=dt.timezone.utc).isoformat()
        except ValueError:
            return None


def _backed_off(feed_state: dict, now: dt.datetime) -> bool:
    try:
        return dt.datetime.fromisoformat(str(feed_state.get("retry_after", ""))).astimezone(dt.timezone.utc) > now
    except ValueError:
        return False


def poll(config_path: Path = DEFAULT_CONFIG, state_path: Path = DEFAULT_STATE, backfill: bool = False, fetcher: Callable[[str], tuple[int | None, str, str]] | None = None) -> tuple[list[dict], list[str]]:
    """Poll configured feeds; first run establishes a baseline unless backfill."""
    state = _load_state(state_path)
    seen = set(state.get("seen_ids", []))
    new_records, gaps, all_records = [], [], []
    now = dt.datetime.now(dt.timezone.utc)
    feed_states = state.setdefault("feeds", {})
    for feed in load_config(config_path):
        feed_url = str(feed["url"])
        feed_state = feed_states.setdefault(feed_url, {})
        label = str(feed.get("label") or feed_url)
        if _backed_off(feed_state, now):
            gaps.append(f"RSS {label}: skipped until Retry-After {feed_state['retry_after']}")
            continue
        if fetcher is None:
            status, final_url, body, headers = _fetch_conditional(feed_url, feed_state)
        else:
            status, final_url, body = fetcher(feed_url)
            headers = {}
        feed_state["last_status"] = status
        feed_state["last_polled_at"] = now.isoformat()
        if status == 304:
            continue
        if status == 429:
            deadline = _retry_deadline(headers, now)
            if not deadline:
                deadline = (now + dt.timedelta(seconds=RATE_LIMIT_FALLBACK_SECONDS)).isoformat()
                gaps.append(f"RSS {label}: rate-limited without Retry-After; conservatively deferred until {deadline}")
            else:
                gaps.append(f"RSS {label}: rate-limited; deferred until {deadline}")
            feed_state["retry_after"] = deadline
            continue
        feed_state.pop("retry_after", None)
        if status is not None and status < 400:
            for header_name, state_name in (("ETag", "etag"), ("Last-Modified", "last_modified")):
                value = next((v for k, v in headers.items() if k.lower() == header_name.lower()), "")
                if value:
                    feed_state[state_name] = value
        if status is None or status >= 400:
            gaps.append(f"RSS {label}: fetch status {status}; {body[:160]}")
            continue
        entries = parse_feed(body, final_url)
        if not entries:
            gaps.append(f"RSS {label}: no parseable RSS/Atom entries")
        for entry in entries:
            entry.update({"label": label, "category": str(feed.get("category") or "RSS/Atom"), "feed_url": final_url})
            all_records.append(entry)
    first_run = not state.get("initialized", False)
    for entry in all_records:
        if entry["id"] in seen:
            continue
        seen.add(entry["id"])
        if first_run and not backfill:
            continue
        reason = hype_reason(entry["clean_text"])
        if reason:
            gaps.append(f"RSS {entry['label']}: pre-filtered {reason}: {entry['title'][:100]}")
            continue
        new_records.append(entry)
    state.update({"seen_ids": list(seen), "initialized": True, "last_poll_at": dt.datetime.now(dt.timezone.utc).isoformat()})
    _save_state(state_path, state)
    return new_records, gaps


def append_handoff(records: list[dict], path: Path = DEFAULT_HANDOFF) -> int:
    """Durably append normalized new records for the heavier worker to consume."""
    if not records:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
    return len(records)


def claim_handoff(path: Path = DEFAULT_HANDOFF) -> tuple[list[dict], Path | None]:
    """Atomically claim a batch; an interrupted worker will retry its claim."""
    processing = path.with_suffix(path.suffix + ".processing")
    claimed = processing if processing.exists() else None
    if claimed is None and path.exists() and path.stat().st_size:
        try:
            path.replace(processing)
            claimed = processing
        except FileNotFoundError:
            return [], None
    if claimed is None:
        return [], None
    records = []
    for line in claimed.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("id"):
            records.append(item)
    return records, claimed


def acknowledge_handoff(claim_path: Path | None) -> None:
    if claim_path:
        try:
            claim_path.unlink()
        except FileNotFoundError:
            pass
