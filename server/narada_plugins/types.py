"""Narada plugin dataclasses + result types.

Plugins implement the five protocols in `protocols.py`; these are the
shapes they hand back. All fields are forward-compatible — adding new
optional fields here doesn't break existing plugins.

No DB, no external deps; pure stdlib (dataclass + Enum). Lives at
the bottom of the import graph so every plugin + the core can pull
from here without circular risk.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


# ─────────────────────────────────────────────────────────────────────
# Lead-shape: what plugins return from a lead search / what we store
# in globus_narada_prospects.
# ─────────────────────────────────────────────────────────────────────

@dataclass
class Lead:
    """One prospect, normalised across lead-source plugins."""
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    company: str = ""
    company_domain: str = ""
    title: str = ""
    linkedin_url: str = ""
    # Plugin-specific raw payload — kept for debugging + future use,
    # not surfaced to the LLM unless the agent explicitly asks.
    source_metadata: dict = field(default_factory=dict)
    # The plugin that produced this lead (e.g. "prospeo", "apollo")
    # so callbacks know which credential to use for follow-up calls.
    source: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ICPFilters:
    """Structured ICP filter set. Plugins map these to their own
    parameter shape (Apollo's `q` syntax, Prospeo's filter objects, etc).
    All fields optional; plugins are expected to ignore filters they
    don't support and document the coverage gap in their docstring."""
    industries: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)           # e.g. ['CMO', 'Head of Marketing']
    seniority: list[str] = field(default_factory=list)       # ['c_suite', 'director', 'manager']
    locations: list[str] = field(default_factory=list)       # ISO countries / regions / city names
    company_size_min: int = 0
    company_size_max: int = 0                                # 0 = no max
    company_funding_stage: list[str] = field(default_factory=list)
    technologies: list[str] = field(default_factory=list)    # ['Stripe', 'HubSpot'] (intent signals)
    keywords: list[str] = field(default_factory=list)        # free-text signals
    # Plugin-specific escape hatch — anything we couldn't normalise.
    raw: dict = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────
# Verifier results
# ─────────────────────────────────────────────────────────────────────

class VerifyStatus(str, Enum):
    VALID = "valid"           # safe to send
    INVALID = "invalid"       # hard bounce expected
    RISKY = "risky"           # catch-all / disposable / role-based — send at own risk
    UNKNOWN = "unknown"       # provider couldn't determine
    ERROR = "error"           # API call itself failed


@dataclass
class VerifyResult:
    email: str
    status: VerifyStatus
    confidence: float = 0.0          # 0.0 - 1.0 if the provider gives one
    is_catch_all: bool = False
    is_disposable: bool = False
    is_role: bool = False             # e.g. info@, sales@
    raw: dict = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────
# Sender results
# ─────────────────────────────────────────────────────────────────────

class SendStatus(str, Enum):
    QUEUED = "queued"
    SENT = "sent"
    FAILED = "failed"
    SUPPRESSED = "suppressed"      # blocked by our local suppression list pre-send
    THROTTLED = "throttled"        # provider over daily cap


@dataclass
class SendResult:
    status: SendStatus
    message_id: str = ""             # RFC 822 Message-ID
    thread_id: str = ""              # provider thread id (Gmail-style)
    external_id: str = ""            # provider's own send id (Smartlead, etc.)
    error: str = ""                  # populated iff status == FAILED
    raw: dict = field(default_factory=dict)


@dataclass
class Reply:
    """One inbound reply, normalised across senders. Detected by the
    sender plugin's `detect_replies` method (Gmail pulls via API,
    Smartlead via webhook event, etc.)."""
    in_reply_to_message_id: str       # the Message-ID of the original send
    from_addr: str
    subject: str
    body: str
    received_at: str                  # ISO-8601
    thread_id: str = ""
    raw: dict = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────
# CRM types
# ─────────────────────────────────────────────────────────────────────

@dataclass
class DealData:
    title: str
    stage: str = ""                   # plugin maps to CRM-specific stage IDs
    value: float = 0.0
    currency: str = "USD"
    close_date: str = ""              # ISO date
    custom_fields: dict = field(default_factory=dict)


@dataclass
class Activity:
    type: str                         # "email_sent" | "reply_received" | "note"
    subject: str = ""
    body: str = ""
    occurred_at: str = ""             # ISO-8601
    custom_fields: dict = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────
# Plugin-discovery shape — what each plugin advertises so the
# /members/narada credentials page can render setup forms generically.
# ─────────────────────────────────────────────────────────────────────

class PluginCategory(str, Enum):
    LEAD_SOURCE = "lead_source"
    VERIFIER = "verifier"
    SENDER = "sender"
    CRM = "crm"
    LINKEDIN = "linkedin"
    ENRICHMENT = "enrichment"
    WARMUP = "warmup"


class AuthMethod(str, Enum):
    COMPOSIO = "composio"             # managed via Composio OAuth flow
    API_KEY = "api_key"               # paste API key into our form
    OAUTH_CUSTOM = "oauth_custom"     # we run our own OAuth dance


@dataclass
class PluginInfo:
    """What every plugin returns from its `info()` classmethod. Used
    by the registry to render the credentials page + the
    'choose tool for this campaign' dropdowns."""
    name: str                         # slug, e.g. "prospeo"
    display_name: str                 # "Prospeo"
    category: PluginCategory
    auth_method: AuthMethod
    requires_credentials: list[str] = field(default_factory=list)  # env/cfg keys for api_key auth
    composio_app: str = ""            # set iff auth_method == COMPOSIO
    homepage: str = ""
    description: str = ""
    free_tier: bool = False
    docs_url: str = ""
