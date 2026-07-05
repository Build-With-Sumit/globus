"""Monday Sales CRM plugin — pipes Narada hot replies into the marketer's
monday.com CRM as contact items + deal items + updates.

monday.com is board-based: contacts and deals are items on two boards
the marketer picks (the Sales CRM product ships with default Contacts
and Deals boards). Column ids vary per account, so we discover each
board's columns at runtime (email / numbers / status / date /
connect-boards) — custom board layouts work without extra config.

Auth: personal API token (monday.com → avatar → Developers → My access
tokens), sent raw in the Authorization header. API: GraphQL, single
endpoint https://api.monday.com/v2.
Docs: https://developer.monday.com/api-reference/

Slug `monday_sales_crm`; the member pastes:
  - api_key            — personal API v2 token
  - contacts_board_id  — Contacts board id (bare id or pasted board URL)
  - deals_board_id     — Deals board id (bare id or pasted board URL)

Dedup: items_page_by_column_values on the contacts board's email column.
Activities post as Updates on the contact item. NOTE: monday's API
requires a paid plan — no free-tier API access.
"""
from __future__ import annotations
import json
import re
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from narada_creds import get_credential, has_credential, touch_last_used
from narada_plugins import register
from narada_plugins.types import (
    Activity, AuthMethod, DealData, Lead,
    PluginCategory, PluginInfo,
)


MONDAY_API_URL = "https://api.monday.com/v2"
MONDAY_TIMEOUT = 30

_BOARD_COLUMNS_Q = """
query ($ids: [ID!]) {
  boards (ids: $ids) { columns { id title type } }
}
"""

_SEARCH_BY_COLUMN_Q = """
query ($board_id: ID!, $columns: [ItemsPageByColumnValuesQuery!]) {
  items_page_by_column_values (board_id: $board_id, limit: 1,
                               columns: $columns) {
    items { id }
  }
}
"""

_CREATE_ITEM_M = """
mutation ($board_id: ID!, $item_name: String!, $column_values: JSON) {
  create_item (board_id: $board_id, item_name: $item_name,
               column_values: $column_values,
               create_labels_if_missing: true) { id }
}
"""

_CREATE_UPDATE_M = """
mutation ($item_id: ID!, $body: String!) {
  create_update (item_id: $item_id, body: $body) { id }
}
"""


def _board_id(raw: str) -> str:
    """Accept a bare numeric board id or a pasted board URL
    (https://yourco.monday.com/boards/1234567890)."""
    raw = (raw or "").strip().rstrip("/")
    if raw.isdigit():
        return raw
    m = re.search(r"boards/(\d+)", raw)
    if m:
        return m.group(1)
    m = re.search(r"\d{6,}", raw)
    return m.group(0) if m else ""


def _creds(member_email: str) -> tuple[str, str, str] | None:
    """Return (api_key, contacts_board_id, deals_board_id) or None."""
    c = get_credential(member_email, "monday_sales_crm")
    if not c:
        return None
    api_key = (c.get("api_key") or "").strip()
    contacts = _board_id(c.get("contacts_board_id") or "")
    deals = _board_id(c.get("deals_board_id") or "")
    if not api_key or not contacts or not deals:
        return None
    return api_key, contacts, deals


