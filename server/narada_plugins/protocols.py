"""The five plugin protocols Narada talks to.

Every external SaaS that wants to plug into Narada implements ONE of
these (or two, for tools like Apollo that do both leads + sending).
The Narada core (`narada_core.py`) calls plugins through these
interfaces only — it never knows which provider is behind any call,
the marketer picks at campaign-creation time and the registry
resolves the slug to the plugin instance.

Why Protocol (PEP 544) instead of ABC: plugins can be plain functions
+ classes without needing to inherit from a base class — useful for
wrapping Composio session objects, which we don't subclass.

All methods take `member_email` as the first arg so per-member
credential lookup is mandatory. A plugin that ignores member_email
and uses a global API key would still work for v1 (single-tenant
prod) but break in multi-tenant OSS installs.
"""
from __future__ import annotations
from datetime import datetime
from typing import Protocol, runtime_checkable

from .types import (
    Lead, ICPFilters, VerifyResult, SendResult, Reply,
    DealData, Activity, PluginInfo,
)


# ─────────────────────────────────────────────────────────────────────
# 1. LeadSource — search for prospects matching an ICP
# ─────────────────────────────────────────────────────────────────────

@runtime_checkable
class LeadSource(Protocol):
    """Find prospects + their emails. May also verify (in which case
    the plugin should also implement Verifier; e.g. Prospeo does both)."""

    @classmethod
    def info(cls) -> PluginInfo: ...

    def is_available(self, member_email: str) -> bool:
        """True iff credentials for this plugin are configured for
        this member. Called before every campaign-creation flow that
        offers this plugin in a dropdown."""
        ...

    def search(self, member_email: str, icp: ICPFilters,
                count: int = 50) -> list[Lead]:
        """Return up to `count` leads matching the ICP. Plugins MUST
        respect `count` — running over is a fast way to bankrupt the
        marketer on per-credit pricing."""
        ...

    def find_email(self, member_email: str, first_name: str,
                    last_name: str, company_domain: str) -> str | None:
        """Lookup an email from name + company. Returns None if not
        found. Used when the LLM picks up a prospect from another
        source (LinkedIn URL, CSV upload) and we need the email."""
        ...

    def cost_estimate(self, action: str, count: int = 1) -> dict:
        """Estimate the credit/dollar cost of an action ahead of time.
        Return shape: {"credits": int, "approx_usd": float, "notes": str}.
        Used by the dashboard to show 'this search will cost ~$X'."""
        ...


# ─────────────────────────────────────────────────────────────────────
# 2. Verifier — confirm an email is deliverable
# ─────────────────────────────────────────────────────────────────────

@runtime_checkable
class Verifier(Protocol):

    @classmethod
    def info(cls) -> PluginInfo: ...

    def is_available(self, member_email: str) -> bool: ...

    def verify(self, member_email: str, email: str) -> VerifyResult:
        """Single-email verification. Caller batches if it wants
        bulk pricing; plugins should NOT silently batch upstream."""
        ...

    def cost_estimate(self, count: int = 1) -> dict: ...


# ─────────────────────────────────────────────────────────────────────
# 3. Sender — push the email + detect replies
# ─────────────────────────────────────────────────────────────────────

@runtime_checkable
class Sender(Protocol):

    @classmethod
    def info(cls) -> PluginInfo: ...

    def is_available(self, member_email: str) -> bool: ...

    def daily_send_cap(self, member_email: str) -> int:
        """How many emails can this sender push today for this member.
        Gmail Workspace = ~2000, Smartlead = 150000 etc. Narada core
        respects this when queueing sends; doesn't trust the sender
        to silently throttle (some don't, and the marketer's reputation
        eats the bill)."""
        ...

    def send(self, member_email: str, from_addr: str, to: str,
             subject: str, body: str,
             headers: dict | None = None,
             reply_to: str | None = None) -> SendResult: ...

    def detect_replies(self, member_email: str,
                        since: datetime) -> list[Reply]:
        """Pull any inbound replies received since `since`. Senders
        with native webhook support (Smartlead, Lemlist) cache these
        and return from cache; pure-API senders (Gmail) query live.
        Caller pages over time windows; plugins MUST NOT return
        unbounded result sets."""
        ...

    def supports_warmup(self) -> bool:
        """True iff the sender has built-in warmup (Smartlead, Lemlist,
        Instantly). False for raw email (Gmail, custom SMTP). Used by
        the dashboard to suggest pairing with a Warmup plugin."""
        ...


# ─────────────────────────────────────────────────────────────────────
# 4. CRM — sync hot replies + deal data
# ─────────────────────────────────────────────────────────────────────

@runtime_checkable
class CRM(Protocol):

    @classmethod
    def info(cls) -> PluginInfo: ...

    def is_available(self, member_email: str) -> bool: ...

    def upsert_contact(self, member_email: str, lead: Lead) -> str:
        """Upsert a contact, return the CRM's internal contact id.
        Plugins MUST dedup by email — multiple Narada campaigns to
        the same prospect should result in one CRM contact, not many."""
        ...

    def create_deal(self, member_email: str, contact_id: str,
                     deal: DealData) -> str:
        """Create a deal/opportunity attached to the contact. Return
        the deal id. Called when a reply is classified 'interested'."""
        ...

    def log_activity(self, member_email: str, contact_id: str,
                      activity: Activity) -> None:
        """Log an outbound send / inbound reply / note against the
        contact. Best-effort — failures don't block the send."""
        ...


# ─────────────────────────────────────────────────────────────────────
# 5. LinkedInChannel — DMs + connection requests + profile views
# ─────────────────────────────────────────────────────────────────────

@runtime_checkable
class LinkedInChannel(Protocol):
    """⚠️ LinkedIn outbound operates in LinkedIn's grey-zone TOS.
    All LinkedIn plugins must include a clear in-UI disclaimer; default-
    off, opt-in per campaign. We rate-limit aggressively (≤ 100
    connection requests/week, ≤ 50 DMs/day) regardless of plugin
    capacity, because that's the actual safe band for not getting
    a member's account suspended."""

    @classmethod
    def info(cls) -> PluginInfo: ...

    def is_available(self, member_email: str) -> bool: ...

    def send_connection_request(self, member_email: str,
                                 linkedin_url: str,
                                 note: str = "") -> str: ...

    def send_dm(self, member_email: str, linkedin_url: str,
                 body: str) -> str: ...

    def visit_profile(self, member_email: str,
                       linkedin_url: str) -> None:
        """Profile-view as an outreach signal (no-op for tools that
        don't support it; agencies use it to trigger 'who visited my
        profile' notifications in the prospect's LinkedIn)."""
        ...
