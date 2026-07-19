"""Org-portal HTML pages for the optional ORG-ONLY employee portals
("Globus for Organizations" — see org_db.py).

Pure functions, zero DB access; chrome (esc) from html_chrome. These are the
org-host equivalents of members_auth_html.login_html / code_html and the
members landing — branded to the org and wired to the org login flow, which
the do_GET/do_POST host gate routes on an org host.

Ships login (email code + optional Google), home, chat, self-connect, the
admin console (sharing grants + team/roles) and the pre-auth legal pages.
The grant-filtered shared-agents dashboard is not wired yet — org_home_html
degrades to a friendly placeholder when `cards_html` is empty.
"""
from __future__ import annotations
import html_chrome
from html_chrome import esc


_GOOGLE_SVG = (
    "<svg width=\"18\" height=\"18\" viewBox=\"0 0 18 18\" xmlns=\"http://www.w3.org/2000/svg\">"
    "<path d=\"M17.64 9.2c0-.637-.057-1.251-.164-1.84H9v3.481h4.844a4.14 4.14 0 0 1-1.796 2.716v2.258h2.908c1.702-1.567 2.684-3.875 2.684-6.615z\" fill=\"#4285F4\"/>"
    "<path d=\"M9 18c2.43 0 4.467-.806 5.956-2.18l-2.908-2.259c-.806.54-1.837.86-3.048.86-2.344 0-4.328-1.584-5.036-3.711H.957v2.332A8.997 8.997 0 0 0 9 18z\" fill=\"#34A853\"/>"
    "<path d=\"M3.964 10.71A5.41 5.41 0 0 1 3.682 9c0-.593.102-1.17.282-1.71V4.958H.957A8.996 8.996 0 0 0 0 9c0 1.452.348 2.827.957 4.042l3.007-2.332z\" fill=\"#FBBC05\"/>"
    "<path d=\"M9 3.58c1.321 0 2.508.454 3.44 1.345l2.582-2.58C13.463.891 11.426 0 9 0A8.997 8.997 0 0 0 .957 4.958L3.964 7.29C4.672 5.163 6.656 3.58 9 3.58z\" fill=\"#EA4335\"/>"
    "</svg>")


def _org_name(org):
    return esc((org or {}).get("name") or (org or {}).get("slug") or "Your workspace")


def _org_page(org, title, body):
    """Globus-branded page chrome for the ORG portal — the tenant's own branding,
    never the host site's. Reuses the shared stylesheet (neutral styling) but its
    own header + title. Brand: '<Org> Globus' linking to the org home."""
    nm = (org or {}).get("name") or (org or {}).get("slug") or ""
    brand = ((esc(nm) + " ") if nm else "") + "<strong>Globus</strong>"
    site = getattr(html_chrome, "_SITE", "") or ""
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"UTF-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">"
        "<title>" + title + "</title>"
        "<link rel=\"stylesheet\" href=\"" + site + "/styles.css\"></head><body>"
        "<header class=\"site-header\"><div class=\"container nav\">"
        "<a class=\"brand\" href=\"/members/globus\">"
        "<span class=\"brand-mark\">&lt;/&gt;</span> " + brand + "</a>"
        "</div></header><main>" + body + "</main></body></html>")