def _gql(member_email: str, query: str,
         variables: dict | None = None) -> dict:
    """POST a GraphQL query to monday.com. Returns the `data` dict, or
    {'error': ...}. Never raises. GraphQL errors come back HTTP 200
    with an `errors` array (or legacy `error_message`) — both are
    normalised into the error shape."""
    creds = _creds(member_email)
    if not creds:
        return {"error": "no Monday Sales CRM credential for this member "
                         "(api_key + contacts_board_id + deals_board_id "
                         "required)"}
    api_key, _, _ = creds
    payload = json.dumps({"query": query,
                          "variables": variables or {}}).encode("utf-8")
    req = Request(MONDAY_API_URL, data=payload, method="POST", headers={
        "Authorization": api_key,
        "Content-Type": "application/json",
        "User-Agent": "Narada/1.0 (Globus outbound agent)",
    })
    try:
        with urlopen(req, timeout=MONDAY_TIMEOUT) as r:
            text = r.read().decode("utf-8")
            resp = json.loads(text) if text else {}
    except HTTPError as e:
        body_txt = ""
        try:
            body_txt = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return {"error": f"HTTP {e.code}: {body_txt}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    finally:
        touch_last_used(member_email, "monday_sales_crm")
    if not isinstance(resp, dict):
        return {"error": f"unexpected response shape: {str(resp)[:120]}"}
    if resp.get("errors"):
        msgs = "; ".join(str(err.get("message") or err)
                         for err in resp["errors"][:3])
        return {"error": f"GraphQL: {msgs[:300]}"}
    if resp.get("error_message"):
        return {"error": str(resp["error_message"])[:300]}
    return resp.get("data") or {}


# ─────────────────────────────────────────────────────────────────────
# Column discovery — board layouts differ per account, so resolve
# column ids by type (+ title hint) instead of hardcoding them.
# ─────────────────────────────────────────────────────────────────────

_COLUMN_CACHE: dict[tuple[str, str], list[dict]] = {}


def _board_columns(member_email: str, board_id: str) -> list[dict]:
    """Fetch (and cache per-process) a board's columns [{id,title,type}]."""
    key = (member_email, board_id)
    cached = _COLUMN_CACHE.get(key)
    if cached is not None:
        return cached
    data = _gql(member_email, _BOARD_COLUMNS_Q, {"ids": [board_id]})
    if "error" in data:
        print(f"[narada/monday_sales_crm] board columns fetch failed "
              f"for board {board_id}: {data['error']}", flush=True)
        return []
    boards = data.get("boards") or []
    cols = (boards[0].get("columns") or []) if boards else []
    _COLUMN_CACHE[key] = cols
    return cols


def _find_column(cols: list[dict], col_type: str,
                 title_hints: tuple = (),
                 hint_required: bool = False) -> str:
    """Pick a column id by type, preferring titles containing a hint.
    With hint_required, return '' rather than guessing the first
    column of that type (avoids dumping data into a random column)."""
    typed = [c for c in cols if c.get("type") == col_type]
    for hint in title_hints:
        for c in typed:
            if hint in (c.get("title") or "").lower():
                return str(c.get("id") or "")
    if hint_required:
        return ""
    return str(typed[0].get("id") or "") if typed else ""


# ─────────────────────────────────────────────────────────────────────
# CRM implementation
# ─────────────────────────────────────────────────────────────────────

class MondaySalesCRM:

    @classmethod
    def info(cls) -> PluginInfo:
        return PluginInfo(
            name="monday_sales_crm",
            display_name="Monday Sales CRM",
            category=PluginCategory.CRM,
            auth_method=AuthMethod.API_KEY,
            requires_credentials=["api_key", "contacts_board_id",
                                  "deals_board_id"],
            homepage="https://monday.com/crm",
            docs_url="https://developer.monday.com/api-reference/",
            description=(
                "Pipe Narada hot replies into monday.com's Sales CRM as "
                "contact + deal items with reply notes as Updates. Paste "
                "a personal API token (avatar → Developers → My access "
                "tokens) plus your Contacts and Deals board ids — the "
                "board URL works too. Needs a paid monday plan for API "
                "access."
            ),
            free_tier=False,
        )

    def is_available(self, member_email: str) -> bool:
        return has_credential(member_email, "monday_sales_crm")

    def upsert_contact(self, member_email: str, lead: Lead) -> str:
        """Upsert by email (dedup via items_page_by_column_values on the
        board's email column). Returns the monday item id."""
        if not lead.email:
            return ""
        creds = _creds(member_email)
        if not creds:
            return ""
        _, contacts_board, _ = creds
        cols = _board_columns(member_email, contacts_board)
        email_col = _find_column(cols, "email", ("email",))
        if not email_col:
            # Without an email column we can't dedup — creating anyway
            # would duplicate the contact on every campaign.
            print(f"[narada/monday_sales_crm] contacts board "
                  f"{contacts_board} has no email column — cannot upsert",
                  flush=True)
            return ""
        # Search by email first (dedup).
        search = _gql(member_email, _SEARCH_BY_COLUMN_Q, {
            "board_id": contacts_board,
            "columns": [{"column_id": email_col,
                         "column_values": [lead.email]}],
        })
        if not search.get("error"):
            items = ((search.get("items_page_by_column_values") or {})
                     .get("items")) or []
            if items:
                return str(items[0].get("id") or "")
        # Create.
        name = f"{lead.first_name} {lead.last_name}".strip() or lead.email
        column_values: dict = {
            email_col: {"email": lead.email, "text": lead.email},
        }
        title_col = _find_column(cols, "text", ("title", "job"),
                                 hint_required=True)
        if title_col and lead.title:
            column_values[title_col] = lead.title
        company_col = _find_column(cols, "text", ("company", "account"),
                                   hint_required=True)
        if company_col and company_col != title_col and lead.company:
            column_values[company_col] = lead.company
        resp = _gql(member_email, _CREATE_ITEM_M, {
            "board_id": contacts_board,
            "item_name": name,
            "column_values": json.dumps(column_values),
        })
        if "error" in resp:
            print(f"[narada/monday_sales_crm] upsert_contact failed: "
                  f"{resp['error']}", flush=True)
            return ""
        return str((resp.get("create_item") or {}).get("id") or "")

    def create_deal(self, member_email: str, contact_id: str,
                    deal: DealData) -> str:
        """Create a deal item on the deals board, linked to the contact
        via the board's connect-boards column when one exists."""
        if not (contact_id and deal.title):
            return ""
        creds = _creds(member_email)
        if not creds:
            return ""
        _, _, deals_board = creds
        cols = _board_columns(member_email, deals_board)
        column_values: dict = {}
        link_col = _find_column(cols, "board_relation", ("contact",))
        if link_col:
            try:
                column_values[link_col] = {"item_ids": [int(contact_id)]}
            except (TypeError, ValueError):
                pass
        value_col = _find_column(cols, "numbers", ("value", "amount"))
        if value_col and deal.value:
            column_values[value_col] = str(deal.value)
        date_col = _find_column(cols, "date", ("close",),
                                hint_required=True)
        if date_col and deal.close_date:
            column_values[date_col] = {"date": deal.close_date[:10]}
        stage_col = _find_column(cols, "status", ("stage",),
                                 hint_required=True)
        if stage_col and deal.stage:
            column_values[stage_col] = {"label": deal.stage}
        resp = _gql(member_email, _CREATE_ITEM_M, {
            "board_id": deals_board,
            "item_name": deal.title,
            "column_values": json.dumps(column_values),
        })
        if "error" in resp:
            print(f"[narada/monday_sales_crm] create_deal failed: "
                  f"{resp['error']}", flush=True)
            return ""
        return str((resp.get("create_item") or {}).get("id") or "")

    def log_activity(self, member_email: str, contact_id: str,
                     activity: Activity) -> None:
        """Post an Update on the contact item. Best-effort."""
        if not contact_id:
            return
        body_text = (f"[Narada {activity.type}] {activity.subject}\n\n"
                     f"{activity.body}")[:10000]
        resp = _gql(member_email, _CREATE_UPDATE_M, {
            "item_id": contact_id,
            "body": body_text,
        })
        if "error" in resp:
            print(f"[narada/monday_sales_crm] log_activity failed: "
                  f"{resp['error']}", flush=True)


# Auto-register
try:
    register(MondaySalesCRM())
except Exception as _e:
    print(f"[narada/monday_sales_crm] register failed: "
          f"{type(_e).__name__}: {_e}", flush=True)
