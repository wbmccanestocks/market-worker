#!/usr/bin/env python3
"""Convert Gmail watch output into market-worker evidence packets.

This is read-only with respect to Gmail. It consumes JSON files already written
by `gws gmail +watch`, tracks processed message IDs locally, writes Markdown
evidence packets, and asks the existing signal queue to scan the newest packet.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import email.utils
import html
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
CODE_ROOT = ROOT
DATA_ROOT = Path(os.environ.get("MARKET_WORKER_DATA_DIR", str(ROOT.parent / "handoffs" / "market-worker")))
TZ = ZoneInfo("America/Chicago")
DEFAULT_INBOX = DATA_ROOT / "email-inbox"
DEFAULT_STATE = DATA_ROOT / "email-processor-state.json"
WATCHLIST = {
    "NVDA", "MSFT", "AAPL", "AMD", "AVGO", "GOOGL", "GOOG", "META", "TSLA",
    "AMZN", "LOW", "F", "FB", "QQQ", "SPY", "IWM", "XLK", "SMH", "XOM",
    "CVX", "COP", "OXY", "SLB", "LNG", "ENPH", "FSLR", "XLE", "XOP", "USO",
}
CATALYST_WORDS = {
    "earnings", "guidance", "revenue", "profit", "forecast", "analyst",
    "upgrade", "downgrade", "rating", "price target", "target", "oil", "gas",
    "opec", "inventory", "fed", "inflation", "yield", "tariff", "chips", "ai",
    "semiconductor", "solar", "energy", "data center", "surges", "jumps",
    "rallies", "premarket", "pre-market", "pre-bell", "before the open",
    "after hours", "postmarket", "after close",
}
BENZINGA_STRUCTURED_SECTIONS = {
    "benzinga wire",
    "calendar",
    "insider trades",
    "suggested peers",
}


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "processed_ids": []}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        backup = path.with_suffix(f".bad-{now_ct().strftime('%Y%m%dT%H%M%S')}.json")
        path.replace(backup)
        return {"version": 1, "processed_ids": []}
    state.setdefault("version", 1)
    state.setdefault("processed_ids", [])
    return state


def save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(".tmp")
    processed = list(dict.fromkeys(state.get("processed_ids", [])))[-2000:]
    state["processed_ids"] = processed
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def now_ct() -> dt.datetime:
    return dt.datetime.now(TZ)


def json_files(inbox: Path) -> list[Path]:
    if not inbox.exists():
        return []
    return sorted(
        (p for p in inbox.glob("*.json") if p.name != "last-triage.json"),
        key=lambda p: p.stat().st_mtime,
    )


def first_message_object(data: object) -> dict | None:
    if isinstance(data, dict):
        if "payload" in data and ("id" in data or "threadId" in data):
            return data
        if "body_text" in data and ("message_id" in data or "thread_id" in data):
            return {"_gws_read": data}
        for key in ("message", "gmailMessage", "data"):
            found = first_message_object(data.get(key))
            if found:
                return found
        for value in data.values():
            found = first_message_object(value)
            if found:
                return found
    if isinstance(data, list):
        for item in data:
            found = first_message_object(item)
            if found:
                return found
    return None


def headers_from(message: dict) -> dict[str, str]:
    headers = {}
    for header in message.get("payload", {}).get("headers", []) or []:
        name = str(header.get("name", "")).lower()
        if name:
            headers[name] = str(header.get("value", ""))
    return headers


def decode_body_data(value: str) -> str:
    if not value:
        return ""
    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")
    except Exception:
        return ""


def html_to_text(value: str) -> str:
    value = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    return html.unescape(value)


def strip_tags(value: str) -> str:
    return compact(html.unescape(re.sub(r"(?s)<[^>]+>", " ", value)))


def benzinga_price_rows(body_html: str, market_label: str = "summary") -> list[str]:
    if "emaillink.benzinga.com/ls/click" not in body_html:
        return []
    headings = [
        (match.start(), strip_tags(match.group(1)))
        for match in re.finditer(r"(?is)<h2\b[^>]*>(.*?)</h2>", body_html)
    ]
    row_pattern = re.compile(
        r"""(?is)
        <tr\b[^>]*>.*?
        <td\b[^>]*>\s*
        <a\s+href=["']https://emaillink\.benzinga\.com/ls/click[^"']*["'][^>]*>
        \s*([A-Z][A-Z0-9.$-]{0,9})\s*</a>\s*</td>\s*
        <td\b[^>]*>\s*(\$[^<]*\([+-]?[0-9.]+%\))\s*</td>
        .*?</tr>
        """,
        re.VERBOSE,
    )
    rows = []
    seen = set()
    for match in row_pattern.finditer(body_html):
        ticker = html.unescape(match.group(1)).strip().lstrip("$")
        if ticker not in WATCHLIST:
            continue
        price_change = strip_tags(match.group(2))
        anchor_pos = body_html.rfind("https://emaillink.benzinga.com/ls/click", match.start(), match.end())
        section_pos = anchor_pos if anchor_pos >= 0 else match.start()
        section = "Top Movers"
        for heading_pos, heading in headings:
            if heading_pos > section_pos:
                break
            if heading:
                section = heading
        if section.lower() == "indices":
            continue
        row = f"Benzinga {market_label} {section}: {ticker} {price_change}"
        if row not in seen:
            seen.add(row)
            rows.append(row)
    return rows


def benzinga_market_label(subject: str) -> str:
    lower = subject.lower()
    if "premarket" in lower or "pre-market" in lower:
        return "premarket"
    if "aftermarket" in lower or "after-market" in lower or "after market" in lower:
        return "aftermarket"
    return "summary"


def benzinga_structured_sections(body_html: str, per_section: int = 8) -> list[str]:
    if "emaillink.benzinga.com/ls/click" not in body_html:
        return []
    headings = [
        (match.start(), match.end(), strip_tags(match.group(1)))
        for match in re.finditer(r"(?is)<h[1-6]\b[^>]*>(.*?)</h[1-6]>", body_html)
    ]
    facts = []
    seen = set()
    for idx, (start, end, title) in enumerate(headings):
        if title.lower() not in BENZINGA_STRUCTURED_SECTIONS:
            continue
        section_end = headings[idx + 1][0] if idx + 1 < len(headings) else len(body_html)
        section_html = body_html[end:section_end]
        added = 0
        rows = [
            (strip_tags(match.group(1)), match.group(1))
            for match in re.finditer(r"(?is)<tr\b[^>]*>(.*?)</tr>", section_html)
        ]
        row_idx = 0
        while row_idx < len(rows):
            row, row_html = rows[row_idx]
            row_idx += 1
            if not row or row.lower() in BENZINGA_STRUCTURED_SECTIONS:
                continue
            if "emaillink.benzinga.com/ls/click" not in row_html:
                continue
            if row_idx < len(rows):
                next_row, next_html = rows[row_idx]
                if next_row.startswith("(") and "emaillink.benzinga.com/ls/click" in next_html:
                    row = f"{row} {next_row}"
                    row_idx += 1
            label = title if title.lower().startswith("benzinga") else f"Benzinga {title}"
            fact = f"{label}: {row[:500]}"
            if fact in seen:
                continue
            seen.add(fact)
            facts.append(fact)
            added += 1
            if added >= per_section:
                break
    return facts


def best_message_body(message: dict) -> str:
    body_text = str(message.get("body_text") or "")
    body_html = str(message.get("body_html") or "")
    if not body_html:
        return body_text
    html_text = html_to_text(body_html)
    if not html_text:
        return body_text
    if not body_text or len(compact(html_text)) > len(compact(body_text)) * 2:
        return "\n\n".join(part for part in (body_text, html_text) if part.strip())
    if set(tickers_in(html_text)) - set(tickers_in(body_text)):
        return "\n\n".join(part for part in (body_text, html_text) if part.strip())
    return body_text


def payload_text(payload: dict) -> str:
    chunks: list[str] = []
    mime_type = str(payload.get("mimeType", ""))
    body = payload.get("body", {}) or {}
    data = decode_body_data(str(body.get("data", "")))
    if data:
        chunks.append(html_to_text(data) if "html" in mime_type else data)
    for part in payload.get("parts", []) or []:
        if isinstance(part, dict):
            text = payload_text(part)
            if text:
                chunks.append(text)
    return "\n".join(chunks)


def compact(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def tickers_in(text: str) -> list[str]:
    upper = text.upper()
    return sorted(t for t in WATCHLIST if re.search(rf"\b{re.escape(t)}\b", upper))


def catalyst_hits(text: str) -> list[str]:
    lower = text.lower()
    return sorted(word for word in CATALYST_WORDS if word in lower)


def source_links(text: str, limit: int = 8) -> list[str]:
    links = []
    for match in re.finditer(r"https?://[^\s)>\]]+", text):
        url = match.group(0).rstrip(".,;\"'")
        if url not in links:
            links.append(url)
        if len(links) >= limit:
            break
    return links


def snippets(text: str, max_snips: int = 6) -> list[str]:
    terms = sorted(WATCHLIST | CATALYST_WORDS, key=len, reverse=True)
    out = []
    seen = set()
    clean = compact(text)
    for term in terms:
        for match in re.finditer(rf"(?i)\b{re.escape(term)}\b", clean):
            start = max(0, match.start() - 220)
            end = min(len(clean), match.end() + 360)
            snip = clean[start:end].strip()
            key = snip[:160].lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(snip)
            if len(out) >= max_snips:
                return out
    return out


def evidence_snippets(evidence_text: str, structured_facts: list[str] | None = None) -> list[str]:
    out = list(structured_facts or [])
    for snip in snippets(evidence_text, max_snips=max(0, 6 - len(out))):
        if snip not in out:
            out.append(snip)
    return out or [compact(evidence_text)[:700]]


def parse_received_date(value: str) -> str:
    if not value:
        return "unknown"
    parsed = email.utils.parsedate_to_datetime(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(TZ).isoformat()


def md_escape(value: str) -> str:
    return value.replace("\r", "").strip()


def safe_id(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return safe[:48] or "message"


def packet_for(message: dict, source_file: Path) -> tuple[str, str]:
    if "_gws_read" in message:
        return packet_for_gws_read(message["_gws_read"], source_file)

    message_id = str(message.get("id") or source_file.stem)
    headers = headers_from(message)
    subject = headers.get("subject", "(no subject)")
    sender = headers.get("from", "unknown")
    date = parse_received_date(headers.get("date", ""))
    body = payload_text(message.get("payload", {}) or {})
    structured = []
    evidence_text = "\n".join([subject, sender, body])
    tickers = tickers_in(evidence_text)
    catalysts = catalyst_hits(evidence_text)
    links = source_links(evidence_text)
    snips = evidence_snippets(evidence_text, structured)
    now = now_ct()
    stamp = now.strftime("%Y-%m-%dT%H%M%S")

    lines = [
        "source: market-worker",
        "status: proposed",
        "target: market-scout",
        "run_objective: gmail market alert evidence",
        f"generated_at: {now.isoformat()}",
        "model: deterministic gmail evidence extractor",
        "",
        "## email",
        f"- gmail_message_id: {message_id}",
        f"- source_file: {source_file.name}",
        f"- from: {md_escape(sender)}",
        f"- subject: {md_escape(subject)}",
        f"- received_at: {date}",
        f"- affected_tickers: {', '.join(tickers) if tickers else 'not detected'}",
        f"- catalyst_terms: {', '.join(catalysts) if catalysts else 'not detected'}",
        "",
        "## sources",
    ]
    if links:
        lines.extend(f"- {url}" for url in links)
    else:
        lines.append("- email body only; no URL detected")

    lines.extend(["", "## extracted_facts"])
    for snip in snips:
        snip_tickers = tickers_in(snip)
        lines.append(
            f"- affected_tickers: {', '.join(snip_tickers) if snip_tickers else 'watchlist/sector'}; "
            f"catalyst_type: email alert; time_horizon_relevance: needs scout verification; "
            f"risk_note: verify with market data and original email/source links before use; "
            f"source: gmail:{message_id}; fact: {md_escape(snip[:850])}"
        )

    lines.extend(
        [
            "",
            "## ranking_notes",
            "- Prioritize emails with explicit watchlist tickers plus analyst, earnings, guidance, oil/gas, AI/semiconductor, premarket, or after-hours language.",
            "- Treat promotional/newsletter copy as unverified until confirmed by price action and independent source links.",
            "",
            "## data_gaps",
        ]
    )
    if not tickers:
        lines.append("- no watchlist ticker detected")
    if not catalysts:
        lines.append("- no configured catalyst term detected")
    if not links:
        lines.append("- no source link detected in parsed email body")
    if tickers and catalysts and links:
        lines.append("- none detected by deterministic extractor")

    filename = f"{stamp}-market-packet-email-{safe_id(message_id)}.md"
    return filename, "\n".join(lines).rstrip() + "\n"


def packet_for_gws_read(message: dict, source_file: Path) -> tuple[str, str]:
    message_id = str(message.get("thread_id") or message.get("message_id") or source_file.stem)
    subject = str(message.get("subject") or "(no subject)")
    sender_obj = message.get("from")
    if isinstance(sender_obj, dict):
        sender = f"{sender_obj.get('name') or ''} <{sender_obj.get('email') or ''}>".strip()
    else:
        sender = str(sender_obj or "unknown")
    date = parse_received_date(str(message.get("date") or ""))
    body = best_message_body(message)
    body_html = str(message.get("body_html") or "")
    structured = [
        *benzinga_price_rows(body_html, benzinga_market_label(subject)),
        *benzinga_structured_sections(body_html),
    ]
    evidence_text = "\n".join([subject, sender, *structured, body])
    tickers = tickers_in(evidence_text)
    catalysts = catalyst_hits(evidence_text)
    links = source_links(evidence_text)
    snips = evidence_snippets(evidence_text, structured)
    now = now_ct()
    stamp = now.strftime("%Y-%m-%dT%H%M%S")

    lines = [
        "source: market-worker",
        "status: proposed",
        "target: market-scout",
        "run_objective: gmail market alert evidence",
        f"generated_at: {now.isoformat()}",
        "model: deterministic gmail evidence extractor",
        "",
        "## email",
        f"- gmail_message_id: {message_id}",
        f"- source_file: {source_file.name}",
        f"- from: {md_escape(sender)}",
        f"- subject: {md_escape(subject)}",
        f"- received_at: {date}",
        f"- affected_tickers: {', '.join(tickers) if tickers else 'not detected'}",
        f"- catalyst_terms: {', '.join(catalysts) if catalysts else 'not detected'}",
        "",
        "## sources",
    ]
    lines.extend(f"- {url}" for url in links) if links else lines.append("- email body only; no URL detected")
    lines.extend(["", "## extracted_facts"])
    for snip in snips:
        snip_tickers = tickers_in(snip)
        lines.append(
            f"- affected_tickers: {', '.join(snip_tickers) if snip_tickers else 'watchlist/sector'}; "
            f"catalyst_type: email alert; time_horizon_relevance: needs scout verification; "
            f"risk_note: verify with market data and original email/source links before use; "
            f"source: gmail:{message_id}; fact: {md_escape(snip[:850])}"
        )

    lines.extend(
        [
            "",
            "## ranking_notes",
            "- Prioritize emails with explicit watchlist tickers plus analyst, earnings, guidance, oil/gas, AI/semiconductor, premarket, or after-hours language.",
            "- Treat promotional/newsletter copy as unverified until confirmed by price action and independent source links.",
            "",
            "## data_gaps",
        ]
    )
    if not tickers:
        lines.append("- no watchlist ticker detected")
    if not catalysts:
        lines.append("- no configured catalyst term detected")
    if not links:
        lines.append("- no source link detected in parsed email body")
    if tickers and catalysts and links:
        lines.append("- none detected by deterministic extractor")

    filename = f"{stamp}-market-packet-email-{safe_id(message_id)}.md"
    return filename, "\n".join(lines).rstrip() + "\n"


def process(inbox: Path, state_path: Path, enqueue: bool) -> int:
    inbox.mkdir(parents=True, exist_ok=True)
    state = load_state(state_path)
    processed = set(state.get("processed_ids", []))
    wrote = 0
    skipped = 0

    for path in json_files(inbox):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"skip unreadable {path.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            skipped += 1
            continue
        message = first_message_object(data)
        if not message:
            print(f"skip no gmail message object: {path.name}", file=sys.stderr)
            skipped += 1
            continue
        message_id = str(message.get("id") or path.stem)
        if message_id in processed:
            skipped += 1
            continue
        filename, packet = packet_for(message, path)
        out = DATA_ROOT / filename
        out.write_text(packet, encoding="utf-8")
        processed.add(message_id)
        state["processed_ids"] = list(processed)
        state["last_packet"] = filename
        state["last_processed_at"] = now_ct().isoformat()
        wrote += 1
        print(out)
        if enqueue:
            subprocess.run(
                [sys.executable, str(CODE_ROOT / "market_signal_queue.py"), "enqueue-from-latest", "--limit", "1"],
                cwd=CODE_ROOT,
                check=False,
            )

    save_state(state_path, state)
    print(f"processed={wrote} skipped={skipped} inbox={inbox}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inbox", type=Path, default=DEFAULT_INBOX)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--no-enqueue", action="store_true")
    args = parser.parse_args()
    return process(args.inbox, args.state, enqueue=not args.no_enqueue)


if __name__ == "__main__":
    raise SystemExit(main())
