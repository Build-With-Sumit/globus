"""Narada core — campaign + prospect + send state machine.

This is the orchestrator that the LLM tools (and the dashboard) call.
It owns the writes to globus_narada_campaigns / _prospects / _sends /
_suppression. It DOES NOT talk to any external SaaS directly — every
external call goes through a plugin in `narada_plugins/` resolved via
the registry.

Lifecycle of a campaign:
    draft → reviewing → sending → done
            ↑               ↑
        copy gen,        approval,
        prospect         per-step
        enrichment       sequencing

Per-member ownership is enforced on every read + write — pass member
email as the first arg, and we WHERE-clause every query by it.
Suppression check happens at send-time, never trust the caller.

No external deps; pure stdlib + db_helpers.
"""
from __future__ import annotations
import json
from dataclasses import asdict
from datetime import datetime
from typing import Any

from db_helpers import db_read, db_write
from narada_plugins import (
    get_lead_source, get_verifier, get_sender, get_crm,
    PluginCategory,
)
from narada_plugins.types import (
    Lead, ICPFilters, VerifyStatus, SendStatus, Reply,
)


# ─────────────────────────────────────────────────────────────────────
# Suppression — checked before every send (default-deny on hit)
# ─────────────────────────────────────────────────────────────────────

def is_suppressed(member_email: str, target_email: str) -> bool:
    """True iff `target_email` is on this member's do-not-contact list.
    Single source of truth for 'should we send to this address?' —
    callers never bypass this."""
    if not target_email:
        return True
    rows = db_read(
        "SELECT 1 FROM globus_narada_suppression "
        "WHERE member_email=%s AND email=%s LIMIT 1",
        (member_email, target_email.lower()))
    return bool(rows)


def suppress(member_email: str, target_email: str,
              reason: str = "manual") -> None:
    """Add an email to the member's suppression list. Idempotent."""
    if not target_email:
        return
    db_write(
        "INSERT IGNORE INTO globus_narada_suppression "
        "(member_email, email, reason) VALUES (%s, %s, %s)",
        (member_email, target_email.lower(), reason))


def list_suppression(member_email: str, limit: int = 500) -> list[dict]:
    return db_read(
        "SELECT email, reason, added_at FROM globus_narada_suppression "
        "WHERE member_email=%s ORDER BY added_at DESC LIMIT %s",
        (member_email, limit)) or []


# ─────────────────────────────────────────────────────────────────────
# Campaign CRUD
# ─────────────────────────────────────────────────────────────────────

def create_campaign(member_email: str, *, name: str, product: str = "",
                     icp_description: str = "",
                     icp_filters: ICPFilters | None = None,
                     lead_source: str = "", verifier: str = "",
                     sender: str = "", sender_config: dict | None = None,
                     crm: str = "",
                     send_mode: str = "approve_each") -> int:
    """Create a draft campaign + return its id. Plugin slugs are
    validated against the registry — unknown slugs raise upfront
    rather than failing silently at send time."""
    for slug, getter, label in [
        (lead_source, get_lead_source, "lead_source"),
        (verifier, get_verifier, "verifier"),
        (sender, get_sender, "sender"),
        (crm, get_crm, "crm"),
    ]:
        if slug and not getter(slug):
            raise ValueError(
                f"campaign {label}={slug!r} is not a registered plugin. "
                f"Check installed plugins via list_plugins() or pass "
                f"empty string to skip this category.")
    db_write(
        "INSERT INTO globus_narada_campaigns "
        "(member_email, name, product, icp_description, icp_filters, "
        " lead_source, verifier, sender, sender_config, crm, send_mode) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (member_email, name, product, icp_description,
         json.dumps(asdict(icp_filters)) if icp_filters else None,
         lead_source, verifier, sender,
         json.dumps(sender_config) if sender_config else None,
         crm, send_mode))
    # Capture the new id (PyMySQL lastrowid pattern — re-query)
    rows = db_read(
        "SELECT id FROM globus_narada_campaigns "
        "WHERE member_email=%s ORDER BY id DESC LIMIT 1",
        (member_email,))
    return int(rows[0]["id"]) if rows else 0


