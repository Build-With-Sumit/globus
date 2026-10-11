"""Hermetic checks for the notification relay (POST /api/notify).

Two layers, both with an in-memory stand-in for the database:

  * globus_relay itself: the key lifecycle (mint, verify, revoke, expiry,
    chat scope, the empty-list-is-not-unscoped rule) and the rate limiter;
  * the real `globus_server.Handler` on a socket, because the contract that
    matters is what a service sees: no key -> 401, a key -> the send happens
    once with the right initiator, a key scoped to other chats -> 403.

Run with:  python tests/test_globus_relay.py
"""
import http.client
import json
import os
import sys
import threading
import types
from http.server import ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "server"))

os.environ.setdefault("SESSION_SECRET", "c" * 64)
os.environ.setdefault("SITE", "https://example.com")
os.environ["EMAIL_API_KEY"] = ""

# ── an in-memory globus_relay_keys ───────────────────────────────────────
ROWS = []          # dicts: id,key_hash,service_name,member_email,status,
                   #        allowed_chats(str|None),expired(int),label


def db_read(sql, params=()):
    low = " ".join(sql.split()).lower()
    if low.startswith("select id from globus_relay_keys where key_hash"):
        return [{"id": r["id"]} for r in ROWS if r["key_hash"] == params[0]]
    if low.startswith("select id, service_name, member_email, allowed_chats"):
        return [dict(r) for r in ROWS if r["key_hash"] == params[0]]
    return []


def db_write(sql, params=()):
    low = " ".join(sql.split()).lower()
    if low.startswith("insert into globus_relay_keys"):
        kh, svc, member, chats, expires, label = params
        ROWS.append({"id": len(ROWS) + 1, "key_hash": kh,
                     "service_name": svc, "member_email": member,
                     "status": "active", "allowed_chats": chats,
                     "expired": 1 if expires == "PAST" else 0,
                     "label": label})
    elif low.startswith("update globus_relay_keys set status='revoked'"):
        for r in ROWS:
            if low.endswith("key_hash=%s") and r["key_hash"] == params[0]:
                r["status"] = "revoked"
            elif low.endswith("where id=%s") and r["id"] == params[0]:
                r["status"] = "revoked"
            elif low.endswith("service_name=%s") and \
                    r["service_name"] == params[0]:
                r["status"] = "revoked"
    return True


_dbh = types.ModuleType("db_helpers")
_dbh.db_read = db_read
_dbh.db_write = db_write
_dbh.db_insert = lambda sql, params=(): 1
_dbh.cfg = lambda k, d="": d
_dbh.configure = lambda **kw: None
sys.modules["db_helpers"] = _dbh

import globus_relay as R             # noqa: E402
import globus_server as G            # noqa: E402

PASS, FAIL = [], []


def check(label, ok):
    (PASS if ok else FAIL).append(label)
    print(("  ok   " if ok else "  FAIL ") + label)


print("key lifecycle:")
key = R.create_relay_key("monitoring", "ops@example.com")
check("a key looks like sk-relay-<48 hex>",
      key.startswith("sk-relay-") and len(key) == len("sk-relay-") + 48)
check("only the hash is stored, never the plaintext",
      all(key not in json.dumps(r) for r in ROWS) and ROWS[0]["key_hash"] != key)
info = R.relay_key_verify(key)
check("a fresh key verifies and maps to its service + member",
      info and info["service_name"] == "monitoring"
      and info["member_email"] == "ops@example.com")
check("an unscoped key has allowed_chats None", info["allowed_chats"] is None)
check("a wrong key is refused", R.relay_key_verify("sk-relay-nope") is None)
check("an empty key is refused", R.relay_key_verify("") is None)

scoped = R.create_relay_key("cron", "ops@example.com", allowed_chats=[111, 222])
check("a scoped key stores its chats as strings",
      R.relay_key_verify(scoped)["allowed_chats"] == ["111", "222"])
deny_all = R.create_relay_key("locked", "ops@example.com", allowed_chats=[])
check("an EMPTY allow-list stays empty (deny all), not unscoped",
      R.relay_key_verify(deny_all)["allowed_chats"] == [])

expired = R.create_relay_key("old", "ops@example.com", expires_at="PAST")
check("an expired key is refused", R.relay_key_verify(expired) is None)

R.revoke_relay_key(key=key)
check("a revoked key is refused", R.relay_key_verify(key) is None)
check("revoking one key leaves its siblings working",
      R.relay_key_verify(scoped) is not None)
try:
    R.revoke_relay_key()
    check("revoke with no target raises", False)
except ValueError:
    check("revoke with no target raises", True)

print("rate limit:")
R.RELAY_RATE_MAX = 3
ok = [R.rate_limit_ok("k-test") for _ in range(5)]
check("the fourth message inside the window is refused",
      ok == [True, True, True, False, False])
check("another key has its own budget", R.rate_limit_ok("k-other") is True)
R.RELAY_RATE_MAX = 60

print("over HTTP (real Handler):")
SENT = []


def fake_send(email, chat_id, text, reply_to_message_id=None,
              parse_mode=None, initiator="globus-chat"):
    SENT.append((email, str(chat_id), text, initiator))
    return {"ok": True, "tg_message_id": 7}


G.send_via_member_bot = fake_send
srv = ThreadingHTTPServer(("127.0.0.1", 0), G.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
PORT = srv.server_address[1]


def post(body, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    h = {"Host": "example.com", "Content-Type": "application/json"}
    h.update(headers or {})
    conn.request("POST", "/api/notify", body=json.dumps(body), headers=h)
    r = conn.getresponse()
    raw = r.read().decode("utf-8", "replace")
    conn.close()
    try:
        return r.status, json.loads(raw)
    except ValueError:
        return r.status, raw


live = R.create_relay_key("alerts", "ops@example.com")
only = R.create_relay_key("alerts2", "ops@example.com", allowed_chats=["500"])

st, _ = post({"chat_id": 1, "text": "hi"})
check("no key is 401", st == 401)
st, _ = post({"chat_id": 1, "text": "hi"},
             {"Authorization": "Bearer sk-relay-wrong"})
check("a wrong key is 401", st == 401)
check("...and nothing was sent", SENT == [])

st, body = post({"chat_id": 42, "text": "disk 91%"},
                {"Authorization": f"Bearer {live}"})
check("a valid Bearer key sends (200)", st == 200 and body.get("ok") is True)
check("the send used the key's member and a relay initiator",
      SENT == [("ops@example.com", "42", "disk 91%", "relay:alerts")])
st, _ = post({"chat_id": 42, "text": "again"}, {"X-Relay-Key": live})
check("X-Relay-Key works too", st == 200 and len(SENT) == 2)

st, _ = post({"text": "no chat"}, {"Authorization": f"Bearer {live}"})
check("missing chat_id is 400", st == 400)
st, _ = post({"chat_id": 42, "text": "x" * 5000},
             {"Authorization": f"Bearer {live}"})
check("text over 4096 UTF-16 units is 413", st == 413)
st, _ = post({"chat_id": 999, "text": "nope"},
             {"Authorization": f"Bearer {only}"})
check("a chat outside the key's scope is 403", st == 403)
st, _ = post({"chat_id": 500, "text": "ok"},
             {"Authorization": f"Bearer {only}"})
check("a chat inside the key's scope sends", st == 200)
check("no send happened for the refused calls", len(SENT) == 3)

print()
print(f"{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
