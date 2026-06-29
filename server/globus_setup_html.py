"""Globus setup page — extracted from lead_server.py 2026-06-28 as
refactor slice #6g. The "Wake Globus by connecting your work" page
shown to members who haven't built a vault yet.

Single pure HTML function:
  - globus_setup_html(email, message=""): 3-option panel (connect a
    Google cloud source, upload Obsidian zip, paste markdown).

No DB, no module deps beyond globus_chrome._globus_shell and
html_chrome.esc. `email` is accepted for parity with sibling builders
but not used in markup.
"""
from __future__ import annotations
from html_chrome import esc
from globus_chrome import _globus_shell


def globus_setup_html(email, message=""):
    note = f'<p class="form-error">{esc(message)}</p>' if message else ""
    body = (
        '<a class="back-link" href="/members">&larr; Members area</a>'
        '<span class="eyebrow">Globus &middot; private</span>'
        '<h1>Wake Globus by connecting your work</h1>'
        '<p class="lead">'
        "Globus is your private business-intelligence AI. Connect the tools you "
        "actually use — Drive, Gmail, more sources coming — and Globus answers "
        "questions about your business with full context of what's happening "
        "inside it. Prefer to start with notes? You can upload an Obsidian vault "
        "or paste markdown instead."
        '</p>'
        '<div class="panel" style="border-color:#C7714B;background:#FFF8F2">'
        '<h3>Recommended &middot; Connect a cloud source</h3>'
        '<p class="muted small">Link a Google account (Drive + Gmail) read-only. '
        'Globus syncs in the background and uses what you choose to share as '
        'context. Up to 10 Google accounts per member. WhatsApp, Telegram, and '
        'Microsoft Teams are next.</p>'
        '<p style="margin-top:.7rem"><a class="btn btn-primary" href="/members/connect">'
        'Connect your data sources &rarr;</a></p>'
        '</div>'
        '<div class="panel">'
        '<h3>Option B &middot; Upload an Obsidian vault (zip)</h3>'
        '<p class="muted small">In Obsidian: right-click your vault folder &rarr; '
        '<em>Compress / Send to &gt; Compressed (zipped) folder</em>. Or run '
        '<code>zip -r vault.zip your-vault/</code>. We extract every <code>.md</code> '
        'file and ignore everything else.</p>'
        '<input type="file" id="vault-zip" accept=".zip">'
        '<p style="margin-top:.7rem"><button class="btn btn-primary" id="upload-btn">'
        'Upload &amp; index</button> <span id="upload-status" class="muted small"></span></p>'
        '</div>'
        '<div class="panel">'
        '<h3>Option C &middot; Paste your notes</h3>'
        '<p class="muted small">Paste any markdown. If you have multiple notes, '
        'separate them with <code>--- filename.md ---</code> lines so Globus can cite '
        'specific notes back to you.</p>'
        '<form method="POST" action="/members/globus/upload" id="paste-form">'
        '<input type="hidden" name="source" value="paste">'
        '<textarea name="markdown" rows="14" placeholder="# My note\n\nText goes here..."></textarea>'
        '<p style="margin-top:.7rem"><button class="btn btn-primary" type="submit">Save &amp; start chatting</button></p>'
        '</form>'
        '</div>'
        f'{note}'
        '<script>'
        'document.getElementById("upload-btn").addEventListener("click", async function(){'
        '  var f = document.getElementById("vault-zip").files[0];'
        '  if (!f) { document.getElementById("upload-status").textContent = "Pick a zip first."; return; }'
        '  var btn = this; btn.disabled = true;'
        '  document.getElementById("upload-status").textContent = "Uploading + indexing...";'
        '  try {'
        '    var buf = await f.arrayBuffer();'
        '    var bytes = new Uint8Array(buf), chunk = 0x8000, b64 = "";'
        '    for (var i = 0; i < bytes.length; i += chunk) {'
        '      b64 += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));'
        '    }'
        '    var encoded = btoa(b64);'
        '    var r = await fetch("/members/globus/upload", {'
        '      method: "POST", headers: { "Content-Type": "application/json" },'
        '      body: JSON.stringify({ source: "obsidian-zip", zip_base64: encoded })'
        '    });'
        '    var d = await r.json();'
        '    if (!r.ok) throw new Error(d.error || ("HTTP " + r.status));'
        '    document.getElementById("upload-status").textContent = "Done. Loading chat...";'
        '    location.reload();'
        '  } catch (e) {'
        '    document.getElementById("upload-status").textContent = "Failed: " + e.message;'
        '    btn.disabled = false;'
        '  }'
        '});'
        '</script>'
    )
    return _globus_shell("Globus · Setup", body)
