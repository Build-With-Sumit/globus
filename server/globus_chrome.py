"""Globus-area page chrome — extracted from lead_server.py 2026-06-28
as refactor slice #6f. The upstream of any future globus_*_html.py
page-builder carves.

What's here:
  - _globus_shell(title, body_html, wide=False): page wrapper used by
    every globus subsystem page (setup, chat, agents, agent run,
    sumit.ai, etc.). Embeds _GLOBUS_CSS inline + the globus topnav.
    wide=True relaxes the max-width for the chat layout with sidebar.
  - _GLOBUS_CSS: ~150 lines of globus-only CSS (voice-orb, chat
    bubbles, agent-console table, etc.). Distinct from
    html_chrome._MEMBERS_CSS — globus has its own dark voice stage
    + chat-specific styles.

Uses `esc` from html_chrome (identical to the old `_esc_g` in
lead_server). No DB / configure() needed — pure HTML, the topnav
hardcodes the /members links.
"""
from __future__ import annotations
from html_chrome import esc


_GLOBUS_CSS = """
:root{
  --bg:#FAF9F5;--surface:#FFFFFF;--surface-sunken:#F4F1EA;
  --text:#1F1E1B;--text-muted:#76746E;--text-soft:#3C3A35;
  --accent:#C7714B;--accent-hover:#A95B36;--accent-soft:#F6E9DF;
  --border:#E8E4D9;--border-strong:#D4CFC0;--radius:8px;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font-family:'Inter',system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
  line-height:1.6;-webkit-font-smoothing:antialiased}
a{color:var(--accent);text-decoration:none}
a:hover{color:var(--accent-hover);text-decoration:underline}
h1,h2,h3{font-family:'Newsreader',Georgia,serif;font-weight:500;
  letter-spacing:-.01em;line-height:1.2;color:var(--text);margin:0 0 .6rem}
h1{font-size:2.2rem}
h2{font-size:1.4rem}
h3{font-family:'Inter',sans-serif;font-weight:600;font-size:1.05rem}
p{margin:0 0 1rem}
.topnav{position:sticky;top:0;z-index:50;background:rgba(250,249,245,.85);
  backdrop-filter:blur(10px);border-bottom:1px solid var(--border)}
.topnav-inner{max-width:900px;margin:0 auto;padding:0 24px;
  display:flex;justify-content:space-between;align-items:center;
  height:60px;font-size:.92rem}
.brand{font-weight:600;color:var(--text)}
.brand-pill{display:inline-block;margin-left:.6rem;padding:.18rem .55rem;
  border-radius:5px;background:var(--accent-soft);color:var(--accent);
  font-size:.72rem;font-weight:600;letter-spacing:.05em;text-transform:uppercase}
.container{max-width:900px;margin:0 auto;padding:0 24px}
main{padding:3rem 0 5rem}
.eyebrow{display:inline-block;font-size:.74rem;letter-spacing:.1em;
  text-transform:uppercase;color:var(--accent);font-weight:600;margin-bottom:.7rem}
.lead{font-size:1.1rem;color:var(--text-soft);line-height:1.65;margin-bottom:2rem}
.muted{color:var(--text-muted)}
.small{font-size:.86rem}
.btn{display:inline-block;font-family:inherit;font-weight:500;font-size:.95rem;
  padding:.7rem 1.3rem;border-radius:var(--radius);border:1px solid var(--border-strong);
  background:var(--surface);color:var(--text);cursor:pointer;text-decoration:none;transition:.15s}
.btn:hover{border-color:var(--accent);color:var(--accent);text-decoration:none}
.btn-primary{background:var(--accent);color:#fff!important;border-color:var(--accent)}
.btn-primary:hover{background:var(--accent-hover);border-color:var(--accent-hover)}
.btn-lg{padding:.85rem 1.6rem;font-size:1rem}
.btn:disabled{opacity:.5;cursor:not-allowed}
input[type=text],input[type=file],textarea{width:100%;font-family:inherit;font-size:.97rem;
  padding:.75rem .9rem;color:var(--text);border:1px solid var(--border-strong);
  border-radius:var(--radius);background:var(--surface);transition:.15s}
input:focus,textarea:focus{outline:none;border-color:var(--accent);
  box-shadow:0 0 0 3px var(--accent-soft)}
textarea{resize:vertical;line-height:1.55}
label{display:block;margin-bottom:1.2rem;font-weight:500;font-size:.92rem;color:var(--text-soft)}
label > input,label > textarea{margin-top:.35rem;font-weight:400}
.panel{background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius);padding:1.4rem 1.5rem;margin-bottom:1rem}
.back-link{display:block;margin-bottom:1.6rem;color:var(--text-muted);font-size:.92rem}
.back-link:hover{color:var(--accent)}
.form-error{color:#9F361D;margin-top:.7rem;font-size:.92rem}
.tabs{display:flex;gap:0;border-bottom:1px solid var(--border);margin-bottom:1.2rem}
.tab{padding:.7rem 1.2rem;cursor:pointer;font-weight:500;color:var(--text-muted);
  border-bottom:2px solid transparent;margin-bottom:-1px}
.tab.active{color:var(--accent);border-bottom-color:var(--accent)}
.chat-log{display:flex;flex-direction:column-reverse;gap:1.2rem;margin-bottom:1.4rem;
  max-height:60vh;overflow-y:auto;padding:.5rem 0}
.msg{display:flex;gap:.8rem;align-items:flex-start}
.msg .role{flex:0 0 auto;font-size:.75rem;font-weight:700;letter-spacing:.06em;
  text-transform:uppercase;color:var(--text-muted);width:74px;padding-top:.15rem}
.msg.user .role{color:var(--accent)}
.msg .bubble{flex:1;background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius);padding:.9rem 1.1rem;white-space:pre-wrap;font-size:.97rem;line-height:1.6}
.msg.user .bubble{background:var(--accent-soft);border-color:#F0DCC8}
.composer{display:flex;gap:.6rem;align-items:flex-end;border:1px solid var(--border-strong);
  border-radius:var(--radius);background:var(--surface);padding:.6rem}
.composer textarea{border:none;padding:.4rem;font-size:.97rem;flex:1;min-height:2.5rem;max-height:12rem;box-shadow:none}
.composer textarea:focus{box-shadow:none}
.composer .btn-send{flex:0 0 auto}
.meta-row{display:flex;justify-content:space-between;align-items:center;
  margin-bottom:1rem;flex-wrap:wrap;gap:.6rem;font-size:.86rem;color:var(--text-muted)}
.chip{display:inline-block;background:var(--surface-sunken);padding:.18rem .55rem;
  border-radius:5px;font-size:.85rem;color:var(--text)}
.voice-stage{background:radial-gradient(circle at 50% 50%,#1a0e04 0%,#08050a 50%,#000 100%);
  border-radius:18px;padding:.8rem;margin:1.5rem 0;
  border:1px solid #1a1208;display:flex;flex-direction:column;align-items:center;gap:.8rem;
  box-shadow:inset 0 0 80px rgba(0,0,0,.6);
  aspect-ratio:1/1;max-width:720px;margin-left:auto;margin-right:auto}
.voice-orb{position:relative;width:100%;height:100%;flex:1;
  cursor:pointer;display:flex;align-items:center;justify-content:center;user-select:none;
  border-radius:50%;transition:transform .2s}
.voice-orb:hover{transform:scale(1.02)}
.voice-orb canvas{position:absolute;inset:0;width:100%;height:100%}
.voice-orb .word{position:relative;z-index:2;font:800 1.6rem/1 Inter;letter-spacing:.28em;
  color:#ffe5c2;text-shadow:0 0 22px rgba(255,170,80,.95),0 0 50px rgba(255,140,60,.5);
  pointer-events:none;text-align:center}
.voice-orb .sub{margin-top:.55rem;font:600 .68rem Inter;letter-spacing:.32em;
  color:#ffb066;opacity:.92;text-transform:uppercase;text-shadow:0 0 12px rgba(255,140,60,.7)}
.voice-status{font:600 .95rem Inter;color:#ffc89a;min-height:1.2em;text-align:center;
  letter-spacing:.04em;text-shadow:0 0 18px rgba(255,160,70,.45)}
.voice-error{display:none;margin-top:.4rem;color:#ffd5a6;font-size:.85rem;text-align:center}
.transcript-toggle{display:inline-flex;align-items:center;gap:.4rem;
  background:transparent;color:var(--text-muted);border:1px solid var(--border-strong);
  padding:.5rem 1rem;border-radius:8px;font:500 .85rem Inter;cursor:pointer;
  transition:.15s;text-decoration:none}
.transcript-toggle:hover{color:var(--accent);border-color:var(--accent);text-decoration:none}
.transcript-section{display:none;margin-top:1.5rem}
.transcript-section.open{display:block}
.tt-tabs{display:flex;gap:.25rem;background:var(--surface-sunken);
  border:1px solid var(--border);border-radius:10px;padding:.25rem;margin-bottom:1.1rem}
.tt-tab{flex:1;padding:.55rem .8rem;cursor:pointer;border:none;background:transparent;
  border-radius:7px;color:var(--text-muted);font:600 .85rem Inter;
  letter-spacing:.02em;transition:.15s}
.tt-tab:hover{color:var(--text)}
.tt-tab.active{background:var(--surface);color:var(--text);
  box-shadow:0 1px 3px rgba(0,0,0,.06)}
.tt-view{display:none}
.tt-view.active{display:block}
.live-block{margin-bottom:1.2rem;display:none}
.live-block.show{display:flex;gap:.8rem;align-items:flex-start}
.live-block .role{flex:0 0 auto;font-size:.75rem;font-weight:700;letter-spacing:.06em;
  text-transform:uppercase;color:var(--text-muted);width:74px;padding-top:.95rem}
.live-block.user .role{color:var(--accent)}
.live-block .bubble{flex:1;background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius);padding:.9rem 1.1rem;white-space:pre-wrap;
  font-size:.97rem;line-height:1.6;min-height:1.4em}
.live-block.user .bubble{background:var(--accent-soft);border-color:#F0DCC8}
.live-block .bubble.typing::after{content:"▌";color:var(--accent);
  animation:tt-blink 1s infinite;margin-left:2px;font-weight:400}
@keyframes tt-blink{0%,49%{opacity:1}50%,100%{opacity:0}}
.live-hint{color:var(--text-muted);font-size:.88rem;font-style:italic;
  text-align:center;padding:1.4rem 0}
.agent-console{margin-top:1.8rem;background:var(--surface-sunken);
  border:1px solid var(--border);border-radius:var(--radius);
  padding:.6rem .9rem;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  font-size:.78rem;color:var(--text)}
.agent-console summary{cursor:pointer;font-weight:600;color:var(--text);
  letter-spacing:.04em;padding:.2rem 0;outline:none}
.agent-console summary:focus-visible{outline:1px dashed var(--accent);
  outline-offset:2px}
.agent-console .ac-body{margin-top:.7rem;display:grid;gap:1rem}
.agent-console .ac-h{font-size:.7rem;font-weight:700;letter-spacing:.08em;
  text-transform:uppercase;color:var(--text-muted);margin-bottom:.3rem}
.agent-console .ac-tbl{width:100%;border-collapse:collapse}
.agent-console .ac-tbl td{padding:.18rem .5rem;border-bottom:1px solid
  var(--border);vertical-align:top}
.agent-console .ac-tbl tr:last-child td{border-bottom:0}
.agent-console .ac-tbl td.c-agent{font-weight:600;color:var(--accent);
  width:8em;white-space:nowrap}
.agent-console .ac-tbl td.c-ts{color:var(--text-muted);white-space:nowrap;
  width:11em}
.agent-console .ac-tbl td.c-bytes{color:var(--text-muted);text-align:right;
  width:5em}
.agent-console .ac-tbl td.c-status{width:3em}
.agent-console .ac-tbl td.c-status.ok{color:#16a34a}
.agent-console .ac-tbl td.c-status.fail{color:#dc2626}
.agent-console .ac-tbl td a{color:var(--text);text-decoration:none}
.agent-console .ac-tbl td a:hover{text-decoration:underline}
.agent-console #ac-status{color:var(--text-muted);font-weight:400}
.agent-console .ac-running-row{padding:.3rem 0;display:flex;
  justify-content:space-between;gap:.8rem}
.agent-console .ac-running-row .c-agent{color:#16a34a;font-weight:600}
"""


def _globus_shell(title, body_html, wide=False):
    """wide=True relaxes the container max-width so chat-page layouts
    that need a side panel (the GlobusAgents sidebar) have room."""
    width_style = (' style="max-width:1240px"' if wide else '')
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{esc(title)}</title>'
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        '<link href="https://fonts.googleapis.com/css2?'
        'family=Inter:wght@400;500;600;700&'
        'family=Newsreader:opsz,wght@6..72,300;6..72,400;6..72,500&display=swap" '
        'rel="stylesheet">'
        f'<style>{_GLOBUS_CSS}</style>'
        '</head><body>'
        '<header class="topnav"><div class="topnav-inner">'
        '<a class="brand" href="/members">Build With <strong>Sumit</strong>'
        '<span class="brand-pill">Globus</span></a>'
        '<a href="/members" class="muted">&larr; Members area</a>'
        '</div></header>'
        f'<main><div class="container"{width_style}>{body_html}</div></main>'
        '</body></html>'
    )
