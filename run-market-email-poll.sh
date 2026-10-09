#!/usr/bin/env bash
set -euo pipefail

cd /home/pi/.openclaw/workspace/market-worker
umask 022

GWS="${GWS:-/home/pi/.npm-global/bin/gws}"
HANDOFF_DIR="${MARKET_HANDOFF_DIR:-/home/pi/.openclaw/workspace/handoffs/market-worker}"
OUT_DIR="${MARKET_EMAIL_OUT_DIR:-$HANDOFF_DIR/email-inbox}"
QUERY="${MARKET_EMAIL_QUERY:-newer_than:2d is:unread}"
MAX="${MARKET_EMAIL_MAX:-20}"
MARK_READ="${MARKET_EMAIL_MARK_READ:-1}"
POLLED_IDS_FILE="${MARKET_EMAIL_POLLED_IDS_FILE:-$HANDOFF_DIR/email-last-polled-ids.json}"
export GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND="${GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND:-file}"
export GOOGLE_WORKSPACE_CLI_CONFIG_DIR="${GOOGLE_WORKSPACE_CLI_CONFIG_DIR:-/home/pi/.config/gws/stocks}"

mkdir -p "$OUT_DIR"

python3 - "$OUT_DIR" "$GWS" "$QUERY" "$MAX" "$MARK_READ" "$POLLED_IDS_FILE" <<'PY'
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

out_dir = Path(sys.argv[1])
gws = sys.argv[2]
query = sys.argv[3]
max_results = int(sys.argv[4])
mark_read = sys.argv[5].lower() in {"1", "true", "yes"}
polled_ids_file = Path(sys.argv[6])


def run_gws_export() -> dict:
    result = subprocess.run(
        [gws, "auth", "export", "--unmasked"],
        text=True,
        capture_output=True,
        check=False,
        timeout=90,
    )
    if result.returncode != 0:
        print(result.stderr[-2000:] or result.stdout[-2000:], file=sys.stderr)
        raise SystemExit(result.returncode)
    stdout = "\n".join(line for line in result.stdout.splitlines() if not line.startswith("Using keyring backend"))
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        print(f"gws auth export returned non-JSON output: {exc}", file=sys.stderr)
        raise SystemExit(3)


def get_access_token() -> str:
    credentials = run_gws_export()
    refresh_token = credentials.get("refresh_token")
    client_id = credentials.get("client_id")
    client_secret = credentials.get("client_secret")
    if not refresh_token or not client_id or not client_secret:
        print("gws credentials are missing client_id, client_secret, or refresh_token", file=sys.stderr)
        raise SystemExit(2)
    data = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode()
    request = urllib.request.Request("https://oauth2.googleapis.com/token", data=data, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            token = json.load(response)
    except urllib.error.HTTPError as exc:
        print(exc.read().decode(errors="replace")[-2000:], file=sys.stderr)
        raise SystemExit(exc.code)
    access_token = token.get("access_token")
    if not access_token:
        print("OAuth refresh response did not include an access token", file=sys.stderr)
        raise SystemExit(2)
    return str(access_token)


def gmail_request(method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
    query_string = f"?{urllib.parse.urlencode(params)}" if params else ""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"https://gmail.googleapis.com/gmail/v1/{path}{query_string}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        print(exc.read().decode(errors="replace")[-2000:], file=sys.stderr)
        raise SystemExit(exc.code)


access_token = get_access_token()
profile = gmail_request("GET", "users/me/profile")
if profile.get("emailAddress") != "wbmccane.stocks@gmail.com":
    print(f"refusing to poll unexpected Gmail account: {profile.get('emailAddress')}", file=sys.stderr)
    raise SystemExit(2)

listing = gmail_request("GET", "users/me/messages", {"q": query, "maxResults": max_results})
(out_dir / "last-triage.json").write_text(json.dumps(listing, indent=2, sort_keys=True) + "\n", encoding="utf-8")

message_ids = []
for item in listing.get("messages", []):
    msg_id = item.get("id")
    if not msg_id:
        continue
    message_ids.append(msg_id)
    path = out_dir / f"{msg_id}.json"
    if path.exists():
        continue
    message = gmail_request("GET", f"users/me/messages/{urllib.parse.quote(msg_id)}", {"format": "full"})
    path.write_text(json.dumps(message, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(path)

polled_ids_file.write_text(json.dumps({"message_ids": message_ids}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

python3 ./market_email_processor.py

if [[ "$MARK_READ" =~ ^(1|true|yes)$ ]]; then
  python3 - "$POLLED_IDS_FILE" "$GWS" <<'PY'
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

polled_ids_file = Path(sys.argv[1])
gws = sys.argv[2]
try:
    payload = json.loads(polled_ids_file.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    payload = {}

marked = 0
export_result = subprocess.run(
    [gws, "auth", "export", "--unmasked"],
    text=True,
    capture_output=True,
    check=False,
    timeout=90,
)
if export_result.returncode != 0:
    print(export_result.stderr[-2000:] or export_result.stdout[-2000:], file=sys.stderr)
    raise SystemExit(export_result.returncode)
stdout = "\n".join(line for line in export_result.stdout.splitlines() if not line.startswith("Using keyring backend"))
credentials = json.loads(stdout)
data = urllib.parse.urlencode(
    {
        "client_id": credentials["client_id"],
        "client_secret": credentials["client_secret"],
        "refresh_token": credentials["refresh_token"],
        "grant_type": "refresh_token",
    }
).encode()
with urllib.request.urlopen(urllib.request.Request("https://oauth2.googleapis.com/token", data=data, method="POST"), timeout=30) as response:
    token = json.load(response)
access_token = token["access_token"]

for msg_id in payload.get("message_ids", []):
    request = urllib.request.Request(
        f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{urllib.parse.quote(msg_id)}/modify",
        data=json.dumps({"removeLabelIds": ["UNREAD"]}).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=90):
            pass
    except urllib.error.HTTPError as exc:
        print(exc.read().decode(errors="replace")[-2000:], file=sys.stderr)
        raise SystemExit(exc.code)
    marked += 1

print(f"marked_read={marked}")
PY
fi
