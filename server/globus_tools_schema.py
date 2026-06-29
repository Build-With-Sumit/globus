"""Globus LLM tool schemas — extracted from lead_server.py 2026-06-28
as refactor slice #6u. Pure data: OpenAI-style tool definitions
(DeepSeek is OpenAI-API-compatible) that the chat + voice loops
hand to the LLM so it can call into the per-member toolset
(search_files, read_file, search_content, search_telegram, etc.).

The Python implementations of these tools (globus_read_file,
globus_search_files, ...) still live in lead_server for now —
they have heavy cross-deps on the DB layer, the OAuth refresh chain,
and the Drive/Gmail download helpers. This module is just the
schema list the LLM sees.

No runtime code. No deps. ~290 lines of static dicts.
"""
from __future__ import annotations


GLOBUS_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": (
                "Search the member's indexed files (Google Drive, Gmail, "
                "Obsidian zips, etc.) by filename keyword. Returns up to N "
                "matching files with id, filename, type, modified date, and "
                "character count. USE this whenever the member mentions a "
                "file by name, or asks about a topic specific enough that a "
                "single file likely answers it (e.g. 'the Q3 forecast', "
                "'EmpMonitor agent pipeline', 'the customer contract with "
                "Acme'). Then call read_file on the most relevant result to "
                "actually read it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "keyword(s) to match against filenames"},
                    "limit": {"type": "integer",
                              "description": "max results (default 5, max 20)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read the full text of one specific file by its file_id "
                "(obtained from search_files or list_recent_emails). "
                "Returns the file's content (possibly truncated at 50 000 "
                "chars) plus metadata. USE this after search_files / "
                "list_recent_emails identifies an item worth opening — do "
                "not guess or paraphrase from the subject/filename alone."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "integer",
                                "description": "file_id from a search_files or list_recent_emails result"},
                },
                "required": ["file_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_content",
            "description": (
                "Search INSIDE the content of files for a keyword. USE "
                "when search_files (filename-only) returns nothing or "
                "irrelevant results — e.g. looking for 'June' or 'Q3 "
                "2026' numbers that live INSIDE a sheet, not in its "
                "filename. Returns up to 5 files with a snippet showing "
                "the match in context, so you can pick the right one to "
                "read_file. Slower than search_files (scans up to 500 "
                "files) so don't use it for every query — only when "
                "filename search misses."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "keyword to search inside file content (min 3 chars)"},
                    "limit": {"type": "integer",
                              "description": "max results (default 5, max 10)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_recent_emails",
            "description": (
                "List the member's most recent Gmail messages — the inbox "
                "view, newest first. USE this for any question about "
                "recent email activity, what's in the inbox, what needs a "
                "response, who's been emailing about a topic, or to surface "
                "candidates worth reading. Returns subject, From header, "
                "date, file_id, and char_count for each message. Then call "
                "read_file on the ones worth opening (typically 2-5 of them)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "days_back": {"type": "integer",
                                  "description": "how many days of inbox to scan (default 7, max 90)"},
                    "limit": {"type": "integer",
                              "description": "max rows to return (default 30, max 100)"},
                    "sender_filter": {"type": "string",
                                      "description": "OPTIONAL: substring to match against the From header (e.g. \"sourav\", \"@globussoft.com\")"},
                    "subject_filter": {"type": "string",
                                       "description": "OPTIONAL: substring to match against the subject"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_whatsapp",
            "description": (
                "Search WhatsApp messages captured from the member's "
                "WhatsApp Web via the Globus Chrome extension. USE this "
                "for any question about WhatsApp chats / groups / contacts "
                "— what was discussed in a group, who sent what, what "
                "follow-ups are pending in a chat. Returns up to 20 "
                "matching messages with chat name, sender, timestamp, "
                "direction (in/out), and a body snippet. Body is already "
                "in the snippet — no separate read tool needed for most "
                "messages. Leave `query` empty to get the most-recent "
                "messages overall (good for 'what just came in')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "keyword(s) to search in the message body. Empty = recent overall."},
                    "chat_filter": {"type": "string",
                                    "description": "OPTIONAL: substring to match against the chat / group name (e.g. \"EmpMonitor\", \"Voice AI\")"},
                    "sender_filter": {"type": "string",
                                      "description": "OPTIONAL: substring to match a sender's name or phone number"},
                    "days_back": {"type": "integer",
                                  "description": "look back this many days (default 30, max 365)"},
                    "limit": {"type": "integer",
                              "description": "max rows to return (default 20, max 100)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_telegram",
            "description": (
                "Search Telegram messages captured from the member's "
                "personal Telegram account via the Telethon daemon. USE "
                "this for any question about Telegram chats / groups / "
                "channels — what was discussed in a group, what someone "
                "sent in a 1:1 DM, follow-ups pending. Returns up to 20 "
                "matching messages with chat name, chat type "
                "(private/group/supergroup/channel), sender, timestamp, "
                "direction (in/out), body snippet. Body is already in "
                "the snippet. Leave `query` empty for most-recent overall."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "keyword(s) to search in the message body. Empty = recent overall."},
                    "chat_filter": {"type": "string",
                                    "description": "OPTIONAL: substring to match against the chat/group/channel name (e.g. \"Globussoft AI\", \"Inside Sales\")"},
                    "sender_filter": {"type": "string",
                                      "description": "OPTIONAL: substring to match sender's name or @username"},
                    "chat_type": {"type": "string",
                                  "description": "OPTIONAL: filter by 'private', 'group', 'supergroup', 'channel', 'bot'"},
                    "days_back": {"type": "integer",
                                  "description": "look back this many days (default 30, max 365)"},
                    "limit": {"type": "integer",
                              "description": "max rows to return (default 20, max 100)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_telegram_via_bot",
            "description": (
                "Send a message via the member's Telegram BOT "
                "(@SumitGlobusBot) — write path, not read. Use ONLY for "
                "replies / posts the member has explicitly asked you to "
                "send. The bot can only send into chats it has been "
                "added to AND that are listed in allowed_send_chats. "
                "Every send is audited in globus_telegram_bot_sends. "
                "Default-deny: returns an error if the target chat isn't "
                "on the bot's allow-list."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "chat_id": {"type": "integer",
                                "description": "Telegram chat_id (numeric). Find it via search_telegram results."},
                    "text": {"type": "string",
                             "description": "Message body, plain text or markdown."},
                    "reply_to_message_id": {"type": "integer",
                                            "description": "OPTIONAL: tg_message_id to reply to (threads the message)"},
                    "parse_mode": {"type": "string",
                                   "description": "OPTIONAL: 'Markdown', 'MarkdownV2', or 'HTML'. Default: plain text."},
                },
                "required": ["chat_id", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_agent",
            "description": (
                "Trigger a background Hermes agent run on demand. Allow-"
                "listed agents: chief-of-staff (cross-cutting daily "
                "brief), drona (sales desk), nakul (infrastructure), "
                "vidur (ads watcher), vyas (content team daily report). "
                "Returns immediately — the agent runs async in the "
                "background and writes its brief to /opt/hermes/work/. "
                "Use ONLY when the member explicitly asks to run an "
                "agent (e.g. 'run chief of staff', 'fire vyas now', "
                "'have drona check the sales desk'). Don't pre-emptively "
                "trigger. Live progress is visible to the member in the "
                "Agent Activity Console below the chat transcript."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent": {"type": "string",
                              "description": "agent slug: 'chief-of-staff', 'drona', 'nakul', 'vidur', or 'vyas'"},
                },
                "required": ["agent"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_preference",
            "description": (
                "Save a long-lived preference / rule the member just "
                "told you to remember. USE this when they say things "
                "like 'remember that I prefer...', 'always use...', "
                "'never recommend...', 'save this in your memory: ...'. "
                "The rule gets injected at the top of your persona "
                "on every future call (voice + text). Do NOT use this "
                "for transient context within a single conversation — "
                "only for things the member explicitly asks to persist."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "rule_text": {"type": "string",
                                   "description": "the preference in the member's own words, max 500 chars"},
                },
                "required": ["rule_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_preferences",
            "description": (
                "Read back the saved preferences the member has asked you to "
                "remember (you already see them at the top of your persona, "
                "but call this when they ask 'what have you remembered?' or "
                "you need the rule_ids to call delete_preference). Returns "
                "an array of {id, rule_text, source, created_at}."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_preference",
            "description": (
                "Delete a previously-saved preference by its rule_id. USE "
                "when the member says 'forget that', 'remove that rule', "
                "'I changed my mind about X' etc. Call list_preferences "
                "first if you don't already have the rule_id from the "
                "persona's Member directives block (each entry there has "
                "an [id:N] prefix). Per-member ownership is enforced."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "rule_id": {"type": "integer",
                                "description": "the numeric id of the preference to delete"},
                },
                "required": ["rule_id"],
            },
        },
    },
]
