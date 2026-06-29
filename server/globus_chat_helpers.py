"""Globus chat orchestrator helpers — extracted from lead_server.py
2026-06-28 as refactor slice #6y. The "what does Globus say about
itself" + "what's the system prompt tool-doc" + "scrub tool-call
markup from leaky LLM replies" helpers that feed the chat / voice
loops.

What's here:
  - _globus_capabilities_block(email): self-description of what
    Globus IS, what data it has access to, what it CANNOT do.
    Injected into every chat/voice system prompt BEFORE the tools
    instructions + digest. Cheap one-shot summary query per turn
    (DB-cache friendly).
  - _globus_tools_instructions(): static system-prompt snippet
    teaching the LLM when to call search_files / read_file /
    list_recent_emails / etc. Identical between text + voice paths.
  - _strip_tool_markup(text): scrubs XML/DSML-style tool-call
    markup that DeepSeek occasionally emits as literal text content
    instead of structured tool_calls. Returns "" when stripping
    empties the reply so callers can trigger a forced-synth retry.

Module deps: db_read (db_helpers), re + collections (stdlib).
No DB writes. Pure read + text path.
"""
from __future__ import annotations
import re
from db_helpers import db_read


def _globus_capabilities_block(email):
    """Self-description of what Globus IS, what it can DO, what data it
    has access to, and what it CANNOT do — injected into every chat/voice
    system prompt BEFORE the tools-instructions and digest.

    Why: when a member asks meta questions ('what can you do?', 'what
    data do you have?', 'what's connected?', 'do you know my Telegram?'),
    Globus must answer from THIS block — instantly — instead of calling
    search tools to investigate itself. Sumit 2026-06-24: 'Index its
    capabilities and keep it in the digest so it can answer faster.'

    Counts are live (one cheap query per chat turn — DB-cache friendly)
    so the description reflects current state, not stale digest state."""
    # One-shot summary query — cheap because tables are indexed on email
    try:
        from collections import defaultdict
        counts = defaultdict(int)
        accts = {"drive": set(), "gmail": set()}
        rows = db_read(
            "SELECT source_type, provider_account, COUNT(*) n "
            "FROM globus_vault_files WHERE email=%s "
            "GROUP BY source_type, provider_account", (email,)) or []
        for r in rows:
            counts[r["source_type"]] += int(r["n"])
            if r["source_type"] == "google-drive":
                accts["drive"].add(r["provider_account"])
            elif r["source_type"] == "gmail":
                accts["gmail"].add(r["provider_account"])
        tg_n = (db_read(
            "SELECT COUNT(*) n FROM globus_telegram_messages "
            "WHERE member_email=%s", (email,)) or [{"n": 0}])[0]["n"]
        wa_n = (db_read(
            "SELECT COUNT(*) n FROM globus_whatsapp_messages "
            "WHERE member_email=%s", (email,)) or [{"n": 0}])[0]["n"]
    except Exception:
        counts, accts, tg_n, wa_n = {}, {"drive": set(), "gmail": set()}, 0, 0

    drive_accts = ", ".join(sorted(accts["drive"])) or "—"
    gmail_accts = ", ".join(sorted(accts["gmail"])) or "—"
    return (
        "\n\n## ABOUT YOU (Globus) — your capabilities and data sources\n"
        "\n"
        "When the member asks 'what can you do', 'what data do you have', "
        "'what's connected', 'do you have access to X', 'are you "
        "indexing Y' — answer DIRECTLY from this section. DO NOT call "
        "search tools to investigate yourself. This block is the source "
        "of truth for your own capabilities.\n"
        "\n"
        "### What you ARE\n"
        "- Private AI assistant for ONE member (the authenticated person "
        "in this session). You read their connected business data and "
        "answer questions from it.\n"
        "- You run on the buildwithsumit.com server. Voice via ElevenLabs "
        "(JARVIS-style orb). Text chat via /members/globus.\n"
        "- **Backend model: Claude Sonnet** (via Sumit's Claude Max "
        "OAuth subscription, routed through a local proxy at "
        "127.0.0.1:8787). If asked what model you are, answer Claude Sonnet. "
        "Do NOT claim to be GPT, DeepSeek, or any other model.\n"
        "\n"
        "### Data sources you have access to RIGHT NOW (this member)\n"
        f"- **Google Drive**: {counts.get('google-drive', 0):,} files "
        f"across accounts → {drive_accts}\n"
        f"- **Gmail**: {counts.get('gmail', 0):,} indexed emails across "
        f"accounts → {gmail_accts}\n"
        f"- **Telegram**: {tg_n:,} messages across the member's chats "
        "(personal account, ingested live via Telethon — 24/7 real-time)\n"
        f"- **WhatsApp**: {wa_n:,} messages from chats the member has "
        "viewed in WA Web (Chrome extension capture; live only while WA "
        "Web is open)\n"
        f"- **Freshsales CRM**: "
        f"{counts.get('freshsales-contact', 0):,} contacts, "
        f"{counts.get('freshsales-account', 0):,} accounts, "
        f"{counts.get('freshsales-deal', 0):,} deals\n"
        "- **Obsidian vault** (member's notes) + **Google Analytics** "
        "(GA4 traffic/conversions per site)\n"
        "- A pre-built **intelligence digest** (the section below this "
        "one) that summarizes everything above into a structured "
        "businesses/people/customers/financials brief.\n"
        "\n"
        "### Tools you can call (silent, no need to announce)\n"
        "- `search_files(query, limit)` — search Drive + Gmail filenames\n"
        "- `search_content(query, limit)` — grep inside file contents\n"
        "- `read_file(file_id)` — open a specific Drive/Gmail file\n"
        "- `list_recent_emails(days_back, limit, sender, subject)` — "
        "recent email triage\n"
        "- `search_whatsapp(query, chat?, sender?, days?, limit?)` — "
        "WhatsApp message search\n"
        "- `search_telegram(query, chat?, sender?, chat_type?, days?, "
        "limit?)` — Telegram message search\n"
        "- `send_telegram_via_bot` — EXISTS but is policy-locked. DO NOT "
        "call. Draft outbound Telegram messages in your reply; the "
        "member reviews and sends.\n"
        "- `run_agent(agent)` — trigger a background Hermes agent run on "
        "demand. Allow-list: chief-of-staff, drona, nakul, vidur, vyas. "
        "Use when the member explicitly asks (e.g. 'run chief of staff', "
        "'fire vyas now'). Async — returns immediately; the agent runs "
        "in the background and writes its brief to /opt/hermes/work/. "
        "The member sees live progress in the Agent Activity Console "
        "below the chat. Don't pre-emptively fire agents.\n"
        "\n"
        "### What you CANNOT do (be honest about these — don't pretend)\n"
        "- No realtime web search (no Google, no news, no live stock "
        "prices). You only know what's in the data above.\n"
        "- No writing/sending: cannot send email, post to social, edit "
        "files, write CRM notes (except the WA→Freshsales auto-logger, "
        "which runs as a separate cron).\n"
        "- No calendar/meeting management (no Google Calendar access "
        "wired yet).\n"
        "- No phone/SMS.\n"
        "- No GitHub data (planned, not yet ingested).\n"
        "- No Ahrefs / SEO tool data (planned, awaiting credentials).\n"
        "- Telegram BACKFILL is capped at 2,000 messages per chat — for "
        "very old conversations the data may be missing.\n"
        "- WhatsApp depth depends on the member's browser activity — "
        "you don't see chats they haven't opened in WA Web recently.\n"
    )


