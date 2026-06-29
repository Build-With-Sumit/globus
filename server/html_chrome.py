"""Shared HTML primitives used by every page builder in lead_server.py.
Extracted 2026-06-28 as the upstream of refactor slice #6c (members
HTML carve-out). Once this is in place, future page-builder modules
(members_html.py, forum_html.py, etc.) can carve cleanly with just
`from html_chrome import esc, _page, _MEMBERS_CSS` instead of dragging
lead_server with them.

What's here:
  - esc():           HTML-escape a string (49 callers across lead_server)
  - _page():         standard <html><head>...<body> wrapper for any page
                     (8 callers: globus, members, code, onboarding, members,
                      forum index, forum thread, …)
  - _portal_body():  read the static members-area body.html (with safe
                     fallback if the file isn't there)
  - _fmt_size():     char-count -> "12 chars" / "1.4 KB" / "2.1 MB"
  - _MEMBERS_CSS:    ~140 lines of CSS used by the members-area pages

SITE + MEMBERS_DIR are injected at startup via configure() — same
pattern as fb_capi / voice_helpers / stripe_api / members_db. Zero
deps on lead_server.
"""
from __future__ import annotations
import os


# Module config injected by lead_server at startup.
_SITE = ""
_MEMBERS_DIR = ""


def configure(*, site, members_dir):
    """Wire in the SITE URL + MEMBERS_DIR filesystem path. Both are
    referenced inside _page() and _portal_body() respectively at every
    page render; capturing them once at startup avoids dragging
    lead_server's module-level constants in here at import time."""
    global _SITE, _MEMBERS_DIR
    _SITE = site or ""
    _MEMBERS_DIR = members_dir or ""


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def _portal_body():
    try:
        with open(os.path.join(_MEMBERS_DIR, "body.html"),
                  encoding="utf-8") as fh:
            return fh.read()
    except Exception:
        return ("<div class=\"card\"><p class=\"muted\">Member content "
                "is being set up — check back shortly.</p></div>")


def _page(title, body):
    return ("<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"UTF-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">"
            "<title>" + title + "</title>"
            "<link rel=\"icon\" type=\"image/svg+xml\" href=\"" + _SITE + "/favicon.svg\">"
            "<link rel=\"stylesheet\" href=\"" + _SITE + "/styles.css\"></head><body>"
            "<header class=\"site-header\"><div class=\"container nav\">"
            "<a class=\"brand\" href=\"" + _SITE + "/\"><span class=\"brand-mark\">&lt;/&gt;</span> "
            "Build With <strong>Sumit</strong></a></div></header><main>" + body + "</main></body></html>")


def _fmt_size(chars):
    """Char-count to a friendly size label (1 char ≈ 1 byte for ASCII text)."""
    if not chars:
        return "0"
    if chars < 1000:
        return f"{chars} chars"
    if chars < 1_000_000:
        return f"{chars/1000:.1f} KB"
    return f"{chars/1_000_000:.1f} MB"


def fmt_dt(dt):
    """Format a datetime for member-area pages: 'Jun 28, 2026 14:32'.
    Safe on None / non-datetime — returns the string repr or ''."""
    try:
        return dt.strftime("%b %d, %Y %H:%M")
    except Exception:
        return str(dt or "")


def _members_shell(title, body_html):
    """Members-area page chrome — used by members_html, vault-progress,
    connect, and the 2 agent-configure pages. Different from _page()
    (the marketing-style chrome) — embeds the full members CSS inline
    and shows the topnav with Logout link."""
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{esc(title)}</title>'
        '<link rel="icon" type="image/svg+xml" href="' + _SITE + '/favicon.svg">'
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        '<link href="https://fonts.googleapis.com/css2?'
        'family=Inter:wght@400;500;600;700&'
        'family=Newsreader:opsz,wght@6..72,300;6..72,400;6..72,500&display=swap" '
        'rel="stylesheet">'
        f'<style>{_MEMBERS_CSS}</style>'
        '</head><body>'
        '<header class="topnav"><div class="topnav-inner">'
        '<a class="brand" href="' + _SITE + '/">Build With <strong>Sumit</strong>'
        '<span class="brand-pill">Members</span></a>'
        '<a href="/members/logout" class="muted">Log out</a>'
        '</div></header>'
        f'<main><div class="container">{body_html}</div></main>'
        '</body></html>'
    )