def org_login_html(org, message="", show_google=False):
    """Org sign-in page. Email-OTP is the primary (and, for tenants not on a
    Google Workspace, the only) method — a 6-digit code to the employee's
    company mailbox. The 'Continue with Google' button is shown ONLY when the
    tenant's identity provider is actually Google (show_google=True); otherwise
    it's hidden so cPanel/IMAP staff aren't sent down a broken Google path."""
    name = _org_name(org)
    note = ("<p class=\"form-note\" style=\"color:#dc2626\">" + esc(message)
            + "</p>") if message else ""
    google_section = ""
    if show_google:
        google_btn = (
            "<a href=\"/members/login/google/start\" class=\"btn btn-lg\" "
            "style=\"display:inline-flex;align-items:center;gap:.6rem;background:#fff;"
            "color:#1f1f1f;border:1px solid #dadce0;padding:.7rem 1.4rem;"
            "border-radius:8px;text-decoration:none;font-weight:500\">"
            + _GOOGLE_SVG + "Continue with Google</a>")
        divider = (
            "<div style=\"display:flex;align-items:center;gap:.8rem;margin:1.6rem auto;"
            "max-width:340px;color:#9ca3af;font-size:.85rem\">"
            "<hr style=\"flex:1;border:none;border-top:1px solid #e5e7eb\">"
            "<span>or sign in with email</span>"
            "<hr style=\"flex:1;border:none;border-top:1px solid #e5e7eb\"></div>")
        google_section = "<div style=\"margin:1rem 0 0\">" + google_btn + "</div>" + divider
    body = ("<section class=\"section\"><div class=\"container narrow center\">"
            "<span class=\"eyebrow\">" + name + " · Globus</span>"
            "<h1>Sign in to " + name + " Globus</h1>"
            "<p class=\"lead\">Your team AI workspace. Sign in with your work email.</p>"
            + google_section +
            "<form method=\"POST\" action=\"/members/login\" class=\"signup center\" "
            "style=\"justify-content:center\">"
            "<input type=\"email\" name=\"email\" required placeholder=\"you@yourcompany.com\" "
            "aria-label=\"Work email\">"
            "<button class=\"btn btn-primary btn-lg\" type=\"submit\">Send code</button></form>"
            "<p class=\"muted small\" style=\"margin-top:.6rem\">We'll email a 6-digit code "
            "to your company inbox. Only verified company email addresses can sign in.</p>"
            + note +
            "</div></section>")
    return _org_page(org, name + " · Globus", body)


def org_code_html(org, email, message="", ok=False):
    name = _org_name(org)
    color = "#16a34a" if ok else "#dc2626"
    note = ("<p class=\"form-note\" style=\"color:" + color + "\">" + esc(message)
            + "</p>") if message else ""
    body = ("<section class=\"section\"><div class=\"container narrow center\">"
            "<span class=\"eyebrow\">" + name + " · Globus</span>"
            "<h1>Check your email</h1>"
            "<p class=\"lead\">Enter the 6-digit code we sent to <strong>"
            + esc(email) + "</strong>.</p>"
            "<form method=\"POST\" action=\"/members/verify\" class=\"signup center\" "
            "style=\"justify-content:center\">"
            "<input type=\"hidden\" name=\"email\" value=\"" + esc(email) + "\">"
            "<input type=\"text\" name=\"code\" inputmode=\"numeric\" pattern=\"[0-9]*\" "
            "maxlength=\"6\" required placeholder=\"123456\" aria-label=\"6-digit code\" "
            "autocomplete=\"one-time-code\">"
            "<button class=\"btn btn-primary btn-lg\" type=\"submit\">Verify</button></form>"
            + note +
            "<p class=\"muted small\"><a href=\"/members/globus\">Use a different email</a></p>"
            "</div></section>")
    return _org_page(org, name + " · Globus", body)


def org_home_html(org, email, cards_html="", is_admin=False):
    """Authenticated org landing. `cards_html` is the (grant-filtered) agents
    dashboard. Admins additionally get a 'Sharing' link to the admin console."""
    name = _org_name(org)
    admin_link = ("<a class=\"btn\" href=\"/members/globus/admin\">Sharing</a>"
                  if is_admin else "")
    body = ("<section class=\"section\"><div class=\"container\">"
            "<div style=\"display:flex;justify-content:space-between;align-items:center;"
            "flex-wrap:wrap;gap:1rem\">"
            "<div><span class=\"eyebrow\">" + name + " · Globus</span>"
            "<h1 style=\"margin:.2rem 0 0\">Welcome</h1>"
            "<p class=\"muted small\" style=\"margin:.3rem 0 0\">Signed in as "
            + esc(email) + "</p></div>"
            "<div style=\"display:flex;gap:.5rem;flex-wrap:wrap\">"
            "<a class=\"btn btn-primary\" href=\"/members/globus/chat\">Chat</a>"
            "<a class=\"btn\" href=\"/members/connect\">Connect data</a>"
            + admin_link +
            "<a class=\"btn\" href=\"/members/logout\">Sign out</a></div></div>"
            + (cards_html or
               "<p class=\"lead\" style=\"margin-top:1.4rem\">Your team workspace is "
               "being set up. Agents and chat appear here shortly.</p>")
            + "</div></section>")
    return _org_page(org, name + " · Globus", body)