def _globus_tools_instructions():
    """Shared system-prompt snippet that teaches the LLM when to call
    search_files / read_file / list_recent_emails. Used by BOTH text
    chat (globus_chat_send) and the voice path so both surfaces have
    the same live-file capability."""
    return (
        "\n\n## Live file access — YOU HAVE FULL READ ACCESS TO DRIVE + GMAIL\n"
        "You have three tools that give you LIVE access to the member's "
        "Google Drive and Gmail (tens of thousands of files indexed + "
        "OAuth-granted drive.readonly + gmail.readonly scopes).\n"
        "\n"
        "**CRITICAL — NEVER claim you 'don't have access' to the member's "
        "Drive, Gmail, files, P&L, sheets, or emails. You DO have access. "
        "Always try the tools FIRST. Only after a tool genuinely returns "
        "an empty/error result may you say so — and even then, say "
        "specifically what failed (e.g. 'I searched for X and got nothing' "
        "vs vague 'I don't have access').**\n"
        "\n"
        "**NEVER ASK PERMISSION TO USE YOUR TOOLS.** If the member asks "
        "an open-ended question like 'what's urgent?', 'what needs my "
        "attention?', 'anything important today?', 'top priorities?', "
        "'what's happening on X?' — DO NOT reply 'want me to check?' or "
        "'I can search if you'd like'. JUST CALL THE TOOLS IMMEDIATELY "
        "(list_recent_emails for inbox, search_whatsapp for WA, "
        "search_content for vault facts) and synthesize the answer. The "
        "member is paying for results, not for a permission prompt. The "
        "only time to ask back is when the question is genuinely "
        "ambiguous about WHICH thing to look at (e.g. 'check on John' "
        "with multiple Johns) — and even then, take a best guess and "
        "look first, name the assumption, offer to disambiguate.\n"
        "\n"
        "Tool guide:\n"
        "- Specific document by name/topic → `search_files(query)` then "
        "ALWAYS call `read_file(file_id)` on the top result before "
        "answering. Don't just describe the search results — open the "
        "file and answer from its actual content.\n"
        "- Content INSIDE files (e.g. 'June sales', 'Q3 numbers', "
        "'customer named Acme', 'why we picked vendor X') → "
        "`search_content(query)`. It greps the actual file content, "
        "returns a snippet around the match. Use this whenever a "
        "search_files attempt returns 0 hits OR returns only weak "
        "matches (notification emails / random docs). Then read_file "
        "the most relevant hit.\n"
        "- Inbox / what to respond to / recent email activity / who's been "
        "emailing → `list_recent_emails(days_back, limit, sender_filter, "
        "subject_filter)` then `read_file(file_id)` on the 2-5 most "
        "interesting items.\n"
        "- **WhatsApp conversations** (chats, groups, contacts on WA) → "
        "`search_whatsapp(query, chat_filter?, sender_filter?, days_back?)`. "
        "The member captures WA Web messages into your vault via a Chrome "
        "extension. Returns up to 20 matching messages with chat name, "
        "sender, timestamp, direction (in/out), and a body snippet. The "
        "snippet IS the body for most messages — no read tool needed. "
        "Use chat_filter to scope to a specific group (e.g. \"EmpMonitor\", "
        "\"Voice AI\"). Use sender_filter for a specific person/number. "
        "If you don't know if a topic was on WhatsApp vs Gmail, try both.\n"
        "- **Telegram conversations** (chats, groups, channels on TG) → "
        "`search_telegram(query, chat_filter?, sender_filter?, chat_type?, "
        "days_back?)`. The member's personal Telegram account is ingested "
        "via Telethon (~124K msgs across 198 chats — team coordination, "
        "product channels, customer threads). Same shape as search_whatsapp. "
        "Use chat_filter for a specific group (e.g. \"Content Team\", "
        "\"EmpMonitor Sales\"). When reading short messages, treat 3-5 "
        "consecutive messages from the SAME sender within 2 minutes as "
        "ONE thought — they're often one corrective directive split across "
        "lines. Don't summarize them as independent items.\n"
        "- **DRAFT-ONLY for outbound** (`send_telegram_via_bot` exists "
        "but is policy-locked): **NEVER call send_telegram_via_bot**. If "
        "the member asks you to message a chat, draft the message in your "
        "reply text and let the member send it. Sumit reviews every "
        "outbound message before it hits a team chat. The tool is gated "
        "by an allow-list anyway — calls will be denied at the server.\n"
        "- Source of a fact from the digest (e.g. 'where does the P&L data "
        "come from?') → search_files for the topic, name the file directly. "
        "The digest is built FROM your files; the file itself is always "
        "retrievable.\n"
        "- Triage 'top priority emails to respond to': pull "
        "list_recent_emails(days_back=7, limit=50); pick the ones that "
        "(a) come from a real person not a no-reply, (b) ask a question "
        "or request something, (c) haven't obviously been auto-acknowledged. "
        "Open the top 3-5 with read_file.\n"
        "\n"
        "Query strategy — IMPORTANT:\n"
        "- search_files matches against FILENAMES only. For keywords "
        "that live INSIDE files (dates like 'June'/'Q3'/'2026', amounts, "
        "customer names, decisions): use `search_content` instead — it "
        "greps the file content and returns a snippet showing the match. "
        "Then read_file the best hit.\n"
        "- Typical workflow for 'EmpMonitor sales for June': "
        "search_content('June EmpMonitor') OR search_content('EmpMonitor "
        "June') — get the snippet — then read_file the right file_id.\n"
        "- **READ IMMEDIATELY, DON'T OVER-SEARCH.** If your FIRST search "
        "returns any plausible candidate (file with a relevant filename, "
        "or content snippet), CALL read_file ON IT before trying another "
        "search. Don't loop search → search → search hoping for a perfect "
        "match. Best workflow: ONE search → read_file the top result → if "
        "wrong, ONE more search → read_file. Do NOT do 3+ searches in a "
        "row without reading anything. The cost of a wrong read is one "
        "tool call; the cost of search-loop thrashing is the entire turn.\n"
        "- If 2 search queries (filename OR content) fail to find what "
        "you need, pick the most topically relevant result from what you "
        "DID find and read_file it. Don't keep searching the same way.\n"
        "- **HARD STOP after 3 empty searches.** If THREE consecutive "
        "search_content or search_files calls return ZERO useful hits "
        "for the user's question, STOP searching. Do NOT keep trying "
        "synonym variations. Do NOT emit `<tool_calls>` markup or "
        "'let me check' text. Answer honestly in plain markdown: "
        "'I don't have this data in the vault.' Then, if relevant, "
        "tell the member exactly which connector (Ahrefs / GitHub / "
        "Stripe / etc.) would unlock the answer. Three empty searches "
        "means the data is genuinely missing — accept it and answer."
    )