# ---- members-area CSS (used by the _members_shell page-builder in
# lead_server.py — kept here so the future members_html carve-out can
# pull both the chrome + the CSS from one place). ----
_MEMBERS_CSS = """
:root{
  --bg:#FAF9F5;--surface:#FFFFFF;--surface-sunken:#F4F1EA;
  --text:#1F1E1B;--text-muted:#76746E;--text-soft:#3C3A35;
  --accent:#C7714B;--accent-hover:#A95B36;--accent-soft:#F6E9DF;
  --border:#E8E4D9;--border-strong:#D4CFC0;--radius:10px;
  --done-bg:#E0EEDE;--done-fg:#296A3D;
  --new-bg:#FFE8D5;--new-fg:#A85429;
  --beta-bg:#E0EAFB;--beta-fg:#2154A8;
  --soon-bg:#EDE9DD;--soon-fg:#5E5B50;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font-family:'Inter',system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
  line-height:1.6;-webkit-font-smoothing:antialiased}
a{color:var(--accent);text-decoration:none}
a:hover{color:var(--accent-hover);text-decoration:underline}
h1,h2,h3,h4{font-family:'Newsreader',Georgia,serif;font-weight:500;
  letter-spacing:-.01em;line-height:1.2;color:var(--text);margin:0 0 .5rem}
h1{font-size:2.4rem}
h2{font-size:1.6rem;margin-top:0}
h3{font-family:'Inter',sans-serif;font-weight:600;font-size:1.05rem;margin:0 0 .3rem}
p{margin:0 0 1rem}
.topnav{position:sticky;top:0;z-index:50;background:rgba(250,249,245,.85);
  backdrop-filter:blur(10px);border-bottom:1px solid var(--border)}
.topnav-inner{max-width:960px;margin:0 auto;padding:0 24px;
  display:flex;justify-content:space-between;align-items:center;
  height:60px;font-size:.92rem}
.brand{font-weight:600;color:var(--text)}
.brand-pill{display:inline-block;margin-left:.6rem;padding:.18rem .55rem;
  border-radius:5px;background:var(--accent-soft);color:var(--accent);
  font-size:.72rem;font-weight:600;letter-spacing:.05em;text-transform:uppercase}
.container{max-width:960px;margin:0 auto;padding:0 24px}
main{padding:3rem 0 5rem}
.eyebrow{display:inline-block;font-size:.74rem;letter-spacing:.1em;
  text-transform:uppercase;color:var(--accent);font-weight:600;margin-bottom:.7rem}
.lead{font-size:1.12rem;color:var(--text-soft);line-height:1.65;margin-bottom:2rem}
.muted{color:var(--text-muted)}
.small{font-size:.86rem}
.btn{display:inline-block;font-family:inherit;font-weight:500;font-size:.95rem;
  padding:.7rem 1.3rem;border-radius:8px;border:1px solid var(--border-strong);
  background:var(--surface);color:var(--text);cursor:pointer;text-decoration:none;transition:.15s}
.btn:hover{border-color:var(--accent);color:var(--accent);text-decoration:none}
.btn-primary{background:var(--accent);color:#fff !important;border-color:var(--accent)}
.btn-primary:hover{background:var(--accent-hover);border-color:var(--accent-hover);color:#fff !important}
.btn-lg{padding:.85rem 1.6rem;font-size:1rem}
.this-week{background:linear-gradient(135deg,#FFF8F2 0%,#FFEEDF 100%);
  border:1px solid #F0DCC8;border-radius:14px;padding:1.6rem 1.8rem;margin-bottom:2.4rem;
  box-shadow:0 1px 2px rgba(199,113,75,.06),0 8px 24px rgba(199,113,75,.06)}
.this-week-badge{display:inline-block;background:var(--accent);color:#fff;
  padding:.22rem .65rem;border-radius:5px;font-size:.7rem;font-weight:700;
  letter-spacing:.08em;text-transform:uppercase;margin-bottom:.7rem}
.this-week h2{margin:0 0 .6rem;font-size:1.55rem}
.this-week .row{display:flex;gap:.6rem;flex-wrap:wrap;margin-top:1.2rem}
.category-head{margin:2rem 0 .9rem;font-family:'Inter',sans-serif;
  font-size:.78rem;letter-spacing:.1em;text-transform:uppercase;font-weight:600;
  color:var(--text-muted)}
.tools-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:1rem}
.tool-card{background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius);padding:1.3rem 1.4rem;
  display:flex;flex-direction:column;transition:.15s;
  text-decoration:none;color:var(--text)}
.tool-card:hover{border-color:var(--accent);transform:translateY(-1px);
  box-shadow:0 4px 16px rgba(199,113,75,.08);text-decoration:none}
.tool-card .tc-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:.6rem}
.tool-card .tc-title{display:flex;align-items:center;gap:.55rem;font-weight:600;font-size:1.02rem}
.tool-card .tc-icon{font-size:1.2rem;line-height:1}
.tool-card .tc-desc{color:var(--text-muted);font-size:.92rem;line-height:1.55;margin:0;flex:1}
.tool-card .tc-foot{margin-top:.9rem;color:var(--accent);font-size:.85rem;font-weight:500}
.pill{display:inline-block;font-size:.66rem;font-weight:700;letter-spacing:.06em;
  text-transform:uppercase;padding:.18rem .5rem;border-radius:5px}
.pill-new{background:var(--new-bg);color:var(--new-fg)}
.pill-v0{background:var(--beta-bg);color:var(--beta-fg)}
.pill-done{background:var(--done-bg);color:var(--done-fg)}
.pill-soon{background:var(--soon-bg);color:var(--soon-fg)}
.panel{background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius);padding:1.4rem 1.5rem;margin-bottom:1rem}
.signup-form{display:flex;gap:.5rem;flex-wrap:wrap;margin-top:.7rem}
.signup-form input{flex:1;min-width:200px;font:500 .95rem Inter;
  padding:.65rem .9rem;border:1px solid var(--border-strong);border-radius:8px;
  background:var(--surface);color:var(--text)}
.signup-form input:focus{outline:none;border-color:var(--accent);
  box-shadow:0 0 0 3px var(--accent-soft)}
.form-note{margin-top:.6rem;font-size:.88rem}
.note-ok{color:var(--done-fg)}
.note-err{color:#9F361D}
.divider{border:none;border-top:1px solid var(--border);margin:3rem 0 1.5rem}
.stat-row{display:flex;gap:.4rem;flex-wrap:wrap;margin-top:.55rem}
.stat-chip{display:inline-flex;align-items:baseline;gap:.4rem;
  background:var(--surface-sunken);border:1px solid var(--border);
  border-radius:6px;padding:.22rem .55rem;font-size:.82rem;color:var(--text-soft)}
.stat-chip strong{color:var(--text);font-weight:600}
.stat-chip.stat-empty{color:var(--text-muted);background:transparent;border-style:dashed}
"""
