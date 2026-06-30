"""Public /globus landing page — extracted from lead_server.py
2026-06-28 as refactor slice #6h. Marketing-style page (uses _page
chrome from html_chrome, NOT _globus_shell which is members-only).

Single pure HTML function:
  - public_globus_landing_html(): "Your business, answerable." page
    with feature grid + recent shipping log. No auth required.

Pure HTML — no DB, no module deps beyond html_chrome._page.
"""
from __future__ import annotations
from html_chrome import _page


def public_globus_landing_html(public_chat_enabled=False):
    """Public page at /globus — explains what Globus is + what's being
    built. No auth required. Renders inside the standard _page() shell.

    public_chat_enabled: if True, an anonymous demo chat box is shown
      below the hero (posts to /api/public/chat — see public_chat.py).
      Default off so fresh installs don't expose LLM spend to anyone
      who hits the landing page."""
    chat_box = ""
    if public_chat_enabled:
        chat_box = (
            '<div class="panel" style="max-width:680px;margin:1.6rem auto 0;'
            'text-align:left">'
            '<p class="muted small" style="margin:0 0 .6rem">'
            'Try the demo — no signup. (Rate-limited; sign in for the '
            'full vault chat with your own data.)</p>'
            '<form id="public-chat-form" onsubmit="return _publicChat(event)">'
            '<textarea id="public-chat-input" rows="2" required '
            'maxlength="500" placeholder="What is Globus? How do I install it?" '
            'style="width:100%;padding:.6rem;border:1px solid var(--line);'
            'border-radius:6px;font-family:inherit;font-size:.95rem;'
            'resize:vertical"></textarea>'
            '<div style="margin-top:.5rem;display:flex;justify-content:space-between;'
            'align-items:center;gap:.5rem">'
            '<span class="muted small" id="public-chat-status">&nbsp;</span>'
            '<button type="submit" class="btn btn-primary" id="public-chat-send">'
            'Send</button></div>'
            '</form>'
            '<div id="public-chat-reply" style="margin-top:.9rem;'
            'white-space:pre-wrap;font-size:.92rem;line-height:1.5"></div>'
            '</div>'
            '<script>'
            'async function _publicChat(e){'
            '  e.preventDefault();'
            '  var inp=document.getElementById("public-chat-input");'
            '  var btn=document.getElementById("public-chat-send");'
            '  var stat=document.getElementById("public-chat-status");'
            '  var out=document.getElementById("public-chat-reply");'
            '  var msg=inp.value.trim(); if(!msg) return false;'
            '  btn.disabled=true; stat.textContent="thinking…";'
            '  out.textContent="";'
            '  try{'
            '    var r=await fetch("/api/public/chat",{'
            '      method:"POST",headers:{"Content-Type":"application/json"},'
            '      body:JSON.stringify({message:msg})});'
            '    var j=await r.json();'
            '    if(j.ok){out.textContent=j.reply; stat.textContent="";}'
            '    else{stat.textContent=j.error||"error"; stat.style.color="#b00020";}'
            '  }catch(err){stat.textContent="network error"; stat.style.color="#b00020";}'
            '  btn.disabled=false; return false;'
            '}'
            '</script>')

    body = (
        '<section class="section">'
        '<div class="container narrow center" style="padding-bottom:2rem">'
        '<span class="eyebrow">Globus &middot; your private business AI</span>'
        '<h1 style="font-size:2.4rem;line-height:1.1;margin:.5rem 0 1rem">'
        'Your business, answerable.</h1>'
        '<p class="lead" style="font-size:1.15rem;max-width:680px;margin:0 auto 1.4rem">'
        'Globus is a private AI that knows everything happening across '
        'your business — every email, every CRM record, every WhatsApp '
        'and Telegram message, every Google Drive doc, every customer '
        'conversation. Ask it anything. It cites every claim.</p>'
        '<div class="row" style="justify-content:center;margin-bottom:.5rem">'
        '<a class="btn btn-primary btn-lg" href="/members/globus">Open Globus</a>'
        '<a class="btn btn-lg" href="/community.html">Join the community</a>'
        '</div>'
        '<p class="muted small">Members area &middot; sign in required</p>'
        + chat_box +
        '</div></section>'

        # === What Globus actually does ===
        '<section class="section" style="background:var(--surface-sunken)">'
        '<div class="container">'
        '<h2 style="text-align:center;margin-bottom:2rem">What Globus does</h2>'
        '<div class="tools-grid" style="max-width:1100px;margin:0 auto">'

        '<div class="tool-card" style="cursor:default">'
        '<div class="tc-head"><div class="tc-title">'
        '<span class="tc-icon">💬</span> Ask anything</div></div>'
        '<p class="tc-desc">'
        '<em>"What needs my attention today?"</em> &middot; '
        '<em>"Which deals are stalled and why?"</em> &middot; '
        '<em>"Did anyone email me about cancellation this week?"</em> '
        '&middot; <em>"Show me everything we discussed with NKB '
        'Playtech."</em> Text or voice. 50 messages/day.</p>'
        '</div>'

        '<div class="tool-card" style="cursor:default">'
        '<div class="tc-head"><div class="tc-title">'
        '<span class="tc-icon">🔌</span> Connects everything</div></div>'
        '<p class="tc-desc">Google Drive &middot; Gmail &middot; '
        'Freshsales CRM &middot; WhatsApp Web (Chrome extension) &middot; '
        'Telegram (your personal account, all chats) &middot; '
        'Google Analytics &middot; your Obsidian vault. Read-only, '
        'encrypted at rest, fully per-member-private.</p>'
        '</div>'

        '<div class="tool-card" style="cursor:default">'
        '<div class="tc-head"><div class="tc-title">'
        '<span class="tc-icon">🤖</span> Specialist agents</div></div>'
        '<p class="tc-desc">A standing crew of background agents '
        '(named after Mahabharata characters) running on their own '
        'schedules: <strong>sumit.ai</strong> chief of staff '
        '(hourly), <strong>Dron</strong> sales desk (daily), '
        '<strong>Nakul</strong> infra watch (every 6h), '
        '<strong>Vidur</strong> ads, <strong>Vyas</strong> content. '
        'Each produces a brief; nothing acts without your sign-off.</p>'
        '</div>'

        '<div class="tool-card" style="cursor:default">'
        '<div class="tc-head"><div class="tc-title">'
        '<span class="tc-icon">📌</span> Cites every source</div></div>'
        '<p class="tc-desc">Every claim ties back to a file path, '
        'email, CRM record, or WhatsApp/Telegram message. No silent '
        'hallucination — if Globus doesn\'t have the data, it tells '
        'you exactly which connector would unlock the answer.</p>'
        '</div>'

        '<div class="tool-card" style="cursor:default">'
        '<div class="tc-head"><div class="tc-title">'
        '<span class="tc-icon">🎙️</span> Voice mode</div></div>'
        '<p class="tc-desc">Tap the JARVIS-style orb and talk to '
        'Globus hands-free. ElevenLabs voice + Claude Sonnet '
        'reasoning. Same data, same citations, just out loud.</p>'
        '</div>'

        '<div class="tool-card" style="cursor:default">'
        '<div class="tc-head"><div class="tc-title">'
        '<span class="tc-icon">🔐</span> Yours, not ours</div></div>'
        '<p class="tc-desc">Each member\'s data is isolated to that '
        'member\'s account. No cross-member access, no training on your '
        'data. Source-available infrastructure — see '
        '<a href="https://github.com/Globussoft-Technologies/buildwithsumit" '
        'target="_blank" rel="noopener">GitHub</a>.</p>'
        '</div>'

        '</div></div></section>'

        # === Build log / what we shipped recently ===
        '<section class="section">'
        '<div class="container narrow">'
        '<h2 style="text-align:center">What we\'ve built so far</h2>'
        '<p class="lead" style="text-align:center;margin-bottom:1.6rem">'
        'Globus is built in public. Recent shipping log:</p>'
        '<ul style="line-height:1.85;padding-left:1.2rem">'
        '<li><strong>Telegram bridge</strong> — Telethon-based mirror of '
        'every personal chat (1:1, groups, channels) into the vault. '
        'Bot API send-path for in-chat replies.</li>'
        '<li><strong>WhatsApp Web bridge</strong> — Chrome extension '
        'mirrors WhatsApp into the vault read-only.</li>'
        '<li><strong>Freshsales auto-logger</strong> — every WhatsApp '
        'conversation gets an LLM-summarized status note posted to the '
        'matching Freshsales contact every 15 min.</li>'
        '<li><strong>5 specialist agents on Claude</strong> — '
        'sumit.ai (chief of staff), Dron (sales), Nakul (infra), Vidur '
        '(ads), Vyas (content). Daily briefs without burning your '
        'attention.</li>'
        '<li><strong>Voice mode</strong> — JARVIS-style ElevenLabs '
        'orb on the Globus chat page.</li>'
        '<li><strong>Autonomous quality loop</strong> — Globus '
        'tests itself against 100 fresh business-intelligence questions '
        'per iteration, fixes its own persona when failure patterns '
        'emerge.</li>'
        '</ul>'
        '<div class="row" style="justify-content:center;margin-top:1.6rem">'
        '<a class="btn btn-primary" href="/members/globus">Open Globus</a>'
        '<a class="btn" href="/">Back to home</a>'
        '</div>'
        '</div></section>'
    )
    return _page("Globus — your private business AI · Build With Sumit", body)