_ORG_CHAT_JS = """
<script>
const box = document.getElementById('org-chat-msgs');
const form = document.getElementById('org-chat-form');
const input = document.getElementById('org-chat-input');
function bubble(role, text){
  const d = document.createElement('div');
  d.style.cssText = 'margin:.5rem 0;display:flex;'+(role==='user'?'justify-content:flex-end':'justify-content:flex-start');
  const b = document.createElement('div');
  b.className='bubble';
  b.style.cssText='max-width:80%;padding:.6rem .9rem;border-radius:12px;white-space:pre-wrap;'+
    (role==='user'?'background:#C7714B;color:#fff':'background:var(--surface-sunken,#f4f1ea);color:inherit;border:1px solid var(--border,#e5e7eb)');
  b.textContent=text; d.appendChild(b); box.appendChild(d);
  box.scrollTop = box.scrollHeight; return b;
}
form.addEventListener('submit', async (e)=>{
  e.preventDefault();
  const text = input.value.trim(); if(!text) return;
  input.value=''; bubble('user', text);
  const think = bubble('assistant', '…');
  try {
    const r = await fetch('/members/globus/chat', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({message:text})});
    const j = await r.json();
    think.textContent = j.reply || j.error || '(no response)';
  } catch(err){ think.textContent = '(error reaching Globus)'; }
  box.scrollTop = box.scrollHeight;
});
</script>
"""


def org_chat_html(org, email, messages):
    """Globus chat for an employee — grounded strictly on THEIR OWN connected
    data (the search tools filter WHERE email=%s). Text-only, org-branded."""
    name = _org_name(org)
    hist = []
    for m in (messages or []):
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        align = "flex-end" if role == "user" else "flex-start"
        style = ("background:#C7714B;color:#fff" if role == "user"
                 else "background:var(--surface-sunken,#f4f1ea);color:inherit;"
                      "border:1px solid var(--border,#e5e7eb)")
        hist.append(
            "<div style=\"margin:.5rem 0;display:flex;justify-content:" + align + "\">"
            "<div class=\"bubble\" style=\"max-width:80%;padding:.6rem .9rem;"
            "border-radius:12px;white-space:pre-wrap;" + style + "\">"
            + esc(m.get("content") or "") + "</div></div>")
    body = ("<section class=\"section\"><div class=\"container narrow\">"
            "<div style=\"display:flex;justify-content:space-between;align-items:center;"
            "gap:1rem;flex-wrap:wrap\"><div><span class=\"eyebrow\">" + name
            + " · Globus</span><h1 style=\"margin:.2rem 0 0\">Chat</h1>"
            "<p class=\"muted small\" style=\"margin:.3rem 0 0\">Grounded on your own "
            "connected Gmail &amp; Drive — private to you.</p></div>"
            "<a class=\"btn\" href=\"/members/globus\">&larr; Back</a></div>"
            "<div id=\"org-chat-msgs\" style=\"margin-top:1.2rem;min-height:40vh;"
            "max-height:60vh;overflow-y:auto;padding:.5rem;border:1px solid "
            "var(--border,#e5e7eb);border-radius:12px\">"
            + ("".join(hist) or "<p class=\"muted small\">Ask about your email or "
               "Drive files — e.g. \"what did I get from X this week?\"</p>")
            + "</div>"
            "<form id=\"org-chat-form\" style=\"display:flex;gap:.5rem;margin-top:.8rem\">"
            "<input id=\"org-chat-input\" type=\"text\" placeholder=\"Ask Globus…\" "
            "autocomplete=\"off\" style=\"flex:1\">"
            "<button class=\"btn btn-primary\" type=\"submit\">Send</button></form>"
            "</div></section>" + _ORG_CHAT_JS)
    return _org_page(org, name + " · Globus chat", body)