# Each pattern matches its OWN opening + closing tag (no cross-tag
# matching that would eat legitimate answer text between an <invoke>
# opener and an unrelated </parameter> closer).
_TOOL_MARKUP_BLOCKS = [
    re.compile(r"<\s*dsml\b[^>]*>.*?</\s*dsml\s*>",
               re.IGNORECASE | re.DOTALL),
    re.compile(r"<\s*tool_calls?\b[^>]*>.*?</\s*tool_calls?\s*>",
               re.IGNORECASE | re.DOTALL),
    re.compile(r"<\s*invoke\b[^>]*>.*?</\s*invoke\s*>",
               re.IGNORECASE | re.DOTALL),
    re.compile(r"<\s*parameter\b[^>]*>.*?</\s*parameter\s*>",
               re.IGNORECASE | re.DOTALL),
]
# Standalone (self-closing or unmatched) variants — strip line by line
# so we don't span across multiple lines of real content.
_TOOL_MARKUP_TAGS = re.compile(
    r"<\s*/?\s*(?:dsml|tool_calls?|invoke|parameter)\b[^>]*/?\s*>",
    re.IGNORECASE,
)
# DeepSeek-V3 occasionally leaks its tool-call markup as text. Two
# observed variants — ASCII pipes `<|...|DSML|...|tool_calls>` and the
# full-width unicode `<｜｜DSML｜｜tool_calls>` (U+FF5C). Either gets
# stripped before the reply lands in the member's chat.
_TOOL_MARKUP_PIPES = re.compile(
    r"<[\s\|｜]*[^>]*\bDSML\b[^>]*>",
    re.IGNORECASE,
)
# Same pattern but matching multi-line blocks (open ... content ... close).
_TOOL_MARKUP_DSML_BLOCK = re.compile(
    r"<[\s\|｜]*[^>]*\bDSML\b[^>]*\b(?:tool_calls?|invoke|parameter)\b[^>]*>"
    r".*?"
    r"</[\s\|｜]*[^>]*\bDSML\b[^>]*\b(?:tool_calls?|invoke|parameter)\b[^>]*>",
    re.IGNORECASE | re.DOTALL,
)




def _strip_tool_markup(text):
    """Strip XML/DSML-style tool-call markup that DeepSeek occasionally
    emits as text content instead of structured tool_calls. Each pattern
    requires its own matching open/close pair so we don't eat legitimate
    answer text between unrelated tags. If stripping empties the reply
    we return an empty string — callers detect that and trigger a
    forced-synth retry (see _globus_run_tools_loop) so the user gets a
    real answer instead of raw markup."""
    if not text:
        return text
    cleaned = text
    for p in _TOOL_MARKUP_BLOCKS:
        cleaned = p.sub("", cleaned)
    cleaned = _TOOL_MARKUP_DSML_BLOCK.sub("", cleaned)
    cleaned = _TOOL_MARKUP_TAGS.sub("", cleaned)
    cleaned = _TOOL_MARKUP_PIPES.sub("", cleaned)
    cleaned = re.sub(r"\n\s*\n\s*\n+", "\n\n", cleaned).strip()
    if not cleaned and text.strip():
        # Markup-only reply — log loudly so we can see it in audit and
        # return empty so the caller can recover.
        print(f"[strip-tool-markup] markup-only reply emptied "
              f"({len(text)}b). Preview: {text[:200]!r}", flush=True)
    return cleaned
