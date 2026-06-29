"""Vault-progress live page — extracted from lead_server.py 2026-06-28
as refactor slice #6e.

Single pure HTML function:
  - vault_progress_html(email): live polling page that fetches
    /api/globus/vault-progress every 3s and renders a dashboard.

Pure HTML/JS — no DB, no configure() needed. `email` is accepted for
parity with sibling page builders but isn't used in the markup (the
JSON endpoint is what's auth-gated for the caller's data).
"""
from __future__ import annotations
from html_chrome import _members_shell


def vault_progress_html(email):
    """Live page that polls the JSON endpoint every 3s."""
    body = (
        '<a class="back-link" href="/members/connect" style="display:block">&larr; Data sources</a>'
        '<span class="eyebrow">Vault progress &middot; live</span>'
        '<h1>Globus vault builder</h1>'
        '<p class="lead">Background processor turning your raw Drive + Gmail scrape '
        'into Obsidian notes for Globus. Auto-refreshes every 3 seconds.</p>'
        '<div id="vp-root">'
        '  <p class="muted">loading...</p>'
        '</div>'
        '<style>'
        '.vp-bar{position:relative;height:18px;background:var(--surface-sunken);border-radius:9px;overflow:hidden;margin:.6rem 0 1.1rem}'
        '.vp-bar-fill{position:absolute;left:0;top:0;bottom:0;background:linear-gradient(90deg,var(--accent),#e8a578);transition:width .4s}'
        '.vp-bar-text{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:.78rem;font-weight:600;color:var(--text);mix-blend-mode:multiply}'
        '.vp-card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:1rem 1.2rem;margin-bottom:.7rem}'
        '.vp-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:.7rem;margin-bottom:1.1rem}'
        '.vp-num{font-size:1.55rem;font-weight:600;font-family:Newsreader,serif;color:var(--text);line-height:1}'
        '.vp-lbl{font-size:.74rem;letter-spacing:.08em;text-transform:uppercase;color:var(--text-muted);margin-top:.25rem}'
        '.vp-row{display:flex;justify-content:space-between;gap:.6rem;padding:.35rem 0;border-bottom:1px solid var(--border);font-size:.86rem}'
        '.vp-row:last-child{border-bottom:0}'
        '.vp-row .vp-name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text)}'
        '.vp-row .vp-meta{color:var(--text-muted);font-size:.78rem;white-space:nowrap}'
        '.vp-tag{display:inline-block;font-size:.7rem;letter-spacing:.04em;padding:.1rem .45rem;border-radius:4px;'
        'background:var(--surface-sunken);color:var(--text-soft);margin-right:.35rem;text-transform:uppercase}'
        '</style>'
        '<script>'
        'async function vp(){'
        '  try {'
        '    var r = await fetch("/api/globus/vault-progress");'
        '    var d = await r.json();'
        '    var root = document.getElementById("vp-root");'
        '    var pct = d.pct || 0;'
        '    var eta = d.eta_min == null ? "—" : (d.eta_min < 60 ? Math.round(d.eta_min) + " min" : Math.round(d.eta_min/60*10)/10 + " hours");'
        '    var srcRows = (d.by_source || []).map(function(s){'
        '      var planned = s.status === "planned";'
        '      var tagBg = planned ? "#fee2e2" : "var(--surface-sunken)";'
        '      var tagFg = planned ? "#991b1b" : "var(--text-soft)";'
        '      var counts = planned ? "<em>planned</em>" : (s.processed + " / " + s.extracted);'
        '      return "<div class=\\"vp-row\\"><span class=\\"vp-name\\">" +'
        '        "<span class=\\"vp-tag\\" style=\\"background:" + tagBg + ";color:" + tagFg + "\\">" + (s.tag || "") + "</span>" +'
        '        (s.label || s.source_type) +'
        '        (planned ? " <span class=\\"vp-tag\\" style=\\"background:#f3f4f6;color:#6b7280;margin-left:.3rem\\">SOON</span>" : "") +'
        '        "</span><span class=\\"vp-meta\\">" + counts + "</span></div>";'
        '    }).join("");'
        '    var noteRows = Object.entries(d.notes_by_type || {}).sort(function(a,b){return b[1]-a[1];}).map(function(e){'
        '      return "<div class=\\"vp-row\\"><span class=\\"vp-name\\">" + e[0] + "</span>'
        '<span class=\\"vp-meta\\">" + e[1] + " notes</span></div>";'
        '    }).join("") || "<p class=\\"muted small\\">no notes yet</p>";'
        '    var recentRows = (d.recent || []).map(function(r){'
        '      return "<div class=\\"vp-row\\"><span class=\\"vp-name\\"><span class=\\"vp-tag\\">" + r.source_type + "</span>" +'
        '        (r.filename || "(no name)").replace(/[<>&]/g,"") + "</span>'
        '<span class=\\"vp-meta\\">" + (r.vault_processed_at || "") + "</span></div>";'
        '    }).join("") || "<p class=\\"muted small\\">no files processed yet</p>";'
        '    var now = new Date();'
        '    var hhmm = now.toLocaleTimeString();'
        '    root.innerHTML ='
        '      "<p class=\\"muted small\\" style=\\"text-align:right;margin:0 0 .5rem\\">'
        'Last updated: <strong>" + hhmm + "</strong> &middot; auto-refreshes every 3s</p>"'
        '      + "<div class=\\"vp-grid\\">"'
        '      + "<div class=\\"vp-card\\"><div class=\\"vp-num\\">" + (d.processed||0).toLocaleString() + "</div><div class=\\"vp-lbl\\">Processed</div></div>"'
        '      + "<div class=\\"vp-card\\"><div class=\\"vp-num\\">" + (d.pending||0).toLocaleString() + "</div><div class=\\"vp-lbl\\">Pending</div></div>"'
        '      + "<div class=\\"vp-card\\"><div class=\\"vp-num\\">" + (d.total_notes||0).toLocaleString() + "</div><div class=\\"vp-lbl\\">Obsidian notes generated</div></div>"'
        '      + "<div class=\\"vp-card\\"><div class=\\"vp-num\\">" + (d.per_hour||0) + "/h</div><div class=\\"vp-lbl\\">Throughput · ETA " + eta + "</div></div>"'
        '      + "</div>"'
        '      + "<div class=\\"vp-bar\\"><div class=\\"vp-bar-fill\\" style=\\"width:" + pct + "%\\"></div>'
        '<div class=\\"vp-bar-text\\">" + pct + "% — " + (d.processed||0).toLocaleString() + " of " + (d.extracted||0).toLocaleString() + "</div></div>"'
        '      + "<div class=\\"vp-card\\"><h3 style=\\"margin:0 0 .5rem;font-family:Inter,sans-serif;font-weight:600;font-size:.92rem\\">By source</h3>" + srcRows + "</div>"'
        '      + "<div class=\\"vp-card\\"><h3 style=\\"margin:0 0 .5rem;font-family:Inter,sans-serif;font-weight:600;font-size:.92rem\\">Notes generated by type</h3>" + noteRows + "</div>"'
        '      + "<div class=\\"vp-card\\"><h3 style=\\"margin:0 0 .5rem;font-family:Inter,sans-serif;font-weight:600;font-size:.92rem\\">Most recently processed (last 20)</h3>" + recentRows + "</div>";'
        '  } catch (e) {'
        '    document.getElementById("vp-root").innerHTML = "<p style=\\"color:#dc2626\\">poll failed: " + e.message + "</p>";'
        '  }'
        '}'
        'vp(); setInterval(vp, 3000);'
        '</script>'
    )
    return _members_shell("Vault progress · Globus", body)