def org_connect_html(org, email, connections, cap, message="", message_kind="ok"):
    """Globus-branded, GOOGLE-ONLY connect page for the org portal. Employees
    connect their own Google account(s) (Drive + Gmail) — no MS/WhatsApp/Teams/
    Telegram tiles (those aren't wired on the org host). All actions are keyed to
    the employee's own email."""
    name = _org_name(org)
    color = "#16a34a" if message_kind == "ok" else "#dc2626"
    note = ("<p class=\"form-note\" style=\"color:" + color + "\">" + esc(message)
            + "</p>") if message else ""
    google = [c for c in (connections or []) if c.get("provider") == "google"]

    cards = []
    for c in google:
        acct = esc(c.get("provider_account") or "")
        recon = c.get("needs_reconnect")
        status = ("<span style=\"color:#b45309\">Needs reconnect</span>" if recon
                  else "<span class=\"muted small\">" + esc(c.get("sync_status") or "idle")
                       + "</span>")
        cards.append(
            "<div class=\"card\" style=\"padding:.9rem;display:flex;justify-content:"
            "space-between;align-items:center;gap:1rem;flex-wrap:wrap\">"
            "<div><strong>" + acct + "</strong><br>" + status + "</div>"
            "<div style=\"display:flex;gap:.4rem\">"
            "<form method=\"POST\" action=\"/members/connect/google/sync\" style=\"margin:0\">"
            "<input type=\"hidden\" name=\"conn_id\" value=\"" + esc(c.get("id")) + "\">"
            "<button class=\"btn\" style=\"padding:.25rem .7rem;font-size:.8rem\">Sync</button></form>"
            "<form method=\"POST\" action=\"/members/connect/google/disconnect\" style=\"margin:0\">"
            "<input type=\"hidden\" name=\"conn_id\" value=\"" + esc(c.get("id")) + "\">"
            "<button class=\"btn\" style=\"padding:.25rem .7rem;font-size:.8rem\">Disconnect</button>"
            "</form></div></div>")
    cards_html = ("".join(cards) or
                  "<p class=\"muted small\">No accounts connected yet.</p>")

    if len(google) < (cap or 10):
        add = ("<form method=\"GET\" action=\"/members/connect/google/start\" "
               "class=\"card\" style=\"padding:1rem;margin-top:1rem\">"
               "<p style=\"margin:0 0 .5rem\"><strong>Connect a Google account</strong></p>"
               "<label style=\"margin-right:1rem\"><input type=\"checkbox\" name=\"drive\" "
               "value=\"1\" checked> Drive</label>"
               "<label style=\"margin-right:1rem\"><input type=\"checkbox\" name=\"gmail\" "
               "value=\"1\" checked> Gmail</label>"
               "<div style=\"margin-top:.7rem\"><button class=\"btn btn-primary\" "
               "type=\"submit\">Connect with Google</button></div>"
               "<p class=\"muted small\" style=\"margin:.6rem 0 0\">You'll approve access "
               "on Google's screen. Your data stays private to you.</p></form>")
    else:
        add = "<p class=\"muted small\">Maximum accounts connected.</p>"

    body = ("<section class=\"section\"><div class=\"container narrow\">"
            "<div style=\"display:flex;justify-content:space-between;align-items:center;"
            "gap:1rem;flex-wrap:wrap\"><div><span class=\"eyebrow\">" + name
            + " · Globus</span><h1 style=\"margin:.2rem 0 0\">Connect data</h1>"
            "<p class=\"muted small\" style=\"margin:.3rem 0 0\">Connect your own Google "
            "account so Globus can search, organize, and draft in <em>your</em> Gmail &amp; "
            "Drive — private to you.</p></div>"
            "<a class=\"btn\" href=\"/members/globus\">&larr; Back</a></div>" + note
            + "<div style=\"margin-top:1.2rem\">" + cards_html + "</div>" + add
            + "</div></section>")
    return _org_page(org, name + " · Connect", body)