def get_campaign(member_email: str, campaign_id: int) -> dict | None:
    rows = db_read(
        "SELECT * FROM globus_narada_campaigns "
        "WHERE id=%s AND member_email=%s",
        (campaign_id, member_email))
    return rows[0] if rows else None


def list_campaigns(member_email: str, *,
                     status: str | None = None,
                     limit: int = 50) -> list[dict]:
    if status:
        return db_read(
            "SELECT id, name, product, sender, lead_source, status, "
            "       stats, created_at, updated_at "
            "FROM globus_narada_campaigns "
            "WHERE member_email=%s AND status=%s "
            "ORDER BY created_at DESC LIMIT %s",
            (member_email, status, limit)) or []
    return db_read(
        "SELECT id, name, product, sender, lead_source, status, "
        "       stats, created_at, updated_at "
        "FROM globus_narada_campaigns "
        "WHERE member_email=%s ORDER BY created_at DESC LIMIT %s",
        (member_email, limit)) or []


def update_campaign_status(member_email: str, campaign_id: int,
                            status: str) -> None:
    db_write(
        "UPDATE globus_narada_campaigns SET status=%s "
        "WHERE id=%s AND member_email=%s",
        (status, campaign_id, member_email))


# ─────────────────────────────────────────────────────────────────────
# Prospect CRUD — campaign-scoped, dedup by (campaign, email)
# ─────────────────────────────────────────────────────────────────────

def add_prospects(member_email: str, campaign_id: int,
                    leads: list[Lead]) -> dict:
    """Insert leads into the campaign. Deduped via UNIQUE KEY on
    (campaign_id, email). Returns {added, skipped_dup, skipped_suppressed}."""
    added = skipped_dup = skipped_suppressed = 0
    for lead in leads:
        if not lead.email:
            skipped_dup += 1
            continue
        if is_suppressed(member_email, lead.email):
            skipped_suppressed += 1
            continue
        try:
            db_write(
                "INSERT INTO globus_narada_prospects "
                "(campaign_id, member_email, first_name, last_name, "
                " email, company, company_domain, title, linkedin_url, "
                " source_metadata) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (campaign_id, member_email,
                 lead.first_name[:120], lead.last_name[:120],
                 lead.email.lower()[:320],
                 lead.company[:255], lead.company_domain[:255],
                 lead.title[:255], lead.linkedin_url[:512],
                 json.dumps(lead.source_metadata) if lead.source_metadata else None))
            added += 1
        except Exception:
            skipped_dup += 1
    return {"added": added, "skipped_dup": skipped_dup,
            "skipped_suppressed": skipped_suppressed}


def get_prospect(member_email: str, prospect_id: int) -> dict | None:
    rows = db_read(
        "SELECT * FROM globus_narada_prospects "
        "WHERE id=%s AND member_email=%s",
        (prospect_id, member_email))
    return rows[0] if rows else None


def list_prospects(member_email: str, campaign_id: int,
                     status: str | None = None,
                     limit: int = 200) -> list[dict]:
    if status:
        return db_read(
            "SELECT * FROM globus_narada_prospects "
            "WHERE member_email=%s AND campaign_id=%s AND status=%s "
            "ORDER BY id ASC LIMIT %s",
            (member_email, campaign_id, status, limit)) or []
    return db_read(
        "SELECT * FROM globus_narada_prospects "
        "WHERE member_email=%s AND campaign_id=%s "
        "ORDER BY id ASC LIMIT %s",
        (member_email, campaign_id, limit)) or []


def update_prospect_status(member_email: str, prospect_id: int,
                             status: str) -> None:
    db_write(
        "UPDATE globus_narada_prospects SET status=%s "
        "WHERE id=%s AND member_email=%s",
        (status, prospect_id, member_email))


