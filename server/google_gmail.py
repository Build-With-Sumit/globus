"""Gmail API client + body extractor + RFC-2822 date parser.

Stdlib only. The DB-touching parts (vault_files_upsert, write_extracted_file)
are reused from google_drive — the on-disk shape and indexing are identical
between Drive docs and Gmail messages (both are "one document, one row").

Sync orchestration lives in `sync_gmail.py`.

Constants:
  GMAIL_MAX_MESSAGES   — hard ceiling per full sync (50K)
  GMAIL_PAGE_SIZE      — Gmail's list API max page (500)
  GMAIL_PER_BODY_CHARS — cap per-message body when extracting (50K chars)
"""
from __future__ import annotations
import base64
import json
import re
import urllib.parse
from datetime import datetime, timezone
from urllib.request import Request, urlopen


GMAIL_API = "https://gmail.googleapis.com/gmail/v1"

GMAIL_MAX_MESSAGES = 50_000   # hard ceiling per full sync
GMAIL_PAGE_SIZE = 500         # max page size Gmail accepts
GMAIL_PER_BODY_CHARS = 50_000 # cap each email body when extracting


# ─────────────────────────────────────────────────────────────────────
# Gmail API client (paginated)
# ─────────────────────────────────────────────────────────────────────

def gmail_list_messages(access_token, query, max_results=GMAIL_MAX_MESSAGES,
                         page_size=GMAIL_PAGE_SIZE):
    """Page through Gmail search results up to max_results total.
    Returns a list of stubs `[{"id": "...", "threadId": "..."}, ...]`.
    Gmail caps page size at 500; for >500 we paginate via nextPageToken."""
    out = []
    page_token = None
    while len(out) < max_results:
        remaining = max_results - len(out)
        size = min(page_size, remaining)
        url = (f"{GMAIL_API}/users/me/messages"
               f"?q={urllib.parse.quote(query)}&maxResults={size}")
        if page_token:
            url += f"&pageToken={urllib.parse.quote(page_token)}"
        req = Request(url, headers={"Authorization": "Bearer " + access_token})
        with urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
        out.extend(data.get("messages") or [])
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return out


def gmail_get_message(access_token, message_id):
    """Fetch the full message payload (headers + multipart body parts)."""
    url = f"{GMAIL_API}/users/me/messages/{message_id}?format=full"
    req = Request(url, headers={"Authorization": "Bearer " + access_token})
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def gmail_extract_body_text(payload):
    """Recursively pull the best text body from a Gmail multipart payload.
    Prefers text/plain; falls back to text/html with tag-strip. Used by
    both the full and delta sync paths so the disk-cached body shape stays
    identical regardless of which path created it."""
    if not payload:
        return ""
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        data = (payload.get("body") or {}).get("data")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode(
                "utf-8", errors="replace")
    if mime == "text/html":
        data = (payload.get("body") or {}).get("data")
        if data:
            html = base64.urlsafe_b64decode(data + "==").decode(
                "utf-8", errors="replace")
            return re.sub(r"<[^>]+>", " ", html)
    parts = payload.get("parts") or []
    for p in parts:
        text = gmail_extract_body_text(p)
        if text:
            return text
    return ""


# ─────────────────────────────────────────────────────────────────────
# RFC-2822 date parser — used to fill `modified_at` so the inbox view
# can ORDER BY date DESC. PyMySQL needs a real datetime, not the raw
# header string (MySQL strict mode rejects it).
# ─────────────────────────────────────────────────────────────────────

def parse_email_date(s):
    """RFC-2822 'Sun, 21 Jun 2026 19:31:30 +0000' → naive UTC datetime.
    Returns None for empty/unparseable inputs (so one weird Date: header
    doesn't bury an otherwise-good row in the inbox tool)."""
    if not s:
        return None
    if isinstance(s, datetime):
        return s.replace(tzinfo=None) if s.tzinfo else s
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(str(s).strip())
        if dt is None:
            return None
        if dt.tzinfo:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except (TypeError, ValueError, IndexError):
        return None