def _org_audience_label(g):
    t = g.get("audience_type")
    if t == "all":
        return "Everyone"
    if t == "department":
        return "Team: " + esc(g.get("audience_value") or "")
    return "Person: " + esc(g.get("audience_value") or "")


def org_admin_html(org, email, members, grants, agent_options, message=""):
    """Org admin console (admins only): default-private sharing controls —
    grant/revoke agents to everyone / a team / a person, and set each member's
    team + role. `agent_options` = [(slug, name)]; `members` = rows with
    email/role/department; `grants` = org_agent_grants rows."""
    name = _org_name(org)
    depts = sorted({(m.get("department") or "").strip() for m in members
                    if (m.get("department") or "").strip()})
    note = ("<p class=\"form-note\" style=\"color:#16a34a\">" + esc(message)
            + "</p>") if message else ""

    # ── Grant form ──
    agent_opts = "".join("<option value=\"" + esc(s) + "\">" + esc(n) + "</option>"
                         for s, n in agent_options)
    aud_opts = ["<option value=\"all:\">Everyone in " + name + "</option>"]
    if depts:
        aud_opts.append("<optgroup label=\"Team\">"
                        + "".join("<option value=\"department:" + esc(d) + "\">Team: "
                                  + esc(d) + "</option>" for d in depts) + "</optgroup>")
    aud_opts.append("<optgroup label=\"Person\">"
                    + "".join("<option value=\"member:" + esc(m["email"]) + "\">"
                              + esc(m["email"]) + "</option>" for m in members)
                    + "</optgroup>")
    grant_form = (
        "<form method=\"POST\" action=\"/members/globus/admin/grant\" "
        "style=\"display:flex;gap:.5rem;flex-wrap:wrap;align-items:end;margin:.5rem 0 1.5rem\">"
        "<label>Agent<br><select name=\"agent\">" + agent_opts + "</select></label>"
        "<label>Give access to<br><select name=\"audience\">" + "".join(aud_opts)
        + "</select></label>"
        "<button class=\"btn btn-primary\" type=\"submit\">Grant</button></form>")

    # ── Current grants ──
    if grants:
        grows = "".join(
            "<tr><td>" + esc(g.get("agent_slug") or "") + "</td><td>"
            + _org_audience_label(g) + "</td><td>"
            "<form method=\"POST\" action=\"/members/globus/admin/revoke\" "
            "style=\"margin:0\"><input type=\"hidden\" name=\"grant_id\" value=\""
            + esc(g.get("id")) + "\"><button class=\"btn\" style=\"padding:.2rem .6rem;"
            "font-size:.8rem\">Revoke</button></form></td></tr>"
            for g in grants)
        grants_tbl = ("<table style=\"width:100%;border-collapse:collapse\"><thead><tr>"
                      "<th align=\"left\">Agent</th><th align=\"left\">Shared with</th>"
                      "<th></th></tr></thead><tbody>" + grows + "</tbody></table>")
    else:
        grants_tbl = ("<p class=\"muted small\">No grants yet — nothing is shared. "
                      "Everyone's workspace is private.</p>")

    # ── Members (set team + role) ──
    mrows = []
    for m in members:
        me = m["email"]
        mrows.append(
            "<tr><td>" + esc(me) + "</td>"
            "<td><form method=\"POST\" action=\"/members/globus/admin/set-team\" "
            "style=\"margin:0;display:flex;gap:.3rem\">"
            "<input type=\"hidden\" name=\"email\" value=\"" + esc(me) + "\">"
            "<input type=\"text\" name=\"department\" value=\"" + esc(m.get("department") or "")
            + "\" placeholder=\"team\" style=\"width:8rem\">"
            "<button class=\"btn\" style=\"padding:.2rem .5rem;font-size:.78rem\">Save</button>"
            "</form></td>"
            "<td><form method=\"POST\" action=\"/members/globus/admin/set-role\" "
            "style=\"margin:0;display:flex;gap:.3rem\">"
            "<input type=\"hidden\" name=\"email\" value=\"" + esc(me) + "\">"
            "<select name=\"role\"><option value=\"employee\""
            + (" selected" if m.get("role") != "admin" else "") + ">employee</option>"
            "<option value=\"admin\""
            + (" selected" if m.get("role") == "admin" else "") + ">admin</option></select>"
            "<button class=\"btn\" style=\"padding:.2rem .5rem;font-size:.78rem\">Save</button>"
            "</form></td></tr>")
    members_tbl = ("<table style=\"width:100%;border-collapse:collapse\"><thead><tr>"
                   "<th align=\"left\">Employee</th><th align=\"left\">Team</th>"
                   "<th align=\"left\">Role</th></tr></thead><tbody>"
                   + "".join(mrows) + "</tbody></table>")

    body = ("<section class=\"section\"><div class=\"container\">"
            "<div style=\"display:flex;justify-content:space-between;align-items:center;"
            "flex-wrap:wrap;gap:1rem\"><div><span class=\"eyebrow\">" + name
            + " · Globus admin</span><h1 style=\"margin:.2rem 0 0\">Sharing</h1></div>"
            "<a class=\"btn\" href=\"/members/globus\">&larr; Back</a></div>" + note
            + "<p class=\"muted small\" style=\"margin-top:1rem\">Nothing is shared by "
            "default. Grant an agent to a team or person below.</p>"
            "<h3>Grant an agent</h3>" + grant_form
            + "<h3>Current grants</h3>" + grants_tbl
            + "<h3 style=\"margin-top:2rem\">Team &amp; roles</h3>" + members_tbl
            + "</div></section>")
    return _org_page(org, name + " · Admin", body)