def set_prospect_verified(member_email: str, prospect_id: int,
                            verified: bool) -> None:
    new_status = "verified" if verified else "failed"
    db_write(
        "UPDATE globus_narada_prospects "
        "SET email_verified=%s, status=%s WHERE id=%s AND member_email=%s",
        (1 if verified else 0, new_status, prospect_id, member_email))


def set_prospect_enrichment(member_email: str, prospect_id: int,
                              enrichment: dict) -> None:
    db_write(
        "UPDATE globus_narada_prospects "
        "SET enrichment=%s, status=CASE WHEN status='verified' THEN "
        "'enriched' ELSE status END "
        "WHERE id=%s AND member_email=%s",
        (json.dumps(enrichment), prospect_id, member_email))


def set_prospect_copy(member_email: str, prospect_id: int,
                       variants: list[dict]) -> None:
    """variants is a list of {subject, body, score?, model?}."""
    db_write(
        "UPDATE globus_narada_prospects "
        "SET copy_variants=%s, status=CASE WHEN status IN "
        "('verified','enriched') THEN 'drafted' ELSE status END "
        "WHERE id=%s AND member_email=%s",
        (json.dumps(variants), prospect_id, member_email))


def approve_prospect_copy(member_email: str, prospect_id: int,
                           variant_idx: int) -> None:
    db_write(
        "UPDATE globus_narada_prospects "
        "SET approved_variant_idx=%s, status='approved' "
        "WHERE id=%s AND member_email=%s",
        (variant_idx, prospect_id, member_email))


# ─────────────────────────────────────────────────────────────────────
# Sending — wraps the per-call suppression check + audit row write
# ─────────────────────────────────────────────────────────────────────

def queue_send(member_email: str, campaign_id: int, prospect_id: int,
                from_addr: str, subject: str, body: str,
                sender_slug: str, step_idx: int = 0) -> int:
    """Insert a queued send row + return its id. Caller (or a worker)
    flips status to 'sent' after the sender plugin returns."""
    rows = db_read(
        "SELECT email FROM globus_narada_prospects "
        "WHERE id=%s AND member_email=%s",
        (prospect_id, member_email))
    if not rows:
        raise ValueError(f"prospect {prospect_id} not found")
    to_addr = rows[0]["email"]
    if is_suppressed(member_email, to_addr):
        db_write(
            "INSERT INTO globus_narada_sends "
            "(campaign_id, prospect_id, member_email, step_idx, sender, "
            " from_addr, to_addr, subject, body_preview, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'failed')",
            (campaign_id, prospect_id, member_email, step_idx,
             sender_slug, from_addr, to_addr, subject[:512],
             body[:2000]))
        return 0
    db_write(
        "INSERT INTO globus_narada_sends "
        "(campaign_id, prospect_id, member_email, step_idx, sender, "
        " from_addr, to_addr, subject, body_preview, status) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'queued')",
        (campaign_id, prospect_id, member_email, step_idx,
         sender_slug, from_addr, to_addr, subject[:512], body[:2000]))
    rows = db_read(
        "SELECT id FROM globus_narada_sends "
        "WHERE member_email=%s ORDER BY id DESC LIMIT 1",
        (member_email,))
    return int(rows[0]["id"]) if rows else 0


def mark_send_sent(send_id: int, message_id: str = "",
                    thread_id: str = "", external_id: str = "") -> None:
    db_write(
        "UPDATE globus_narada_sends "
        "SET status='sent', message_id=%s, thread_id=%s, "
        "    external_id=%s, sent_at=NOW() "
        "WHERE id=%s",
        (message_id[:255], thread_id[:255], external_id[:255], send_id))


def mark_send_failed(send_id: int, error: str = "") -> None:
    db_write(
        "UPDATE globus_narada_sends SET status='failed', "
        "    body_preview=CONCAT(SUBSTRING(IFNULL(body_preview,''), 1, 1500), "
        "                        ' [error: ', SUBSTRING(%s, 1, 300), ']') "
        "WHERE id=%s",
        (error, send_id))


