"""Globus chat page — extracted from lead_server.py 2026-06-28 as
refactor slice #6s. The biggest single carve of the stretch: ~740
lines of HTML + JavaScript that powers the /members/globus chat UI
(voice orb + transcript tabs + composer + agent-activity console),
wrapped with the GlobusAgents sidebar in a 2-column flex layout.

Public surface:
  - globus_chat_html(email, vault, messages, daily_used, daily_cap,
    vault_stats):
        * `daily_cap` — caller passes GLOBUS_DAILY_CAP (module constant
          in lead_server).
        * `vault_stats` — caller pre-computes vault_progress_stats(email)
          OR passes None on exception; this module then falls back to
          legacy counts from the `vault` dict.

Signature refactor keeps this module pure-HTML: zero deps on lead_server's
DB helpers or stats fns. Module deps below are the chrome + helper layer
modules already extracted in earlier slices.
"""
from __future__ import annotations
from html_chrome import esc
from globus_chrome import _globus_shell
from globus_agents_html import _ga_sidebar_html
from voice_helpers import voice_token_make
from db_helpers import cfg


def globus_chat_html(email, vault, messages, daily_used, daily_cap, vault_stats):
    remaining = max(0, daily_cap - daily_used)
    char_count = vault.get("char_count") or 0
    uploaded_at = vault.get("uploaded_at")
    uploaded_str = uploaded_at.strftime("%b %d, %Y") if uploaded_at else ""
    # The page header used to read from vault dict which only counts
    # the static digest sources (Drive/Gmail/Freshsales static blob =
    # 2 sources, 0 "files" because they're aggregated). That ignored
    # WhatsApp / GA / obsidian-zip — leaving "2 sources · 0 files" in
    # the header even with 50k items ingested. Pull from
    # vault_progress_stats() which unifies all backing stores.
    # vault_stats is pre-computed by the caller (vault_progress_stats(email))
    # — None if it failed. Falls back to the vault dict's legacy counts.
    if vault_stats:
        live_sources = [s for s in (vault_stats.get("by_source") or [])
                        if s.get("status") == "live" and s.get("extracted", 0) > 0]
        source_count = len(live_sources)
        file_count = sum(s.get("extracted", 0) for s in live_sources)
        sources_used = live_sources
    else:
        sources_used = vault.get("sources_used") or []
        source_count = vault.get("source_count") or 0
        file_count = vault.get("file_count") or 0
    if source_count:
        labels = sorted({s.get("label") or s.get("source_type") or "?"
                         for s in sources_used})
        sources_summary = (
            f"{source_count} source" + ("s" if source_count != 1 else "")
            + (f" ({', '.join(labels)})" if labels else ""))
    else:
        sources_summary = "no sources"

    msg_html = []
    for m in messages:
        role = m["role"]
        bubble = esc(m["content"])
        msg_html.append(
            f'<div class="msg {role}"><div class="role">{role}</div>'
            f'<div class="bubble">{bubble}</div></div>'
        )
    if not msg_html:
        msg_html.append(
            '<p class="muted small">Empty. Ask Globus anything about your notes. '
            'Try: <em>"What am I supposed to be working on this week?"</em> or '
            '<em>"Where are the gaps in my Q4 plan?"</em></p>'
        )

    body = (
        '<a class="back-link" href="/members">&larr; Members area</a>'
        '<span class="eyebrow">Globus &middot; your private AI</span>'
        '<h1 style="text-align:center;margin-bottom:.5rem">Globus</h1>'
        '<p class="muted small" style="text-align:center;margin:0">'
        f'{source_count} sources &middot; {file_count} files &middot; {char_count:,} chars in context &middot; '
        f'{daily_used} / {daily_cap} messages today</p>'
        '<div class="voice-stage">'
        '  <div class="voice-orb" id="voice-orb" role="button" tabindex="0" aria-label="Tap to talk to Globus">'
        '    <canvas id="voice-brain"></canvas>'
        '    <div><div class="word">GLOBUS</div><div class="sub" id="voice-sub">tap to wake</div></div>'
        '  </div>'
        '  <div class="voice-status" id="voice-status">Tap the orb to talk hands-free</div>'
        '  <div class="voice-error" id="voice-error">Voice engine blocked. In Brave, lower Shields for this site and allow the microphone.</div>'
        '</div>'
        '<div style="text-align:center;margin-bottom:1rem">'
        '<button class="transcript-toggle" id="transcript-toggle" type="button" '
        'aria-expanded="false" aria-controls="transcript-section">'
        'View transcript / type a message</button>'
        '</div>'
        '<div class="transcript-section" id="transcript-section">'
        '<p class="muted small" style="margin-bottom:.5rem">'
        f'<a href="/members/globus/setup">Manage data sources</a> &middot; latest update {uploaded_str}</p>'
        '<div class="tt-tabs" role="tablist">'
        '  <button class="tt-tab active" type="button" data-tt="live" role="tab" aria-selected="true">Live transcript</button>'
        '  <button class="tt-tab" type="button" data-tt="history" role="tab" aria-selected="false">History</button>'
        '</div>'
        '<div class="tt-view active" id="tt-live" role="tabpanel">'
        '  <div class="live-block assistant" id="live-agent"><div class="role">Globus</div><div class="bubble" id="live-agent-bubble"></div></div>'
        '  <p class="live-hint" id="live-hint">Tap the orb and ask anything. Globus\'s reply will type out here in sync with the voice. Full back-and-forth lives in History.</p>'
        '</div>'
        '<div class="tt-view" id="tt-history" role="tabpanel">'
        f'  <div class="chat-log" id="chat-log">{"".join(msg_html)}</div>'
        '</div>'
        '<div class="composer">'
        '<textarea id="composer-input" placeholder="Ask Globus about your data..." rows="2"></textarea>'
        '<button class="btn btn-primary btn-send" id="composer-send">Send</button>'
        '</div>'
        f'<p class="muted small" style="margin-top:.8rem">{remaining} messages left today. Cap resets at 00:00 UTC.</p>'
        '</div>'
        # Live agent activity console — Sumit 2026-06-24: 'I want a
        # console below transcript showing what Globus is actually doing.'
        '<details class="agent-console" id="agent-console" open>'
        '  <summary>Agent activity console &middot; <span id="ac-status">loading…</span></summary>'
        '  <div class="ac-body">'
        '    <div class="ac-section">'
        '      <div class="ac-h">Running now</div>'
        '      <div id="ac-running"><em class="muted small">none</em></div>'
        '    </div>'
        '    <div class="ac-section">'
        '      <div class="ac-h">Latest brief per agent</div>'
        '      <table class="ac-tbl" id="ac-latest"><tbody></tbody></table>'
        '    </div>'
        '    <div class="ac-section">'
        '      <div class="ac-h">Recent runs (last 15)</div>'
        '      <table class="ac-tbl" id="ac-recent"><tbody></tbody></table>'
        '    </div>'
        '  </div>'
        '</details>'
        '<script>'
        'var log = document.getElementById("chat-log");'
        'var input = document.getElementById("composer-input");'
        'var sendBtn = document.getElementById("composer-send");'
        'function esc(s){var d=document.createElement("div");d.textContent=s;return d.innerHTML;}'
        # Exposed on window so the voice orb IIFE (separate scope) can write
        # ElevenLabs onMessage turns into the same chat-log.
        # Newest-at-top: chat-log uses flex-direction: column-reverse, so
        # the FIRST child (top of DOM) renders at the bottom. We want
        # newest at the visible TOP → insertBefore log.firstChild puts
        # new message as the LAST child, which renders at the top.
        # Actually: column-reverse with appendChild puts last-appended at
        # top. Both work; using appendChild for simplicity.
        'window.addMsg = function(role, content){'
        '  var div = document.createElement("div");'
        '  div.className = "msg " + role;'
        '  div.innerHTML = "<div class=\\"role\\">" + role + "</div><div class=\\"bubble\\">" + esc(content) + "</div>";'
        '  log.appendChild(div); log.scrollTop = 0;'
        '  return div;'
        '};'
        'var addMsg = window.addMsg;'
        # Live transcript helpers — paint the current turn into the focused
        # tab with a typewriter reveal. addMsg() keeps writing to the full
        # history (the other tab); these helpers only touch the live tab.
        '(function(){'
        '  var lu = document.getElementById("live-user");'
        '  var lub = document.getElementById("live-user-bubble");'
        '  var la = document.getElementById("live-agent");'
        '  var lab = document.getElementById("live-agent-bubble");'
        '  var hint = document.getElementById("live-hint");'
        # Separate timers so the user typewriter and the agent typewriter
        # don\'t kill each other (onMessage(user) triggers both
        # liveSetUserText AND liveStartAgent in the same tick).
        '  var userTimer = null;'
        '  window.__liveAwaitingUser = true;'
        '  function stopUser(){ if (userTimer){ clearTimeout(userTimer); userTimer = null; } }'
        '  function hideHint(){ if (hint) hint.style.display = "none"; }'
        '  function resetAll(){ stopUser();'
        '    window.__agentQueue = ""; window.__agentPos = 0;'
        '    lu.classList.remove("show"); la.classList.remove("show");'
        '    lub.textContent = ""; lab.textContent = "";'
        '    lub.classList.remove("typing"); lab.classList.remove("typing");'
        '  }'
        '  function typeChars(el, text, ms, done){ el.textContent = ""; var i = 0;'
        '    (function step(){ if (i >= text.length){ if (done) done(); return; }'
        '      el.textContent += text[i++]; userTimer = setTimeout(step, ms); })(); }'
        # Agent typewriter is OUTPUT-VOLUME GATED — it only advances while
        # TTS audio is actually playing. State lives on window so the
        # voice poll (which runs every animation frame and has outVol)
        # can call window.tickAgentTypewriter on each frame.
        '  window.__agentQueue = "";'
        '  window.__agentPos = 0;'
        '  window.__agentLastTick = 0;'
        '  window.__agentMsPerWord = 230;'
        # Belt-and-suspenders: strip any tool-call XML/markup that survived
        # the server-side cleaner before painting the bubble or TTSing.
        '  function stripMarkup(s){ if (!s) return s;'
        '    s = s.replace(/<[^>]*\\b(?:DSML|tool_calls|invoke|parameter)\\b[^>]*>/gi, "");'
        '    s = s.replace(/\\n\\s*\\n+/g, "\\n").trim();'
        '    return s;'
        '  }'
        # User-side helpers — kept for API stability but the live tab now
        # only shows Globus\'s reply (user text lives in History). The
        # voice poll + voice onMessage still call these; they no-op
        # silently when the user block isn\'t in the DOM.
        '  window.liveShowUserStarted = function(){ stopUser();'
        '    if (!lu) { window.__liveAwaitingUser = false; return; }'
        '    if (la.classList.contains("show")) resetAll();'
        '    hideHint(); lu.classList.add("show");'
        '    lub.textContent = ""; lub.classList.add("typing");'
        '    window.__liveAwaitingUser = false;'
        '  };'
        # liveSetUserText: ElevenLabs sends user_transcript AFTER it has
        # the agent response ready (not at end-of-speech), so this lands
        # ~10-15s late. If Web Speech has already populated the bubble
        # (the live path), do an instant replace with the canonical
        # ElevenLabs transcript. If Web Speech is unavailable, fall back
        # to typewriter reveal.
        '  window.liveSetUserText = function(text){ stopUser();'
        '    window.__liveAwaitingUser = false;'
        '    if (!lu) return;'
        '    if (la.classList.contains("show") && !lub.textContent.trim()) resetAll();'
        '    hideHint(); lu.classList.add("show"); lub.classList.add("typing");'
        '    typeChars(lub, text, 18, function(){ lub.classList.remove("typing"); });'
        '  };'
        # Web Speech: paint interim+final user text into the bubble as
        # the user is speaking. Called from the SpeechRecognition handler
        # below. No typewriter — text is already live.
        '  window.liveSetUserPartial = function(text){'
        '    if (!text || !lu) return;'
        '    if (la.classList.contains("show")) resetAll();'
        '    hideHint(); lu.classList.add("show"); lub.classList.add("typing");'
        '    lub.textContent = text;'
        '    window.__liveAwaitingUser = false;'
        '  };'
        '  window.liveStartAgent = function(){'
        '    hideHint(); la.classList.add("show"); lab.textContent = "";'
        '    lab.classList.add("typing");'
        '    window.__agentQueue = ""; window.__agentPos = 0;'
        '  };'
        '  window.liveSetAgentText = function(text){'
        '    hideHint(); la.classList.add("show"); lab.classList.add("typing");'
        '    lab.textContent = "";'
        '    window.__agentQueue = stripMarkup(text);'
        '    window.__agentPos = 0;'
        '    window.__agentLastTick = 0;'
        '  };'
        # Called every poll frame from the voice scope with current outVol.
        # Reveals next word ONLY when TTS is actively playing. Pauses
        # typewriter when audio pauses. If TTS finishes before we\'ve
        # revealed all text, drain the remainder quickly.
        '  window.tickAgentTypewriter = function(now, outVol){'
        '    var q = window.__agentQueue, p = window.__agentPos;'
        '    if (!q) return;'
        '    if (p >= q.length){'
        '      if (lab.classList.contains("typing")){'
        '        lab.classList.remove("typing");'
        '        window.__liveAwaitingUser = true;'
        '      }'
        '      return;'
        '    }'
        '    if (outVol > 0.020){'
        '      if (now - window.__agentLastTick < window.__agentMsPerWord) return;'
        '      var nextSpace = q.indexOf(" ", p);'
        '      if (nextSpace === -1) nextSpace = q.length;'
        '      else nextSpace += 1;'
        '      lab.textContent = q.slice(0, nextSpace);'
        '      window.__agentPos = nextSpace;'
        '      window.__agentLastTick = now;'
        '    } else {'
        # TTS just ended or hasn\'t started — don\'t fast-forward when it
        # resumes. But if TTS clearly stopped AND we have unrevealed text,
        # drain the rest after a 1.5s silence so the transcript completes.
        '      window.__agentLastTick = now;'
        '      if (window.__lastOutputAt && (now - window.__lastOutputAt) > 1500){'
        '        lab.textContent = q;'
        '        window.__agentPos = q.length;'
        '      }'
        '    }'
        '  };'
        # Tab switching between Live transcript and History.
        '  document.querySelectorAll(".tt-tab").forEach(function(btn){'
        '    btn.addEventListener("click", function(){'
        '      var key = btn.dataset.tt;'
        '      document.querySelectorAll(".tt-tab").forEach(function(b){'
        '        var act = (b === btn); b.classList.toggle("active", act);'
        '        b.setAttribute("aria-selected", act ? "true" : "false");'
        '      });'
        '      document.querySelectorAll(".tt-view").forEach(function(v){'
        '        v.classList.toggle("active", v.id === "tt-"+key);'
        '      });'
        '    });'
        '  });'
        '})();'
        # Auto-open the transcript panel (used by the voice orb on call start).
        'window.openTranscriptIfClosed = function(){'
        '  var sec = document.getElementById("transcript-section");'
        '  var btn = document.getElementById("transcript-toggle");'
        '  if (sec && btn && !sec.classList.contains("open")) btn.click();'
        '};'
        'log.scrollTop = log.scrollHeight;'
        'async function send(){'
        '  var text = input.value.trim();'
        '  if (!text || sendBtn.disabled) return;'
        '  sendBtn.disabled = true; input.disabled = true;'
        '  addMsg("user", text); input.value = "";'
        '  try { window.liveSetUserText && window.liveSetUserText(text); } catch(e){}'
        '  try { window.liveStartAgent && window.liveStartAgent(); } catch(e){}'
        '  var thinking = addMsg("assistant", "Thinking...");'
        '  try {'
        '    var r = await fetch("/members/globus/chat", {'
        '      method: "POST", headers: { "Content-Type": "application/json" },'
        '      body: JSON.stringify({ message: text })'
        '    });'
        '    var d = await r.json();'
        '    if (!r.ok) throw new Error(d.error || ("HTTP " + r.status));'
        '    thinking.querySelector(".bubble").textContent = d.reply;'
        '    try { window.liveSetAgentText && window.liveSetAgentText(d.reply); } catch(e){}'
        '  } catch (e) {'
        '    thinking.querySelector(".bubble").textContent = "Error: " + e.message;'
        '  } finally {'
        '    sendBtn.disabled = false; input.disabled = false; input.focus();'
        '  }'
        '}'
        'sendBtn.addEventListener("click", send);'
        'input.addEventListener("keydown", function(e){'
        '  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }'
        '});'
        'input.focus();'
        # --- Agent activity console — polls /api/globus/agent-status every 5s
        'function fmtTs(ts){ return (ts||"").replace(" UTC",""); }'
        'function fmtBytes(n){ if(!n) return "—"; if(n<1024) return n+"B"; return (n/1024).toFixed(1)+"KB"; }'
        'function fmtRuntime(s){ if(s<60) return s+"s"; var m=Math.floor(s/60); return m+"m "+(s%60)+"s"; }'
        'function truthBadge(t){'
        '  if(!t || !t.verdict) return "<span class=\\"truth-badge truth-none\\" title=\\"No Truth Layer receipt\\">not verified</span>";'
        '  var labels={healthy:"healthy",verified_no_work:"verified no work",degraded_contradictory:"contradictory",failed:"failed",stale:"stale"};'
        '  var v=String(t.verdict), reasons=(t.reason_codes||[]).join(", ");'
        '  var title="Truth Layer: "+(labels[v]||v)+(reasons ? " — "+reasons : "");'
        '  return "<span class=\\"truth-badge truth-"+esc(v)+"\\" title=\\""+esc(title)+"\\">"+esc(labels[v]||v)+"</span>";'
        '}'
        'async function pollAgents(){'
        '  try {'
        '    var r = await fetch("/api/globus/agent-status");'
        '    if(!r.ok) throw new Error("HTTP "+r.status);'
        '    var d = await r.json();'
        '    var running = d.running || [];'
        '    var rEl = document.getElementById("ac-running");'
        '    if(running.length){'
        '      rEl.innerHTML = running.map(function(p){'
        '        return "<div class=\\"ac-running-row\\"><span class=\\"c-agent\\">" + esc(p.agent)'
        '          + "</span><span class=\\"muted\\">run " + esc(p.id) + " &middot; " + fmtRuntime(p.runtime_sec) + "</span></div>";'
        '      }).join("");'
        '    } else {'
        '      rEl.innerHTML = "<em class=\\"muted small\\">none</em>";'
        '    }'
        '    var latest = d.latest_per_agent || {};'
        '    var lat = document.getElementById("ac-latest").querySelector("tbody");'
        '    var agentRows = Object.keys(latest).sort().map(function(a){'
        '      var b = latest[a];'
        '      return "<tr><td class=\\"c-agent\\">" + esc(a)'
        '        + "</td><td class=\\"c-ts\\">" + fmtTs(b.ts)'
        '        + "</td><td class=\\"c-bytes\\">" + fmtBytes(b.bytes)'
        '        + "</td><td>" + truthBadge(b.truth)'
        '        + "</td></tr>";'
        '    });'
        '    lat.innerHTML = agentRows.length ? agentRows.join("") : "<tr><td class=\\"muted small\\">no briefs yet</td></tr>";'
        '    var recent = d.recent_runs || [];'
        '    var rec = document.getElementById("ac-recent").querySelector("tbody");'
        '    rec.innerHTML = recent.length ? recent.map(function(r){'
        '      return "<tr><td class=\\"c-ts\\">" + fmtTs(r.ts)'
        '        + "</td><td class=\\"c-agent\\">" + esc(r.agent)'
        '        + "</td><td class=\\"c-bytes\\">" + fmtBytes(r.bytes)'
        '        + "</td><td class=\\"c-status " + esc(r.status) + "\\">" + (r.status === "ok" ? "✓" : "✗")'
        '        + "</td><td>" + truthBadge(r.truth)'
        '        + "</td></tr>";'
        '    }).join("") : "<tr><td class=\\"muted small\\">no recent runs</td></tr>";'
        '    var status = (running.length ? running.length + " running" : "idle")'
        '      + " &middot; snapshot " + fmtTs(d.snapshot_at);'
        '    document.getElementById("ac-status").innerHTML = status;'
        '  } catch(e){'
        '    document.getElementById("ac-status").textContent = "polling failed: " + e.message;'
        '  }'
        '}'
        'pollAgents();'
        'setInterval(pollAgents, 5000);'
        '</script>'
        # --- voice orb: JARVIS-style amber neural sphere with distinct
        #     idle / listening / speaking animations
        '<script>'
        'window.GLOBUS_MODE = "idle";'
        'window.GLOBUS_AGENT_ID = "' + (cfg("MEMBERS_ELEVENLABS_AGENT_ID") or cfg("ELEVENLABS_AGENT_ID") or "") + '";'
        # Catch-all client-error reporter — every uncaught JS error +
        # unhandled promise rejection gets POST'd to the server so we
        # can grep journalctl instead of asking Sumit to open DevTools.
        '(function(){'
        '  function send(payload){'
        '    try {'
        '      payload.url = location.href;'
        '      navigator.sendBeacon'
        '        ? navigator.sendBeacon("/api/globus/client-error", new Blob([JSON.stringify(payload)], {type:"application/json"}))'
        '        : fetch("/api/globus/client-error", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload), keepalive:true});'
        '    } catch(e) {}'
        '  }'
        '  window.addEventListener("error", function(ev){'
        '    send({kind:"error", message: (ev.error && ev.error.message) || ev.message || "?", source: ev.filename || "", line: ev.lineno||0, col: ev.colno||0, stack: (ev.error && ev.error.stack) || ""});'
        '  });'
        '  window.addEventListener("unhandledrejection", function(ev){'
        '    var r = ev.reason || {};'
        '    var msg = (r.message || String(r)), stk = (r.stack || "");'
        # The @elevenlabs/client lib throws an unhandled TypeError ("reading
        # 'error_type'") when ElevenLabs sends a malformed error event mid-call;
        # left unhandled it can tear the voice session down silently. The
        # server-side empty-completion fix removes the trigger; this neutralizes
        # any residual one so the call survives.
        '    var elevenCrash = /error_type/.test(msg) || /elevenlabs/i.test(stk) || /handleErrorEvent/.test(stk);'
        '    send({kind:"unhandledrejection", message: msg.slice(0,1000), stack: stk.slice(0,3000), neutralized: elevenCrash});'
        '    if (elevenCrash) { try { ev.preventDefault(); } catch(e){} }'
        '  });'
        '})();'
        'window.GLOBUS_MEMBER_EMAIL = "' + esc(email) + '";'
        'window.GLOBUS_VOICE_TOKEN = "' + voice_token_make(email) + '";'
        '(function(){'
        '  var canvas = document.getElementById("voice-brain");'
        '  if (!canvas) return;'
        '  var ctx = canvas.getContext("2d"), W, H, DPR;'
        '  function resize(){ DPR = Math.min(window.devicePixelRatio||1,2);'
        '    var r = canvas.getBoundingClientRect();'
        '    W = r.width; H = r.height;'
        '    canvas.width = W*DPR; canvas.height = H*DPR;'
        '    ctx.setTransform(DPR,0,0,DPR,0,0);'
        '  }'
        '  resize(); window.addEventListener("resize", resize);'
        # Brain shape — two fibonacci-distributed hemispheres with a
        # central fissure + surface wrinkles (gyri/sulci). Each lobe is
        # an ellipsoid: narrower side-to-side, taller top-to-bottom,
        # longest front-to-back (kidney-bean shape from above).
        '  var N_PER = 110, pts = [];'
        '  function addLobe(side){'
        '    var xOff = side * 0.50;'
        '    for (var i=0;i<N_PER;i++){'
        '      var y = 1-(i/(N_PER-1))*2;'
        '      var rad = Math.sqrt(Math.max(0,1-y*y));'
        '      var phi = i*Math.PI*(3-Math.sqrt(5));'
        '      var bx = Math.cos(phi)*rad, bz = Math.sin(phi)*rad;'
        # Ellipsoid stretching for brain proportions
        '      var ex = bx*0.42, ey = y*0.62, ez = bz*0.88;'
        # Surface wrinkles — three layered sinusoids give a gyri/sulci look
        '      var wrinkle = Math.sin(bx*6 + side*2)*Math.cos(bz*7)*0.07'
        '                  + Math.sin(y*9)*0.045'
        '                  + Math.cos(bx*9 + bz*5 + side)*0.035;'
        '      var len = Math.sqrt(ex*ex + ey*ey + ez*ez);'
        '      var nf = (len + wrinkle) / Math.max(0.05, len);'
        '      pts.push({x: ex*nf + xOff, y: ey*nf, z: ez*nf, lobe: side});'
        '    }'
        '  }'
        '  addLobe(-1); addLobe(1);'
        '  var N = pts.length;'
        # Edge mesh — only connect points WITHIN the same hemisphere so
        # the central fissure stays visible (no lines crossing it).
        '  var edges = [];'
        '  for (var a=0;a<N;a++) for (var b=a+1;b<N;b++){'
        '    if (pts[a].lobe !== pts[b].lobe) continue;'
        '    var dx=pts[a].x-pts[b].x, dy=pts[a].y-pts[b].y, dz=pts[a].z-pts[b].z;'
        '    if (dx*dx+dy*dy+dz*dz < 0.08) edges.push([a,b]);'
        '  }'
        # More sparks travelling along edges
        '  var sparks = []; for (var s=0;s<14;s++) sparks.push({e:(Math.random()*edges.length)|0, t:Math.random(), sp:0.006+Math.random()*0.012});'
        # Outward shockwave rings (speaking) + inward sonar rings (listening)
        '  var shocks = [];'
        '  var sonars = [];'
        '  var t = 0, pulseBoost = 0, lastEmit = 0;'
        '  function rotate(p, ay, ax){'
        '    var cy=Math.cos(ay), sy=Math.sin(ay);'
        '    var x = p.x*cy - p.z*sy, z = p.x*sy + p.z*cy;'
        '    var cx2 = Math.cos(ax), sx2 = Math.sin(ax);'
        '    var yy = p.y*cx2 - z*sx2, zz = p.y*sx2 + z*cx2;'
        '    return {x:x, y:yy, z:zz};'
        '  }'
        '  function frame(){'
        '    var mode = window.GLOBUS_MODE || "idle";'
        '    var speaking = mode==="speaking", listening = mode==="listening";'
        '    var baseGlow = speaking?0.85:listening?0.55:0.32;'
        '    var pulseAmp = speaking?0.09:listening?0.045:0.020;'
        '    var ringSpeed = speaking?2.2:listening?1.4:0.9;'
        '    var sparkMul = speaking?2.6:listening?1.4:0.85;'
        '    t += 0.0045*(speaking?1.5:1);'
        '    if (pulseBoost > 0) pulseBoost *= 0.94;'
        '    ctx.clearRect(0,0,W,H);'
        '    var cx = W/2, cy = H/2, R = Math.min(W,H)*0.44;'
        '    var lvl = window.__voiceLevel || 0;'
        '    var pulse = 1 + Math.sin(t*2.2)*pulseAmp + pulseBoost*0.12 + lvl*0.18;'
        '    var ay = t, ax = Math.sin(t*0.5)*0.22;'
        '    var P = pts.map(function(p){ var q = rotate(p, ay, ax);'
        '      var persp = 1/(1.75 - q.z*0.6);'
        '      return {x:cx + q.x*R*pulse*persp, y:cy + q.y*R*pulse*persp, z:q.z, persp:persp}; });'
        # Hot core radial gradient — warm amber, brighter when speaking
        '    var coreI = baseGlow + pulseBoost*0.25 + lvl*0.4;'
        '    var g = ctx.createRadialGradient(cx,cy,0, cx,cy, R*1.6);'
        '    g.addColorStop(0,    "rgba(255,220,150," + Math.min(1,coreI) + ")");'
        '    g.addColorStop(0.18, "rgba(255,170,80,"  + Math.min(0.85,coreI*0.7) + ")");'
        '    g.addColorStop(0.50, "rgba(220,110,40,0.18)");'
        '    g.addColorStop(1,    "rgba(0,0,0,0)");'
        '    ctx.fillStyle = g; ctx.fillRect(0,0,W,H);'
        # Sonar rings (listening) — emit one every ~45 frames, contract from R*1.6 -> R*0.4
        '    if (listening && t - lastEmit > 0.18){ sonars.push({r:1.6, life:1}); lastEmit = t; }'
        '    if (speaking  && t - lastEmit > 0.14){ shocks.push({r:1.0, life:1}); lastEmit = t; }'
        '    for (var sn=sonars.length-1; sn>=0; sn--){'
        '      var s = sonars[sn]; s.r -= 0.015; s.life -= 0.014;'
        '      if (s.life <= 0) { sonars.splice(sn,1); continue; }'
        '      ctx.strokeStyle = "rgba(255,180,90," + (s.life*0.55) + ")";'
        '      ctx.lineWidth = 1.4;'
        '      ctx.beginPath(); ctx.arc(cx, cy, R*s.r, 0, Math.PI*2); ctx.stroke();'
        '    }'
        '    for (var sh=shocks.length-1; sh>=0; sh--){'
        '      var s2 = shocks[sh]; s2.r += 0.02; s2.life -= 0.012;'
        '      if (s2.life <= 0) { shocks.splice(sh,1); continue; }'
        '      ctx.strokeStyle = "rgba(255,200,110," + (s2.life*0.6) + ")";'
        '      ctx.lineWidth = 1.6;'
        '      ctx.beginPath(); ctx.arc(cx, cy, R*s2.r, 0, Math.PI*2); ctx.stroke();'
        '    }'
        # Neural mesh — amber lines, brighter where points are closer to camera
        '    ctx.lineWidth = 1;'
        '    for (var k=0;k<edges.length;k++){'
        '      var pa = P[edges[k][0]], pb = P[edges[k][1]];'
        '      var depth = (pa.z + pb.z)/2;'
        '      var op = Math.max(0, (speaking?0.18:listening?0.13:0.10) + depth*0.24);'
        '      ctx.strokeStyle = "rgba(255,150,60," + op + ")";'
        '      ctx.beginPath(); ctx.moveTo(pa.x, pa.y); ctx.lineTo(pb.x, pb.y); ctx.stroke();'
        '    }'
        # Travelling sparks along edges — bright yellow-white with glow
        '    for (var spi=0;spi<sparks.length;spi++){'
        '      var e = edges[sparks[spi].e]; if (!e) continue;'
        '      sparks[spi].t += sparks[spi].sp*sparkMul;'
        '      if (sparks[spi].t > 1){ sparks[spi].t = 0; sparks[spi].e = (Math.random()*edges.length)|0; }'
        '      var A = P[e[0]], B = P[e[1]], tt = sparks[spi].t;'
        '      var sx = A.x + (B.x-A.x)*tt, sy = A.y + (B.y-A.y)*tt;'
        '      ctx.fillStyle = "rgba(255,240,200,0.95)";'
        '      ctx.shadowColor = "rgba(255,180,80,1)"; ctx.shadowBlur = speaking?16:11;'
        '      ctx.beginPath(); ctx.arc(sx, sy, speaking?2.0:1.6, 0, 7); ctx.fill();'
        '    }'
        '    ctx.shadowBlur = 0;'
        # Node dots — warm amber, brighter for closer (higher z) points
        '    for (var n=0;n<P.length;n++){'
        '      var p = P[n], d = (p.z + 1)/2, rr = 0.9 + d*1.9;'
        '      ctx.fillStyle = "rgba(255," + ((150 + d*80)|0) + "," + ((60 + d*60)|0) + "," + (0.42 + d*0.5) + ")";'
        '      ctx.shadowColor = "rgba(255,160,70,0.9)"; ctx.shadowBlur = (speaking?12:listening?8:5)*d;'
        '      ctx.beginPath(); ctx.arc(p.x, p.y, rr, 0, 7); ctx.fill();'
        '    }'
        '    ctx.shadowBlur = 0;'
        # Radial filaments from core to a few outermost points — JARVIS "rays"
        '    var rayCount = speaking?12:listening?6:3;'
        '    var topPts = P.slice().sort(function(a,b){return b.z-a.z;}).slice(0, rayCount);'
        '    for (var rp=0;rp<topPts.length;rp++){'
        '      var tp = topPts[rp];'
        '      var grad = ctx.createLinearGradient(cx, cy, tp.x, tp.y);'
        '      grad.addColorStop(0, "rgba(255,210,120," + (speaking?0.42:0.22) + ")");'
        '      grad.addColorStop(1, "rgba(255,160,70,0)");'
        '      ctx.strokeStyle = grad; ctx.lineWidth = speaking?1.2:0.8;'
        '      ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(tp.x, tp.y); ctx.stroke();'
        '    }'
        # Outer rotating arc rings — slow when idle, fast when speaking
        '    for (var rk=0;rk<2;rk++){'
        '      var ringR = R*(1.18 + rk*0.15);'
        '      var a0 = t*ringSpeed*(rk%2?-1:1)*(0.5 + rk*0.18);'
        '      ctx.strokeStyle = "rgba(255,170,80," + (0.28 - rk*0.06) + ")";'
        '      ctx.lineWidth = rk===0?1.4:1;'
        '      ctx.beginPath(); ctx.arc(cx, cy, ringR, a0, a0+Math.PI*1.4); ctx.stroke();'
        '    }'
        '    requestAnimationFrame(frame);'
        '  }'
        '  frame();'
        '  window.__voicePulse = function(){ pulseBoost = 1; };'
        '})();'
        # Transcript toggle
        '(function(){'
        '  var btn = document.getElementById("transcript-toggle");'
        '  var sec = document.getElementById("transcript-section");'
        '  if (!btn || !sec) return;'
        '  btn.addEventListener("click", function(){'
        '    var open = sec.classList.toggle("open");'
        '    btn.setAttribute("aria-expanded", open ? "true" : "false");'
        '    btn.textContent = open ? "Hide transcript" : "View transcript / type a message";'
        '    if (open) { var l = document.getElementById("chat-log"); if (l) l.scrollTop = l.scrollHeight; }'
        '  });'
        '})();'
        # --- ElevenLabs Conversational AI SDK ---
        '(async function(){'
        '  var AGENT = window.GLOBUS_AGENT_ID;'
        '  var orb = document.getElementById("voice-orb");'
        '  var status = document.getElementById("voice-status");'
        '  var sub = document.getElementById("voice-sub");'
        '  var err = document.getElementById("voice-error");'
        '  function set(mode, msg, subTxt){'
        '    window.GLOBUS_MODE = mode;'
        '    if (msg != null) status.textContent = msg;'
        '    if (subTxt != null) sub.textContent = subTxt;'
        '  }'
        '  var Conversation;'
        # PINNED to 1.11.2 — dropped from 1.12.1 on 2026-06-26 after seeing
        # the SAME handleErrorEvent crash recur (TypeError: Cannot read
        # properties of undefined (reading 'error_type')). 1.12.1 was
        # thought to be the last good release but ElevenLabs' backend
        # appears to now emit malformed error events that crash 1.12+
        # client too. 1.11.2 predates this handleErrorEvent code path
        # entirely — verified via npm release history. Confirmed via
        # /api/globus/client-error logs at 2026-06-26 19:19 UTC: every
        # tap-to-talk emits the crash before the conversation can start.
        # When ElevenLabs ships a fixed client (any version > 1.14.0 that
        # actually guards the error event), bump deliberately after a
        # browser test.
        '  try { ({ Conversation } = await import("https://cdn.jsdelivr.net/npm/@elevenlabs/client@1.11.2/+esm")); }'
        '  catch (e) { err.style.display = "block"; set("idle", "Voice engine blocked.", "blocked"); return; }'
        '  var conv = null, busy = false;'
        '  async function toggle(){'
        '    if (busy) return;'
        '    window.__voicePulse && window.__voicePulse();'
        '    if (conv) { try { await conv.endSession(); } catch (e) {} return; }'
        '    busy = true;'
        '    set("connecting", "Waking Globus...", "connecting");'
        '    try {'
        '      await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true}});'
        '      conv = await Conversation.startSession({'
        '        agentId: AGENT,'
        '        dynamicVariables: {'
        '          member_email: window.GLOBUS_MEMBER_EMAIL || "",'
        '          voice_token:  window.GLOBUS_VOICE_TOKEN  || ""'
        '        },'
        '        onConnect: function(){'
        '          set("listening", "Listening &mdash; just talk. Tap to end.", "listening");'
        # Open the transcript panel so live turns are visible as soon as
        # the call starts (Sumit asked — audio isn\'t always clear).
        '          try { window.openTranscriptIfClosed && window.openTranscriptIfClosed(); } catch (e) {}'
        '        },'
        '        onModeChange: function(m){'
        '          var mode = (m && m.mode) || "listening";'
        '          try { console.log("[orb] mode=", mode); } catch(e){}'
        # High-confidence signals only. Poll handles the rest — and "listening"
        # transitions because the SDK can fire "listening" mid-thinking-gap.
        '          if (mode === "thinking" || mode === "processing") {'
        '            window.__forcedThinkingUntil = Date.now() + 30000;'
        '            set("thinking", "Globus is thinking...", "thinking");'
        '          } else if (mode === "speaking") {'
        '            window.__forcedThinkingUntil = 0;'
        '            set("speaking", "Globus is speaking...", "speaking");'
        '          }'
        '        },'
        # onMessage fires when a turn finalizes. When the user role lands,
        # we lock the orb to "thinking" for up to 30s (covers the tool loop).
        # When the assistant role lands, TTS is about to start → release.
        '        onMessage: function(m){'
        '          try { navigator.sendBeacon("/api/globus/client-error", new Blob([JSON.stringify({kind:"voice-event", event:"onMessage", source: m && m.source, len: m && m.message ? m.message.length : 0})], {type:"application/json"})); } catch(e){}'
        '          if (!m || !m.message) return;'
        '          try { console.log("[orb] msg source=", m.source, "len=", m.message.length); } catch(e){}'
        '          var src = (m.source || "").toString().toLowerCase();'
        '          var role = (src === "user" || src.indexOf("user") === 0) ? "user" : "assistant";'
        '          if (role === "user") {'
        '            window.__forcedThinkingUntil = Date.now() + 30000;'
        '            set("thinking", "Globus is thinking...", "thinking");'
        '            try { window.liveSetUserText && window.liveSetUserText(m.message); } catch(e){}'
        '            try { window.liveStartAgent && window.liveStartAgent(); } catch(e){}'
        '          } else if (role === "assistant") {'
        '            window.__forcedThinkingUntil = 0;'
        '            try { window.liveSetAgentText && window.liveSetAgentText(m.message); } catch(e){}'
        '          }'
        '          if (typeof window.addMsg === "function") window.addMsg(role, m.message);'
        '        },'
        '        onDisconnect: function(){ conv = null; set("idle", "Globus disconnected", "tap to talk"); },'
        '        onError: function(e){ console.error("Globus voice err", e); set("listening", "Hiccup &mdash; keep talking or tap to restart.", "listening"); }'
        '      });'
        # Auto-open transcript on call start so the visible record is
        # there even when audio is unclear (Sumit explicitly asked —
        # covers EL client crashes mid-call, choppy connection, muted
        # speakers). Mirrors the manual-toggle behaviour above so the
        # button label + aria stay in sync.
        '      try {'
        '        var _tSec = document.getElementById("transcript-section");'
        '        var _tBtn = document.getElementById("transcript-toggle");'
        '        if (_tSec && !_tSec.classList.contains("open")) {'
        '          _tSec.classList.add("open");'
        '          if (_tBtn) {'
        '            _tBtn.setAttribute("aria-expanded", "true");'
        '            _tBtn.textContent = "Hide transcript";'
        '          }'
        '        }'
        '      } catch(_e) {}'
        # Audio-driven status state machine. Three display states:
        #   "speaking" — TTS audio playing (outVol > 0.02)
        #   "listening" — user currently talking OR idle, waiting for user
        #   "thinking" — post-user, pre-agent gap (sticky lock from
        #                onMessage(user)/onModeChange survives the whole
        #                10-15s tool loop; audio fallback as backup).
        # Always re-evaluates from primitives so it can\'t get stuck.
        '      window.__forcedThinkingUntil = 0;'
        '      window.__lastInputAt = 0;'
        '      window.__lastOutputAt = 0;'
        '      window.__lastDiagPost = 0;'
        '      (function poll(){'
        '        if (!conv){ window.__voiceLevel = 0; return; }'
        '        try {'
        '          var outVol = (typeof conv.getOutputVolume === "function") ? conv.getOutputVolume() : 0;'
        '          var inVol  = (typeof conv.getInputVolume  === "function") ? conv.getInputVolume()  : 0;'
        '          window.__voiceLevel = outVol;'
        '          var now = Date.now();'
        # Mic ambient noise floor measured at ~0.018-0.025 (echo cancel +
        # noise suppress on). Threshold 0.030 catches real speech (typical
        # 0.04-0.25) without false-positives on background. Lower threshold
        # left __lastInputAt updating every frame, so the "thinking" branch
        # never triggered — diag showed sinceIn=0 the entire tool loop.
        '          if (inVol  > 0.030) {'
        '            window.__lastInputAt = now;'
        # First mic spike of a new turn — paint the "you" bubble with a
        # blinking cursor so the live tab feels responsive immediately,
        # even before the ASR final transcript arrives.
        '            if (window.__liveAwaitingUser && window.liveShowUserStarted) {'
        '              try { window.liveShowUserStarted(); } catch(e){}'
        '            }'
        '          }'
        '          if (outVol > 0.020) window.__lastOutputAt = now;'
        # Drive the audio-gated agent typewriter every frame.
        '          if (window.tickAgentTypewriter) {'
        '            try { window.tickAgentTypewriter(now, outVol); } catch(e){}'
        '          }'
        '          var sinceInput  = window.__lastInputAt  ? now - window.__lastInputAt  : 999999;'
        '          var sinceOutput = window.__lastOutputAt ? now - window.__lastOutputAt : 999999;'
        '          var forced = now < window.__forcedThinkingUntil;'
        '          var newMode, newMsg, newSub;'
        '          if (outVol > 0.020) {'
        '            window.__forcedThinkingUntil = 0;'
        '            newMode = "speaking"; newMsg = "Globus is speaking..."; newSub = "speaking";'
        # forced/sticky thinking lock wins over current input — if we KNOW
        # we sent a user transcript, ambient mic noise shouldn\'t flip back
        # to "listening" while tools are running.
        '          } else if (forced) {'
        '            newMode = "thinking"; newMsg = "Globus is thinking..."; newSub = "thinking";'
        '          } else if (inVol > 0.030) {'
        '            newMode = "listening"; newMsg = "Listening..."; newSub = "listening";'
        '          } else if (window.__lastInputAt > 0 && sinceInput < 30000 && sinceInput > 500 && sinceOutput > 800) {'
        '            newMode = "thinking"; newMsg = "Globus is thinking..."; newSub = "thinking";'
        '          } else {'
        '            newMode = "listening"; newMsg = "Listening..."; newSub = "listening";'
        '          }'
        '          if (window.GLOBUS_MODE !== newMode) set(newMode, newMsg, newSub);'
        # Diagnostic — post poll values to server every 2s.
        '          if (now - window.__lastDiagPost > 2000) {'
        '            window.__lastDiagPost = now;'
        '            try {'
        '              var payload = {kind:"voice-diag", mode:window.GLOBUS_MODE, outVol:outVol.toFixed(3), inVol:inVol.toFixed(3), forced:forced, sinceIn:sinceInput, sinceOut:sinceOutput, ts:now};'
        '              if (navigator.sendBeacon) navigator.sendBeacon("/api/globus/client-error", new Blob([JSON.stringify(payload)], {type:"application/json"}));'
        '            } catch(e){}'
        '          }'
        '        } catch(e){}'
        '        requestAnimationFrame(poll);'
        '      })();'
        '    } catch (e) { conv = null; err.style.display = "block";'
        '      set("idle", "Couldn\\u0027t start &mdash; allow the mic, then tap again.", "tap to talk"); }'
        '    finally { busy = false; }'
        '  }'
        '  orb.addEventListener("click", toggle);'
        '  orb.addEventListener("keydown", function(e){ if (e.key === "Enter" || e.key === " "){ e.preventDefault(); toggle(); } });'
        '})();'
        '</script>'
    )
    # Wrap body in a 2-col flex layout: chat main on the left, GlobusAgents
    # sidebar on the right. On narrow screens it stacks (sidebar below)
    # via the @media rule. Inline-styled so we don't touch the global CSS.
    sidebar = _ga_sidebar_html()
    wrapped = (
        '<style>'
        '@media (max-width: 900px){.globus-flex{flex-direction:column!important}'
        '.globus-flex > aside{position:static!important;flex:1 1 auto!important;'
        'width:100%!important;margin-top:1.5rem}}'
        '</style>'
        '<div class="globus-flex" '
        'style="display:flex;gap:1.8rem;align-items:flex-start">'
        '<div style="flex:1;min-width:0">'
        + body +
        '</div>'
        + sidebar +
        '</div>'
    )
    return _globus_shell("Globus · Chat", wrapped, wide=True)