# ── Legal pages, served pre-auth on the org host so the Google OAuth consent
# screen can link + fetch them. Deliberately plain baseline wording that each
# operator should have reviewed for their own jurisdiction.
#
# `entity` (who operates this workspace), `contact` (privacy contact) and
# `updated` (last-reviewed date) are supplied by the route handler from
# ORG_LEGAL_ENTITY / ORG_LEGAL_CONTACT config; entity falls back to the org's
# own name, and blank contact/updated simply omit those lines rather than
# printing a placeholder.


def _legal_entity(org, entity=""):
    return esc(entity) if entity else _org_name(org)


def _legal_page(org, title, sections_html, contact="", updated=""):
    name = _org_name(org)
    updated_line = ("<p class=\"muted small\">Last updated: " + esc(updated)
                    + "</p>") if updated else ""
    contact_line = ("<p class=\"muted small\" style=\"margin-top:2rem\">Questions "
                    "about this policy? Contact <a href=\"mailto:" + esc(contact)
                    + "\">" + esc(contact) + "</a>.</p>") if contact else ""
    body = ("<section class=\"section\"><div class=\"container narrow\">"
            "<span class=\"eyebrow\">" + name + " · Globus</span>"
            "<h1>" + esc(title) + "</h1>"
            + updated_line
            + sections_html
            + contact_line +
            "<p class=\"muted small\"><a href=\"/members/globus\">&larr; Back to "
            + name + " Globus</a></p>"
            "</div></section>")
    return _org_page(org, name + " · " + esc(title), body)