def record_reply(send_id: int, classification: str, body: str,
                  received_at: datetime | None = None) -> None:
    db_write(
        "UPDATE globus_narada_sends "
        "SET status='replied', reply_classification=%s, "
        "    reply_body=%s, reply_received_at=COALESCE(%s, NOW()) "
        "WHERE id=%s",
        (classification, body[:5000], received_at, send_id))


# ─────────────────────────────────────────────────────────────────────
# Stats — campaign-scoped aggregates for the dashboard
# ─────────────────────────────────────────────────────────────────────

def campaign_stats(member_email: str, campaign_id: int) -> dict:
    """Live per-campaign aggregate counts. Called from the dashboard
    poll + the LLM tool `narada_campaign_stats`."""
    prospects = db_read(
        "SELECT status, COUNT(*) AS n FROM globus_narada_prospects "
        "WHERE member_email=%s AND campaign_id=%s GROUP BY status",
        (member_email, campaign_id)) or []
    sends = db_read(
        "SELECT status, COUNT(*) AS n FROM globus_narada_sends "
        "WHERE member_email=%s AND campaign_id=%s GROUP BY status",
        (member_email, campaign_id)) or []
    by_classification = db_read(
        "SELECT reply_classification AS k, COUNT(*) AS n "
        "FROM globus_narada_sends "
        "WHERE member_email=%s AND campaign_id=%s "
        "  AND reply_classification IS NOT NULL "
        "GROUP BY reply_classification",
        (member_email, campaign_id)) or []
    return {
        "prospects_by_status": {r["status"]: int(r["n"]) for r in prospects},
        "sends_by_status":     {r["status"]: int(r["n"]) for r in sends},
        "replies_by_class":    {r["k"]: int(r["n"]) for r in by_classification},
    }


# ─────────────────────────────────────────────────────────────────────
# Shared helpers — used by both the HTTP server and the LLM tool
# dispatcher (kept here so the two entry points can't drift)
# ─────────────────────────────────────────────────────────────────────

def sender_config_of(camp: dict) -> dict:
    """campaign.sender_config comes back from MySQL as a JSON string (or
    None); normalise to a dict so the send paths can read from_addr."""
    sc = camp.get("sender_config")
    if isinstance(sc, str) and sc.strip():
        try:
            sc = json.loads(sc)
        except Exception:
            sc = None
    return sc if isinstance(sc, dict) else {}


def member_send_accounts(member_email: str) -> list:
    """Google accounts this member can send from (gmail.send scope),
    newest connection first — feeds the campaign send-from picker."""
    rows = db_read(
        "SELECT provider_account FROM globus_oauth_connections "
        "WHERE email=%s AND provider='google' AND "
        "scopes LIKE '%%gmail.send%%' ORDER BY updated_at DESC",
        (member_email,)) or []
    out = []
    for r in rows:
        a = (r.get("provider_account") or "").strip().lower()
        if a and a not in out:
            out.append(a)
    return out


def parse_pasted_leads(text: str) -> list:
    """Parse pasted leads into Lead objects — bring-your-own-leads import.
    One lead per line; accepts a bare email, 'First Last <email>', or comma
    fields in any order (the token with an @ is the email; the rest map to
    first/last/company/title). Lines with no email are skipped."""
    import re
    from narada_plugins.types import Lead
    email_re = re.compile(
        r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
    leads = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = email_re.search(line)
        if not m:
            continue
        addr = m.group(0).lower()
        parts = [p.strip() for p in line.split(",")]
        rest = [p for p in parts if not email_re.search(p)]
        first = last = company = title = ""
        if rest:
            first = rest[0]
            last = rest[1] if len(rest) > 1 else ""
            company = rest[2] if len(rest) > 2 else ""
            title = rest[3] if len(rest) > 3 else ""
        elif "<" in line:                       # "First Last <email>"
            np = line.split("<", 1)[0].strip().split()
            first = np[0] if np else ""
            last = " ".join(np[1:]) if len(np) > 1 else ""
        leads.append(Lead(
            first_name=first[:120], last_name=last[:120], email=addr,
            company=company[:255], company_domain=addr.split("@")[-1][:255],
            title=title[:255], source="import"))
    return leads