def org_privacy_html(org, entity="", contact="", updated=""):
    name = _org_name(org)
    ent = _legal_entity(org, entity)
    s = (
        "<p>" + name + " Globus (\"the Service\") is an internal AI workspace "
        "operated by " + ent + " for its authorized personnel. This policy "
        "explains what data the Service accesses and how it is used.</p>"

        "<h3>1. Information we access</h3>"
        "<ul>"
        "<li><strong>Account &amp; profile</strong> — when you sign in with your "
        "company Google account, we receive your email address, name, and basic "
        "profile, solely to authenticate you and scope your workspace to you.</li>"
        "<li><strong>Google Workspace data you choose to connect</strong> — if you "
        "explicitly connect a source (e.g. Google Drive or Gmail) via Google "
        "OAuth, the Service accesses that data only to build <em>your</em> private "
        "workspace and power your assistant. You choose what to connect, and you "
        "can disconnect at any time.</li>"
        "<li><strong>Usage</strong> — basic operational logs (sign-in events, "
        "requests) for security and reliability.</li>"
        "</ul>"

        "<h3>2. How we use it</h3>"
        "<ul>"
        "<li>Authenticate you and keep your workspace and data isolated to you.</li>"
        "<li>Provide the AI assistant, search over your connected data, and agent "
        "briefs — all scoped to you or your organization.</li>"
        "<li>Operate, secure, and improve the Service.</li>"
        "</ul>"
        "<p>We do <strong>not</strong> sell your data, and we do <strong>not</strong> "
        "use your Google Workspace data for advertising.</p>"

        "<h3>3. Google API Services — Limited Use</h3>"
        "<p>The Service's use of information received from Google APIs adheres to "
        "the <a href=\"https://developers.google.com/terms/api-services-user-data-policy\">"
        "Google API Services User Data Policy</a>, including the Limited Use "
        "requirements. Specifically: we use Google user data only to provide and "
        "improve the workspace features you request; we do not transfer it to "
        "others except as necessary to provide the Service, comply with law, or "
        "with your consent; we do not use it for advertising; and no human reads "
        "it except with your consent, for security, or as required by law.</p>"

        "<h3>4. Storage &amp; sharing</h3>"
        "<p>Data is stored on infrastructure controlled by " + ent + ", with "
        "per-user isolation. We share data only with service providers strictly "
        "necessary to operate the Service (for example, the AI model provider that "
        "processes your requests) and where required by law.</p>"

        "<h3>5. Retention, access &amp; deletion</h3>"
        "<p>You can revoke the Service's access to your Google data at any time "
        "from your <a href=\"https://myaccount.google.com/permissions\">Google "
        "Account permissions</a>. To request deletion of your workspace data, "
        "contact us at the address below.</p>"
    )
    return _legal_page(org, "Privacy Policy", s, contact=contact, updated=updated)


def org_terms_html(org, entity="", contact="", updated=""):
    name = _org_name(org)
    ent = _legal_entity(org, entity)
    s = (
        "<p>These Terms govern your use of " + name + " Globus (\"the Service\"), "
        "an internal tool operated by " + ent + ".</p>"

        "<h3>1. Eligibility &amp; access</h3>"
        "<p>The Service is for <strong>authorized " + ent + " personnel only</strong>. "
        "Access is granted based on your verified company account and may be "
        "suspended or removed at any time, including when your authorization ends.</p>"

        "<h3>2. Acceptable use</h3>"
        "<p>Use the Service only for legitimate " + ent + " business purposes. Do "
        "not misuse it, attempt to access another person's or organization's data, "
        "or use it to violate any law or company policy.</p>"

        "<h3>3. AI output</h3>"
        "<p>The assistant and agents produce drafts and suggestions to aid your "
        "work. Outputs may be incomplete or inaccurate — review and verify before "
        "relying on or acting on them. Nothing is sent to any external party on "
        "your behalf without a human deciding to do so.</p>"

        "<h3>4. No warranty; limitation of liability</h3>"
        "<p>The Service is provided \"as is,\" without warranties of any kind. To "
        "the extent permitted by law, " + ent + " is not liable for any indirect or "
        "consequential damages arising from use of the Service.</p>"

        "<h3>5. Changes</h3>"
        "<p>We may update these Terms and the Service from time to time. Continued "
        "use after an update constitutes acceptance of the revised Terms.</p>"
    )
    return _legal_page(org, "Terms of Service", s, contact=contact, updated=updated)
