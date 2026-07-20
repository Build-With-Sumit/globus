"""Local-only stdlib HTTP dashboard and JSON API."""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlsplit

from .approval_center import ApprovalCenterError, ApprovalNotFoundError
from .service import TruthService
from .storage import ReceiptConflict

MAX_REQUEST_BYTES = 64 * 1024
_SAFE_STORAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_SAFE_DECISION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SAFE_PROPOSAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REJECTED_JSON = object()
FAVICON_SVG = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="16" fill="#0f1726"/>
<path d="M17 33l10 10 20-24" fill="none" stroke="#51e6d1"
 stroke-width="7" stroke-linecap="round" stroke-linejoin="round"/>
</svg>"""

DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Globus Mission Control · Verified AgentOS</title>
  <link rel="icon" href="/favicon.svg" type="image/svg+xml">
  <style>
    :root{color-scheme:dark;--ink:#f5f7ff;--muted:#9aa4b8;--panel:#111827cc;
      --line:#263249;--bg:#070b14;--cyan:#51e6d1;--blue:#7aa2ff;--good:#54d69c;
      --quiet:#76b7ff;--warn:#ffbe68;--bad:#ff748c;--stale:#bf8cff}
    *{box-sizing:border-box} body{margin:0;background:
      radial-gradient(circle at 15% -10%,#183b4c 0,transparent 36rem),
      radial-gradient(circle at 100% 10%,#282050 0,transparent 34rem),var(--bg);
      color:var(--ink);font:15px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,sans-serif;
      min-height:100vh} button,textarea{font:inherit}
    .shell{width:min(1180px,calc(100% - 32px));margin:auto;padding:34px 0 60px}
    header{display:flex;align-items:flex-start;justify-content:space-between;gap:24px;margin-bottom:28px}
    .eyebrow{color:var(--cyan);font-size:12px;font-weight:800;letter-spacing:.16em;text-transform:uppercase}
    h1{font-size:clamp(32px,5vw,58px);letter-spacing:-.045em;line-height:1;margin:8px 0 12px}
    .lede{color:var(--muted);max-width:680px;font-size:17px;margin:0}
    .pulse{margin-top:9px;display:flex;gap:8px;align-items:center;color:var(--muted);white-space:nowrap}
    .pulse i{width:10px;height:10px;background:var(--cyan);border-radius:50%;
      box-shadow:0 0 0 6px #51e6d119}
    .actions{display:flex;gap:10px;flex-wrap:wrap;margin:22px 0}
    button{border:1px solid var(--line);background:#151e30;color:var(--ink);padding:10px 14px;
      border-radius:10px;cursor:pointer;font-weight:700}
    button:disabled{cursor:wait;opacity:.7;transform:none}
    button:hover{border-color:#50617e;transform:translateY(-1px)}
    button.primary{background:linear-gradient(120deg,#55dfcb,#78a7ff);color:#071019;border:0}
    .mission{padding:26px;background:linear-gradient(135deg,#102c31ee,#171d3bee);margin:24px 0}
    .mission-top{display:grid;grid-template-columns:1.15fr .85fr;gap:28px;align-items:start}
    .mission h2{font-size:clamp(25px,4vw,36px);letter-spacing:-.025em;margin:5px 0 9px}
    .mission p{color:var(--muted);margin:0;max-width:680px}
    .mission .actions{margin:18px 0 0}.mission-status{min-height:22px;color:var(--cyan);margin-top:8px}
    .firewall{padding:26px;background:linear-gradient(135deg,#162b31ee,#201a3bee);margin:24px 0}
    .firewall-top{display:grid;grid-template-columns:1.15fr .85fr;gap:28px;align-items:start}
    .firewall h2{font-size:clamp(25px,4vw,36px);letter-spacing:-.025em;margin:5px 0 9px}
    .firewall p{color:var(--muted);margin:0;max-width:700px}
    .firewall .actions{margin:18px 0 0}.firewall-status{min-height:22px;color:var(--cyan);margin-top:8px}
    .firewall-metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:9px}
    .firewall-metric{padding:13px;background:#07121ee6;border:1px solid #385063;border-radius:12px}
    .firewall-metric strong{display:block;color:var(--cyan);font-size:25px;line-height:1.1}
    .firewall-metric span{display:block;color:var(--muted);font-size:11px;margin-top:5px}
    .control-flow{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-top:22px}
    .control-step{position:relative;padding:12px;background:#08111de6;border:1px solid #33445e;border-radius:12px}
    .control-step small{display:block;color:var(--cyan);font-weight:800;text-transform:uppercase;
      letter-spacing:.06em}.control-step span{display:block;margin-top:3px;font-weight:750;font-size:13px}
    .control-step:not(:last-child)::after{content:"→";position:absolute;right:-8px;top:50%;z-index:2;
      color:var(--cyan);font-weight:900;transform:translate(50%,-50%)}
    .capabilities{display:grid;grid-template-columns:repeat(3,1fr);gap:9px}
    .capability{padding:13px;background:#07121ee6;border:1px solid #2b4655;border-radius:12px}
    .capability strong{display:block;color:var(--cyan);font-size:25px;line-height:1.1}
    .capability span{display:block;color:var(--muted);font-size:11px;margin-top:5px}
    .disclosure{color:var(--warn);font-size:11px;margin-top:10px;line-height:1.45}
    .agent-flow{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-top:22px}
    .agent-step{position:relative;padding:12px;background:#08111de6;border:1px solid #294058;border-radius:12px}
    .agent-step small{display:block;color:var(--cyan);font-weight:800;text-transform:uppercase;
      letter-spacing:.06em}.agent-step span{display:block;margin-top:3px;font-weight:750;font-size:13px}
    .agent-step:not(:last-child)::after{content:"→";position:absolute;right:-8px;top:50%;z-index:2;
      color:var(--cyan);font-weight:900;transform:translate(50%,-50%)}
    .lab{display:grid;grid-template-columns:1.4fr 1fr;gap:22px;align-items:center;padding:24px;
      background:linear-gradient(135deg,#11292dee,#151c35ee);margin:24px 0}
    .lab h2{font-size:25px;margin:5px 0 8px}.lab p{color:var(--muted);margin:0;max-width:650px}
    .lab .actions{margin:17px 0 0}.lab-status{min-height:22px;color:var(--cyan);margin-top:9px}
    .flow{display:grid;grid-template-columns:1fr 1fr;gap:9px}.flow-card{padding:12px;
      background:#08111de6;border:1px solid #294058;border-radius:12px}
    .flow-card b{display:block;color:var(--cyan);font-size:11px;letter-spacing:.08em;text-transform:uppercase}
    .flow-card span{display:block;margin-top:3px;font-weight:750}
    .summary{display:grid;grid-template-columns:1.3fr repeat(3,1fr);gap:12px;margin:24px 0}
    .metric,.panel{border:1px solid var(--line);background:var(--panel);backdrop-filter:blur(16px);
      border-radius:16px;box-shadow:0 18px 70px #0005}
    .metric{padding:18px}.metric .label{color:var(--muted);font-size:12px;text-transform:uppercase;
      letter-spacing:.1em}.metric strong{font-size:30px;display:block;margin-top:3px}
    .metric.hero{background:linear-gradient(135deg,#12262aee,#151c34ee)}
    .metric.hero strong{color:var(--cyan)}
    .toolbar{display:flex;align-items:center;justify-content:space-between;gap:14px;margin:32px 0 12px}
    h2{font-size:19px;margin:0}.legend{display:flex;gap:12px;flex-wrap:wrap;color:var(--muted);font-size:12px}
    .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px}
    .table-wrap{overflow:auto}.runs{width:100%;border-collapse:collapse;min-width:780px}
    .runs th{text-align:left;color:var(--muted);font-size:11px;letter-spacing:.09em;text-transform:uppercase;
      padding:13px 15px;border-bottom:1px solid var(--line)}
    .runs td{padding:15px;border-bottom:1px solid #202b3e;vertical-align:top}
    .runs tr:last-child td{border:0}.runs tbody tr{cursor:pointer}.runs tbody tr:hover{background:#ffffff05}
    .agent{font-weight:800}.sub{color:var(--muted);font-size:12px;margin-top:2px}
    .badge{display:inline-flex;align-items:center;border:1px solid currentColor;border-radius:999px;
      padding:4px 9px;font-size:11px;font-weight:900;text-transform:uppercase;letter-spacing:.04em}
    .healthy{color:var(--good)}.verified_no_work{color:var(--quiet)}
    .degraded_contradictory{color:var(--warn)}.failed{color:var(--bad)}.stale{color:var(--stale)}
    .empty{padding:54px;text-align:center;color:var(--muted)}
    dialog{width:min(760px,calc(100% - 28px));max-height:86vh;overflow:auto;color:var(--ink);
      background:#0f1726;border:1px solid #33415b;border-radius:18px;padding:0;box-shadow:0 30px 100px #000b}
    dialog::backdrop{background:#03060dcc;backdrop-filter:blur(4px)}
    .modal-head{position:sticky;top:0;background:#0f1726ed;backdrop-filter:blur(12px);display:flex;
      justify-content:space-between;gap:14px;padding:20px 22px;border-bottom:1px solid var(--line)}
    .modal-body{padding:22px}.modal-head h2{font-size:23px}
    .section{margin:24px 0 8px;color:var(--muted);font-size:11px;text-transform:uppercase;
      letter-spacing:.12em;font-weight:800}.detail-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}
    .detail{padding:12px;background:#0b1220;border:1px solid #202c41;border-radius:10px}
    .detail small{display:block;color:var(--muted)}.check{display:flex;gap:10px;padding:11px 0;border-bottom:1px solid #202b3e}
    .check:last-child{border:0}.check b{color:var(--good)}.check.bad b{color:var(--bad)}
    .check div{overflow-wrap:anywhere}.close{padding:7px 11px}
    .challenge-flow{display:grid;grid-template-columns:repeat(4,1fr);gap:9px;margin:18px 0}
    .challenge-step{padding:14px;background:#0b1220;border:1px solid #26364f;border-radius:12px}
    .challenge-step small{display:block;color:var(--muted);margin-bottom:4px}.hash{font-family:
      ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;overflow-wrap:anywhere}
    .outcome-flow{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:18px 0}
    .outcome-phase{padding:16px;background:#0b1220;border:1px solid #26364f;border-radius:13px}
    .outcome-phase h3{font-size:17px;margin:7px 0 13px}.outcome-facts{display:grid;gap:7px}
    .outcome-fact{display:flex;justify-content:space-between;gap:14px;border-top:1px solid #202b3e;padding-top:7px}
    .outcome-fact span:first-child{color:var(--muted)}.outcome-fact strong{text-align:right}
    .outcome-phase .actions{margin:15px 0 0}.outcome-phase .actions button{font-size:12px;padding:8px 10px}
    .approval-callout{padding:15px;background:#241b0de6;border:1px solid #76562d;border-radius:12px;
      color:var(--warn);margin:14px 0}
    .approval-callout strong{display:block;color:var(--ink);font-size:16px;margin-bottom:3px}
    .approval-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-top:16px}
    .approval-result-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:12px;margin:18px 0}
    .approval-case{padding:16px;background:#0b1220;border:1px solid #26364f;border-radius:13px}
    .approval-case h3{font-size:17px;margin:8px 0 5px}.approval-case p{color:var(--muted);margin:0}
    .approval-attempts{display:grid;grid-template-columns:repeat(3,1fr);gap:9px;margin-top:12px}
    .approval-attempt{padding:13px;background:#0b1220;border:1px solid #26364f;border-radius:12px}
    .approval-attempt small{display:block;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}
    .approval-attempt strong{display:block;margin:5px 0}.approval-attempt .sub{overflow-wrap:anywhere}
    .approved{color:var(--good)}.pending{color:var(--warn)}.blocked{color:var(--bad)}
    textarea{width:100%;min-height:220px;resize:vertical;background:#080e19;color:#dbe5ff;
      border:1px solid var(--line);border-radius:10px;padding:13px;font-family:ui-monospace,monospace;font-size:12px}
    .status{min-height:22px;color:var(--muted);margin-top:8px}.status.bad{color:var(--bad)}
    footer{color:var(--muted);font-size:12px;margin-top:20px;text-align:center}
    @media(max-width:760px){header{display:block}.pulse{margin-top:18px}.mission-top,.firewall-top,.lab{grid-template-columns:1fr}
      .agent-flow{grid-template-columns:1fr}.agent-step:not(:last-child)::after{content:"↓";right:50%;top:auto;bottom:-9px}
      .control-flow{grid-template-columns:1fr}.control-step:not(:last-child)::after{content:"↓";right:50%;top:auto;bottom:-9px}
      .challenge-flow{grid-template-columns:1fr 1fr}.summary{grid-template-columns:1fr 1fr}
      .outcome-flow,.approval-result-grid{grid-template-columns:1fr}.approval-attempts{grid-template-columns:1fr}
      .metric.hero{grid-column:1/-1}
      .toolbar{align-items:flex-start;flex-direction:column}.detail-grid{grid-template-columns:1fr}}
    @media(max-width:420px){.shell{width:min(100% - 20px,1180px);padding-top:22px}
      .mission,.firewall,.lab{padding:18px}.capabilities,.firewall-metrics,.approval-grid{grid-template-columns:1fr}
      .summary{grid-template-columns:1fr}
      .metric.hero{grid-column:auto}.challenge-flow{grid-template-columns:1fr}.modal-body{padding:16px}}
  </style>
</head>
<body>
<main class="shell">
  <header>
    <div><div class="eyebrow">Verified AgentOS for organizations</div><h1>Globus Mission Control</h1>
      <p class="lede">Run agents across knowledge, communication, and business systems—then
      independently verify their outcomes before consequential actions can proceed.</p></div>
    <div class="pulse"><i></i><span>localhost · private by default</span></div>
  </header>
  <section class="firewall panel" aria-labelledby="firewallTitle">
    <div class="firewall-top">
      <div><div class="eyebrow">Consequence Firewall · Human Approval Center</div>
        <h2 id="firewallTitle">Agents can ask. Humans decide what leaves.</h2>
        <p>In this governed judge path, a generated high-risk request pauses before
        execution so a person can approve or reject its exact scope. Approval alone is
        not enough: current Truth evidence must still permit the bounded local action.</p>
        <div class="actions"><button class="primary" id="stageApproval">Stage generated approval request</button></div>
        <div class="firewall-status" id="approvalStatus" role="status"></div>
      </div>
      <div>
        <div class="firewall-metrics" aria-label="Approval control summary">
          <div class="firewall-metric"><strong id="firewallAgents">4</strong><span>registered built-in agents</span></div>
          <div class="firewall-metric"><strong id="firewallTools">20</strong><span>registered LLM-facing tools</span></div>
          <div class="firewall-metric"><strong id="firewallExplicit">2</strong><span>high-risk tools marked explicit approval</span></div>
        </div>
        <div class="disclosure">This judge path uses generated local data and a bounded local
          action. It sends no email or message, calls no provider, and does not claim every
          platform capability is governed by this path.</div>
      </div>
    </div>
    <div class="control-flow" aria-label="Consequence Firewall control path">
      <div class="control-step"><small>01 · Runtime boundary</small><span>Agent tools scoped</span></div>
      <div class="control-step"><small>02 · Human review</small><span>Exact scope approved</span></div>
      <div class="control-step"><small>03 · Truth Gate</small><span>Current evidence checked</span></div>
      <div class="control-step"><small>04 · Execute once</small><span>Bound local action</span></div>
      <div class="control-step"><small>05 · Prevent</small><span>Bad evidence or replay blocked</span></div>
    </div>
  </section>
  <section class="mission panel" aria-labelledby="missionTitle">
    <div class="mission-top">
      <div><div class="eyebrow">Business Outcome Gate</div><h2 id="missionTitle">From agent claim to verified action</h2>
        <p>Mission Control creates three deidentified follow-ups in a separate local destination,
        reopens and measures that destination, issues a Truth receipt, and permits one bounded
        action only while the evidence is healthy. It then removes one row and proves the same
        action is blocked.</p>
        <div class="actions"><button class="primary" id="runOutcome">Run verified business workflow</button></div>
        <div class="mission-status" id="outcomeStatus" role="status"></div>
      </div>
      <div>
        <div class="capabilities" aria-label="Shipped platform capabilities">
          <div class="capability"><strong id="platformAgents">4</strong><span>built-in agents</span></div>
          <div class="capability"><strong id="platformTools">20</strong><span>LLM-facing tools</span></div>
          <div class="capability"><strong id="platformAdapters">33</strong><span>implemented provider adapters</span></div>
        </div>
        <div class="disclosure" id="platformDisclosure">Implemented/setup required does not mean
          connected or configured. Counts describe code shipped in this repository.</div>
      </div>
    </div>
    <div class="agent-flow" aria-label="Verified business workflow">
      <div class="agent-step"><small>01 · Agent claim</small><span>3 follow-ups created</span></div>
      <div class="agent-step"><small>02 · Read-back</small><span>Destination reopened</span></div>
      <div class="agent-step"><small>03 · Truth receipt</small><span>Counts + SHA-256</span></div>
      <div class="agent-step"><small>04 · Action Gate</small><span>Policy checks verdict</span></div>
      <div class="agent-step"><small>05 · Outcome</small><span>Execute or prevent</span></div>
    </div>
  </section>
  <section class="lab panel">
    <div><div class="eyebrow">Globus Truth Layer · Evidence Lab</div><h2>Change one byte. Watch Globus catch it.</h2>
      <p>Judge Mode writes a real local artifact, verifies it with the same read-back
      primitive as the production AgentRunner, appends one controlled byte, and verifies again.
      No model, account, API key, Docker, or external call.</p>
      <div class="actions"><button class="primary" id="runChallenge">Run live tamper challenge</button></div>
      <div class="lab-status" id="challengeStatus" role="status"></div></div>
    <div class="flow" aria-label="Challenge flow">
      <div class="flow-card"><b>01 · Write</b><span>Real local bytes</span></div>
      <div class="flow-card"><b>02 · Verify</b><span>Healthy receipt</span></div>
      <div class="flow-card"><b>03 · Change</b><span>Append one byte</span></div>
      <div class="flow-card"><b>04 · Catch</b><span>Contradiction</span></div>
    </div>
  </section>
  <div class="actions">
    <button id="loadSamples">Load five safe scenarios</button>
    <button id="showIngest">Ingest a receipt</button>
    <button id="refresh">Refresh</button>
  </div>
  <section class="summary" aria-label="Fleet summary">
    <div class="metric hero"><span class="label">Trusted receipts</span><strong id="trusted">—</strong></div>
    <div class="metric"><span class="label">Total runs</span><strong id="total">—</strong></div>
    <div class="metric"><span class="label">Needs attention</span><strong id="attention">—</strong></div>
    <div class="metric"><span class="label">Agents seen</span><strong id="agents">—</strong></div>
  </section>
  <div class="toolbar"><h2>Fleet receipts</h2><div class="legend">
    <span><i class="dot" style="background:var(--good)"></i>healthy</span>
    <span><i class="dot" style="background:var(--quiet)"></i>verified no-work</span>
    <span><i class="dot" style="background:var(--warn)"></i>contradictory</span>
    <span><i class="dot" style="background:var(--bad)"></i>failed</span>
    <span><i class="dot" style="background:var(--stale)"></i>stale</span></div></div>
  <section class="panel table-wrap"><table class="runs"><thead><tr><th>Agent / run</th>
    <th>Verdict</th><th>Declared</th><th>Measured work</th><th>Finished</th><th>Why</th>
    </tr></thead><tbody id="runRows"></tbody></table><div class="empty" id="empty" hidden>
    No receipts yet. Load the safe scenarios or POST a receipt to the API.</div></section>
  <footer>All evaluation and storage happen on this machine. No external service is called.</footer>
</main>
<dialog id="outcome"><div class="modal-head"><div><div class="eyebrow">Verified business outcome</div>
  <h2>Action Gate report</h2></div><button class="close" data-close="outcome">Close</button></div>
  <div class="modal-body"><div id="outcomeBody"></div><div class="actions">
    <button id="downloadOutcome">Download outcome JSON</button>
  </div></div></dialog>
<dialog id="approval"><div class="modal-head"><div><div class="eyebrow">Human Approval Center</div>
  <h2 id="approvalTitle">Review one exact action</h2></div><button class="close" data-close="approval">Close</button></div>
  <div class="modal-body"><div id="approvalBody"></div><div class="actions">
    <button id="downloadApproval" hidden>Download approval proof JSON</button>
  </div></div></dialog>
<dialog id="gate"><div class="modal-head"><div><div class="eyebrow">Immutable authorization audit</div>
  <h2 id="gateTitle">Action Gate decision</h2></div><button class="close" data-close="gate">Close</button></div>
  <div class="modal-body" id="gateBody"></div></dialog>
<dialog id="detail"><div class="modal-head"><div><div class="eyebrow">Run inspection</div>
  <h2 id="detailTitle"></h2></div><button class="close" data-close="detail">Close</button></div>
  <div class="modal-body" id="detailBody"></div></dialog>
<dialog id="ingest"><div class="modal-head"><div><div class="eyebrow">JSON API</div>
  <h2>Ingest one receipt</h2></div><button class="close" data-close="ingest">Close</button></div>
  <div class="modal-body"><textarea id="receiptJson" spellcheck="false"
    aria-label="Receipt JSON"></textarea><div class="actions"><button class="primary" id="submitReceipt">
    Evaluate and persist</button></div><div class="status" id="ingestStatus"></div></div></dialog>
<dialog id="challenge"><div class="modal-head"><div><div class="eyebrow">Live artifact proof</div>
  <h2>One-byte tamper challenge</h2></div><button class="close" data-close="challenge">Close</button></div>
  <div class="modal-body"><div id="challengeBody"></div><div class="actions">
    <button id="inspectClean">Inspect healthy receipt</button>
    <button id="inspectCaught">Inspect caught receipt</button>
    <button id="downloadChallenge">Download challenge JSON</button>
  </div></div></dialog>
<script>
"use strict";
const $=id=>document.getElementById(id), state={runs:[],challenge:null,outcome:null,
  approval:null,approvals:[],platform:null};
const labels={healthy:"healthy",verified_no_work:"verified no-work",
  degraded_contradictory:"contradictory",failed:"failed",stale:"stale"};
function el(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=String(text);
  if(cls)n.className=cls;return n}
function cell(row,node){const td=el("td");td.append(node);row.append(td)}
function fmt(ts){if(!ts)return "—";const d=new Date(ts);return Number.isNaN(d.valueOf())?String(ts):d.toLocaleString()}
function renderRows(runs){const body=$("runRows");body.replaceChildren();$("empty").hidden=runs.length>0;
  for(const item of runs){const r=item.receipt||{},v=item.evaluation||{},tr=el("tr");
    const who=el("div");who.append(el("div",r.agent_id||"(invalid agent)","agent"),
      el("div",r.run_id||item.storage_id,"sub"));cell(tr,who);
    cell(tr,el("span",labels[v.verdict]||v.verdict,"badge "+(labels[v.verdict]?v.verdict:"failed")));
    cell(tr,el("span",r.declared_status||"invalid"));
    const counts=el("div",`${r.output?.items_processed??"?"} processed · ${r.output?.items_changed??"?"} changed`);
    counts.append(el("div",`${r.input?.items_seen??"?"} seen · ${r.input?.items_eligible??"?"} eligible`,"sub"));cell(tr,counts);
    cell(tr,el("span",fmt(r.finished_at)));
    cell(tr,el("span",(v.reason_codes||[]).join(", ")||"—","sub"));
    tr.addEventListener("click",()=>showDetail(item));body.append(tr)}}
function pair(grid,label,value){const d=el("div",undefined,"detail");d.append(el("small",label),el("div",value??"—"));grid.append(d)}
function showDetail(item){const r=item.receipt||{},v=item.evaluation||{};$("detailTitle").textContent=r.agent_id||"Invalid receipt";
  const root=$("detailBody");root.replaceChildren();
  const badge=el("span",labels[v.verdict]||v.verdict,"badge "+(labels[v.verdict]?v.verdict:"failed"));root.append(badge);
  const grid=el("div",undefined,"detail-grid");pair(grid,"Receipt",r.receipt_id||item.storage_id);
  pair(grid,"Run",r.run_id);pair(grid,"Declared",r.declared_status);pair(grid,"Finished",fmt(r.finished_at));
  root.append(el("div","Run facts","section"),grid,el("div","Summary","section"),el("div",r.summary||"—"));
  root.append(el("div","Evidence","section"));
  if(!(r.evidence||[]).length)root.append(el("div","No evidence records.","sub"));
  for(const e of r.evidence||[]){const d=el("div",undefined,"detail");d.append(el("small",`${e.kind||"?"} · ${fmt(e.observed_at)}`),
    el("div",e.ref||"—"));if(e.detail)d.append(el("div",e.detail,"sub"));root.append(d)}
  root.append(el("div","Evaluator checks","section"));
  for(const c of v.checks||[]){const d=el("div",undefined,"check"+(c.passed?"":" bad"));
    d.append(el("b",c.passed?"✓":"×"));const text=el("div");text.append(el("div",c.name),el("div",c.detail,"sub"));d.append(text);root.append(d)}
  $("detail").showModal()}
function outcomePhase(name){return (state.outcome?.phases||[]).find(p=>p.name===name)||{}}
function outcomeFact(root,label,value,hash=false){const row=el("div",undefined,"outcome-fact");
  row.append(el("span",label),el("strong",value??"—",hash?"hash":undefined));root.append(row)}
function renderOutcome(report){state.outcome=report;const root=$("outcomeBody");root.replaceChildren();
  const action=report.action||{},destination=report.destination||{};
  root.append(el("span",report.expectations_met?"workflow verified":"workflow needs attention",
    "badge "+(report.expectations_met?"healthy":"failed")));
  root.append(el("div",`Challenge ${report.challenge_id||"unknown"} used generated data, local verification, and ${report.external_calls??0} external calls.`,"status"));
  const flow=el("div",undefined,"outcome-flow");
  for(const phase of report.phases||[]){const before=phase.name==="before_change";
    const auditVerified=phase.gate?.audit_verified===true;
    const authorized=phase.gate?.authorized===true;
    const actionResult=before
      ?(auditVerified&&authorized&&action.first_executed===true?"Authorized and executed":"Not executed")
      :(!authorized&&auditVerified&&action.second_prevented===true?"Blocked and prevented":"Not proven");
    const card=el("section",undefined,"outcome-phase");
    card.append(el("span",labels[phase.verdict]||phase.verdict||"unknown",
      "badge "+(labels[phase.verdict]?phase.verdict:"failed")));
    card.append(el("h3",`${phase.claimed_count??"?"} → ${phase.observed_count??"?"} · ${
      actionResult}`));
    const facts=el("div",undefined,"outcome-facts");
    outcomeFact(facts,"Truth verdict",labels[phase.verdict]||phase.verdict||"unknown");
    outcomeFact(facts,"Action Gate",auditVerified
      ?(authorized?"authorized + audited":"blocked + audited")
      :"unverified / fail-closed");
    outcomeFact(facts,"Audit read-back",auditVerified?"exact decision verified":"not verified");
    outcomeFact(facts,"Business action",before&&action.first_executed?"executed":
      !before&&action.second_prevented?"prevented":"not proven");
    outcomeFact(facts,"Expected SHA-256",phase.expected_sha256,true);
    outcomeFact(facts,"Observed SHA-256",phase.observed_sha256,true);
    card.append(facts);
    const actions=el("div",undefined,"actions"),receiptButton=el("button","Inspect receipt"),
      gateButton=el("button","Inspect gate decision");
    receiptButton.addEventListener("click",()=>inspectOutcomeReceipt(phase.name));
    gateButton.addEventListener("click",()=>inspectOutcomeGate(phase.name));
    actions.append(receiptButton,gateButton);card.append(actions);flow.append(card)}
  root.append(flow);
  const grid=el("div",undefined,"detail-grid");
  pair(grid,"Destination",destination.name||"generated local destination");
  pair(grid,"Rows changed",destination.rows_modified);
  pair(grid,"First outbox count",action.outbox_rows_after_first_gate);
  pair(grid,"Final outbox count",action.final_outbox_rows);root.append(grid);
  root.append(el("div","Decision","section"),el("div",report.expectations_met
    ?"3 → 3 stayed healthy, so one bounded local action executed. After the destination changed to 3 → 2, the contradictory receipt caused the same policy to block and the second action was never invoked."
    :"The workflow completed without proving the expected healthy-allow and contradictory-block transition. Inspect the receipts and gate decisions before relying on it.","sub"))}
async function inspectOutcomeReceipt(name){const phase=outcomePhase(name);if(!phase.storage_id)return;
  try{let item=state.runs.find(run=>run.storage_id===phase.storage_id);
    if(!item)item=await api(`/api/v1/runs/${encodeURIComponent(phase.storage_id)}`);
    $("outcome").close();showDetail(item)}catch(e){alert(`Receipt unavailable: ${e.message}`)}}
function showGate(decision){$("gateTitle").textContent=decision.authorized?"Action authorized":"Action blocked";
  const root=$("gateBody");root.replaceChildren();
  root.append(el("span",decision.authorized?"authorized":"blocked",
    "badge "+(decision.authorized?"healthy":"degraded_contradictory")));
  const grid=el("div",undefined,"detail-grid");pair(grid,"Decision",decision.decision_id);
  pair(grid,"Receipt",decision.storage_id);pair(grid,"Action",decision.action_id);
  pair(grid,"Policy",decision.policy_id);pair(grid,"Observed verdict",labels[decision.observed_verdict]||decision.observed_verdict);
  pair(grid,"Decided",fmt(decision.decided_at));root.append(grid,
    el("div","Reason codes","section"),el("div",(decision.reason_codes||[]).join(", ")||"—","sub"));
  $("gate").showModal()}
async function inspectOutcomeGate(name){const phase=outcomePhase(name),id=phase.gate?.decision_id;if(!id)return;
  try{const decision=await api(`/api/v1/gate/decisions/${encodeURIComponent(id)}`);
    $("outcome").close();showGate(decision)}catch(e){alert(`Decision unavailable: ${e.message}`)}}
function downloadOutcome(){if(!state.outcome)return;const body=JSON.stringify(state.outcome,null,2);
  const url=URL.createObjectURL(new Blob([body],{type:"application/json"}));const link=el("a");
  link.href=url;link.download=`${state.outcome.challenge_id||"globus-outcome-gate"}.json`;
  document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),0)}
function firstValue(...values){for(const value of values){if(value!==undefined&&value!==null&&value!=="")return value}
  return undefined}
function approvalProposal(report){const candidate=report?.proposal||report?.request||report||{};
  return candidate&&typeof candidate==="object"?candidate:{}}
function approvalTruth(source){const truth=source?.truth||source?.receipt||{};
  return truth&&typeof truth==="object"?truth:{}}
function approvalReasons(source){const reasons=firstValue(source?.reason_codes,source?.reasons,source?.gate?.reason_codes);
  return Array.isArray(reasons)&&reasons.length?reasons.join(", "):"—"}
function approvalIdentifier(report){const p=approvalProposal(report);
  return firstValue(report?.proposal_id,p.proposal_id,report?.request_id,p.request_id)}
function approvalDetail(grid,label,value,hash=false){const d=el("div",undefined,"detail");
  d.append(el("small",label),el("div",value??"—",hash?"hash":undefined));grid.append(d)}
function renderApprovalPending(report){state.approval=report;$("approvalTitle").textContent="Review one exact action";
  $("downloadApproval").hidden=true;const root=$("approvalBody");root.replaceChildren();
  const p=approvalProposal(report),truth=approvalTruth(p),action=report.action||{};
  root.append(el("span","awaiting human review","badge pending"));
  const callout=el("div",undefined,"approval-callout");
  callout.append(el("strong","Paused safely — no action has executed."),
    el("span","This request uses generated local data. Approval is limited to the exact scope shown below."));
  root.append(callout);
  root.append(el("div",firstValue(p.summary,report.summary,
    "A governed demo actor is requesting one bounded local follow-up action."),"status"));
  const grid=el("div",undefined,"approval-grid");
  approvalDetail(grid,"Requested by",firstValue(p.agent_id,p.actor_id,p.requested_by,"generated-demo-agent"));
  approvalDetail(grid,"Capability",firstValue(p.capability_id,p.tool_id,p.tool,"bounded local action"));
  approvalDetail(grid,"Action",firstValue(p.action_id,p.action_kind,action.action_id,action.kind,"generated follow-up"));
  approvalDetail(grid,"Risk / approval",`${firstValue(p.risk,"high")} · ${firstValue(p.approval_mode,p.approval,"explicit")}`);
  approvalDetail(grid,"Scope",firstValue(p.scope_summary,p.scope,report.scope_summary,"1 generated local record"));
  approvalDetail(grid,"Truth evidence",`${firstValue(p.truth_verdict,truth.verdict,report.truth_verdict,"pending read-back")} · ${
    firstValue(p.truth_storage_id,p.storage_id,truth.storage_id,report.truth_storage_id,"receipt bound at execution")}`);
  approvalDetail(grid,"Approval lifetime",firstValue(p.expires_at,report.expires_at,"short-lived"));
  approvalDetail(grid,"Maximum uses",firstValue(p.max_uses,report.max_uses,1));
  root.append(grid,el("div","Exact payload fingerprint","section"),
    el("div",firstValue(p.payload_sha256,p.scope_sha256,report.payload_sha256,"not reported"),"detail hash"),
    el("div","Changing the actor, capability, action, payload, or bound evidence requires a new approval.","status"));
  const actions=el("div",undefined,"actions"),approve=el("button","Approve this exact action","primary"),
    reject=el("button","Reject and keep blocked");
  approve.addEventListener("click",()=>resolveApproval(report,"approved",approve,reject));
  reject.addEventListener("click",()=>resolveApproval(report,"rejected",approve,reject));
  actions.append(approve,reject);root.append(actions)}
function approvalPhases(report){let phases=firstValue(report?.phases,report?.truth_cases,report?.cases);
  if(Array.isArray(phases))return phases;
  if(phases&&typeof phases==="object")return Object.entries(phases).map(([name,value])=>({
    name,...(value&&typeof value==="object"?value:{})}));
  const fallback=[];if(report?.healthy_phase)fallback.push({name:"healthy",...report.healthy_phase});
  if(report?.contradictory_phase)fallback.push({name:"contradictory",...report.contradictory_phase});
  return fallback}
function approvalAttempts(report){const attempts=firstValue(report?.attempts,report?.execution_attempts,
    report?.replay_proof);
  if(Array.isArray(attempts))return attempts;
  if(attempts&&typeof attempts==="object")return Object.entries(attempts).map(([name,value])=>({
    name,...(value&&typeof value==="object"?value:{})}));
  return []}
function phaseCard(phase){const name=String(firstValue(phase.name,phase.phase,"case"));
  const truth=approvalTruth(phase),verdict=firstValue(phase.truth_verdict,truth.verdict,phase.verdict,"not reported");
  const approved=firstValue(phase.human_approved,phase.approved)===true;
  const gateAllowed=firstValue(phase.gate_authorized,phase.authorized,phase.gate?.authorized)===true;
  const executed=firstValue(phase.executed,phase.action_executed)===true;
  const healthy=name.includes("healthy")||verdict==="healthy";
  const card=el("section",undefined,"approval-case");
  card.append(el("span",executed?"executed once":gateAllowed?"authorized":"blocked",
    "badge "+(executed||gateAllowed?"healthy":"degraded_contradictory")));
  card.append(el("h3",healthy?"Human approved + Truth healthy":"Human approved + Truth contradictory"));
  card.append(el("p",healthy
    ?(approved&&gateAllowed&&executed
      ?"Approval and current evidence agreed, so one bounded local action executed."
      :"The service did not prove the full approved, allowed, executed sequence.")
    :(approved&&!gateAllowed&&!executed
      ?"The person approved, but contradictory evidence still blocked execution."
      :"The service did not prove the expected contradictory-evidence block.")));
  const facts=el("div",undefined,"outcome-facts");
  outcomeFact(facts,"Human approval",approved?"approved":"not proven");
  outcomeFact(facts,"Truth verdict",verdict);
  outcomeFact(facts,"Truth Gate",gateAllowed?"allowed":"blocked");
  outcomeFact(facts,"Action",executed?"executed once":"not executed");
  outcomeFact(facts,"Reason",approvalReasons(phase));card.append(facts);return card}
function attemptCard(attempt){const name=String(firstValue(attempt.name,attempt.kind,attempt.label,"attempt"));
  const executed=firstValue(attempt.executed,attempt.action_executed)===true;
  const authorized=firstValue(attempt.authorized,attempt.allowed)===true;
  const status=String(firstValue(attempt.status,"")).toLowerCase(),reason=approvalReasons(attempt);
  const blocked=!executed&&(!authorized||["blocked","rejected","already_consumed"].includes(status)||
    reason.includes("scope_mismatch")||reason.includes("already_consumed")||reason.includes("rejected"));
  const title=name.includes("change")?"Changed payload":name.includes("replay")?"Replay":name.includes("exact")
    ?"Exact approved payload":firstValue(attempt.label,name);
  const card=el("div",undefined,"approval-attempt");
  card.append(el("small",title),el("strong",executed?"executed once":blocked?"blocked":"not proven",
    executed?"approved":blocked?"blocked":"pending"),
    el("div",reason,"sub"));
  const fingerprint=firstValue(attempt.payload_sha256,attempt.scope_sha256);
  if(fingerprint)card.append(el("div",fingerprint,"sub hash"));return card}
function renderApprovalResolved(report){state.approval=report;$("downloadApproval").hidden=false;
  const root=$("approvalBody");root.replaceChildren();const disposition=firstValue(report.disposition,
    report.resolution,report.status),rejected=disposition==="rejected"||disposition==="denied";
  const phases=approvalPhases(report),attempts=approvalAttempts(report);
  const hasHealthy=phases.some(phase=>String(firstValue(phase.name,phase.phase,"")).includes("healthy")||
    firstValue(phase.truth_verdict,phase.verdict,phase.truth?.verdict)==="healthy");
  const hasContradictory=phases.some(phase=>String(firstValue(phase.name,phase.phase,"")).includes("contradict")||
    firstValue(phase.truth_verdict,phase.verdict,phase.truth?.verdict)==="degraded_contradictory");
  $("approvalTitle").textContent=rejected?"Request rejected safely":"Consequence Firewall proof";
  if(rejected){root.append(el("span","rejected by human","badge blocked"),
      el("div","The operator rejected the generated request. No external action was attempted.","approval-callout"));
  }else{root.append(el("span",report.expectations_met===true?"control proven":"inspect result",
      "badge "+(report.expectations_met===true?"healthy":"pending")),
      el("div",report.expectations_met===true
        ?(hasHealthy&&hasContradictory
          ?"Human approval was necessary, but not sufficient: healthy evidence executed once; contradictory evidence still blocked."
          :"The exact approved payload executed once; a changed payload and replay stayed blocked.")
        :"The challenge resolved, but the service did not report every expected approval and Truth transition.","status"))}
  if(phases.length){const cases=el("div",undefined,"approval-result-grid");
    for(const phase of phases)cases.append(phaseCard(phase));root.append(cases)}
  if(attempts.length){root.append(el("div","Exact-scope and replay checks","section"));
    const cards=el("div",undefined,"approval-attempts");
    for(const attempt of attempts)cards.append(attemptCard(attempt));root.append(cards)}
  const p=approvalProposal(report),audit=report.audit||{},approval=report.approval||audit.approval||{},
    claim=report.claim||audit.claim||{},completion=report.completion||audit.completion||{},action=report.action||{};
  root.append(el("div","Privacy-safe approval audit","section"));
  const grid=el("div",undefined,"detail-grid");
  approvalDetail(grid,"Proposal",approvalIdentifier(report));
  approvalDetail(grid,"Actor",firstValue(p.agent_id,p.actor_id,approval.actor_id));
  approvalDetail(grid,"Capability",firstValue(p.capability_id,p.tool_id,approval.capability_id));
  approvalDetail(grid,"Action",firstValue(p.action_id,p.action_kind,action.action_id,approval.action_id));
  approvalDetail(grid,"Payload SHA-256",firstValue(p.payload_sha256,p.scope_sha256,approval.payload_sha256),"hash");
  approvalDetail(grid,"Truth receipt",firstValue(p.truth_storage_id,p.storage_id,approval.truth_storage_id,report.truth_storage_id));
  approvalDetail(grid,"Approved",firstValue(approval.approved_at,approval.decided_at,report.approved_at,
    rejected?"not approved":"not reported"));
  approvalDetail(grid,"Consumed",firstValue(approval.consumed_at,claim.claimed_at,report.consumed_at,
    rejected?"not consumed":"not reported"));
  approvalDetail(grid,"Truth Gate decision",firstValue(claim.gate_decision_id,report.gate_decision_id,
    rejected?"not invoked":"not reported"));
  approvalDetail(grid,"Completion",firstValue(completion.outcome,completion.reason_code,
    rejected?"not invoked":"not reported"));
  approvalDetail(grid,"Execution count",firstValue(action.execution_count,action.executions,action.final_outbox_rows,0));
  approvalDetail(grid,"External calls",firstValue(report.external_calls,0));root.append(grid,
    el("div","Generated local proof only. No email, message, or provider call is represented as sent.","status"))}
async function resolveApproval(report,disposition,...buttons){const proposalId=approvalIdentifier(report);
  if(!proposalId){$("approvalStatus").textContent="Approval request is missing a safe proposal identifier.";return}
  for(const button of buttons)button.disabled=true;
  $("approvalStatus").textContent=disposition==="approved"
    ?"Applying this exact approval, then testing Truth and replay controls…"
    :"Rejecting the request and verifying that no action executes…";
  try{const resolved=await api(`/api/v1/judge/approval-center/${encodeURIComponent(proposalId)}/${
      disposition==="approved"?"approve":"reject"}`,
      {method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});
    renderApprovalResolved(resolved);$("approvalStatus").textContent=disposition==="approved"
      ?(resolved.expectations_met===true
        ?(approvalPhases(resolved).some(phase=>String(firstValue(phase.name,phase.phase,"")).includes("contradict"))
          ?"Verified: healthy evidence executed once; contradictory evidence and unsafe reuse were blocked."
          :"Verified: the exact approved payload executed once; payload changes and replay were blocked.")
        :"Resolved safely; inspect the reported controls.")
      :"Rejected: the generated request stayed blocked.";
    await Promise.all([refresh(),loadApprovals()])}
  catch(e){$("approvalStatus").textContent=`Approval resolution failed safely: ${e.message}`;
    for(const button of buttons)button.disabled=false}}
function downloadApproval(){if(!state.approval)return;const body=JSON.stringify(state.approval,null,2);
  const url=URL.createObjectURL(new Blob([body],{type:"application/json"}));const link=el("a");
  link.href=url;link.download=`${approvalIdentifier(state.approval)||"globus-approval-proof"}.json`;
  document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),0)}
function challengePhase(name){return (state.challenge?.phases||[]).find(p=>p.name===name)||{}}
function shortHash(value){const text=String(value||"");return text?`${text.slice(0,16)}…`:"—"}
function renderChallenge(report){state.challenge=report;const root=$("challengeBody");root.replaceChildren();
  const artifact=report.artifact||{},clean=challengePhase("before_tamper"),caught=challengePhase("after_tamper");
  root.append(el("span",report.expectations_met?"challenge passed":"challenge needs attention",
    "badge "+(report.expectations_met?"healthy":"failed")));
  root.append(el("div",`Challenge ${report.challenge_id||"unknown"} used real local bytes and ${report.external_calls??0} external calls.`,"status"));
  const flow=el("div",undefined,"challenge-flow");
  const before=artifact.expected_bytes,after=artifact.final_bytes;
  const appended=Number.isInteger(before)&&Number.isInteger(after)?after-before:"?";
  const steps=[
    ["1 · Written",`${before??"?"} bytes`],
    ["2 · Verified",labels[clean.verdict]||clean.verdict||"unknown"],
    ["3 · Tampered",`+${appended} byte`],
    [report.expectations_met?"4 · Caught":"4 · Result",labels[caught.verdict]||caught.verdict||"unknown"]];
  for(const [label,value] of steps){const card=el("div",undefined,"challenge-step");
    card.append(el("small",label),el("strong",value));flow.append(card)}root.append(flow);
  const grid=el("div",undefined,"detail-grid");pair(grid,"Artifact",artifact.name);
  pair(grid,"Before / after",`${before??"?"} / ${after??"?"} bytes`);
  pair(grid,"Expected SHA-256",shortHash(artifact.expected_sha256));
  pair(grid,"Observed after tamper",shortHash(artifact.final_sha256));root.append(grid);
  root.append(el("div","What happened","section"),
    el("div",report.expectations_met
      ?"The first receipt remains an immutable point-in-time verification. The second re-verification records the changed bytes as a new contradictory receipt."
      :"The challenge completed, but it did not prove the expected one-byte healthy-to-contradictory transition. Inspect both receipts before trusting the result.","sub"))}
function inspectChallenge(name){const phase=challengePhase(name);
  const item=state.runs.find(run=>run.storage_id===phase.storage_id);
  if(!item)return alert("Receipt is not available in the current list.");
  $("challenge").close();showDetail(item)}
function downloadChallenge(){if(!state.challenge)return;const body=JSON.stringify(state.challenge,null,2);
  const url=URL.createObjectURL(new Blob([body],{type:"application/json"}));const link=el("a");
  link.href=url;link.download=`${state.challenge.challenge_id||"globus-truth-challenge"}.json`;
  document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),0)}
async function api(path,options){const res=await fetch(path,options);let data={};try{data=await res.json()}catch(_){}
  if(!res.ok)throw new Error(data.error||`HTTP ${res.status}`);return data}
async function refresh(){try{const [summary,list]=await Promise.all([api("/api/v1/summary"),api("/api/v1/runs?limit=200")]);
  state.runs=list.runs||[];$("trusted").textContent=summary.trusted;$("total").textContent=summary.total;
  $("attention").textContent=summary.attention;$("agents").textContent=new Set(state.runs.map(x=>x.receipt?.agent_id).filter(Boolean)).size;
  renderRows(state.runs)}catch(e){$("empty").hidden=false;$("empty").textContent=`Could not load: ${e.message}`}}
async function loadPlatform(){try{const platform=await api("/api/v1/platform/capabilities");
  state.platform=platform;const summary=platform.summary||{},headline=summary.headline||{};
  $("platformAgents").textContent=headline.built_in_agents??"—";
  $("platformTools").textContent=headline.llm_tools??"—";
  $("platformAdapters").textContent=headline.implemented_provider_adapters??"—";
  $("firewallAgents").textContent=headline.built_in_agents??"—";
  $("firewallTools").textContent=headline.llm_tools??"—";
  const capabilities=Array.isArray(platform.capabilities)?platform.capabilities:[];
  $("firewallExplicit").textContent=capabilities.filter(item=>item?.kind==="tool"&&
    item?.risk==="high"&&item?.approval==="explicit").length||"—";
  if(summary.disclosure)$("platformDisclosure").textContent=summary.disclosure}
  catch(e){$("platformDisclosure").textContent=`Capability inventory unavailable: ${e.message}`}}
async function loadApprovals(){try{const result=await api("/api/v1/approvals?limit=100");
  state.approvals=Array.isArray(result)?result:(result.proposals||result.approvals||[]);
  const pending=state.approvals.filter(item=>["pending","awaiting_review","staged"].includes(
    String(firstValue(item?.status,item?.state,"")).toLowerCase())).length;
  if(pending>0&&!state.approval)$("approvalStatus").textContent=`${pending} generated approval request${
    pending===1?" is":"s are"} awaiting review.`}
  catch(e){$("approvalStatus").textContent=`Approval Center unavailable: ${e.message}`}}
$("loadSamples").addEventListener("click",async()=>{const b=$("loadSamples");b.disabled=true;try{
  await api("/api/v1/samples/load",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});await refresh()}
  catch(e){alert(e.message)}finally{b.disabled=false}});
$("runChallenge").addEventListener("click",async()=>{const b=$("runChallenge"),out=$("challengeStatus");
  b.disabled=true;out.textContent="Writing, verifying, changing one byte, and verifying again…";try{
    const report=await api("/api/v1/judge/challenge",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});
    await refresh();renderChallenge(report);out.textContent=report.expectations_met
      ?"Caught: the changed artifact could not stay green."
      :"Challenge completed, but the expected mismatch was not proven.";
    $("challenge").showModal()}catch(e){out.textContent=`Challenge failed safely: ${e.message}`}finally{b.disabled=false}});
$("runOutcome").addEventListener("click",async()=>{const b=$("runOutcome"),out=$("outcomeStatus");
  b.disabled=true;out.textContent="Creating 3 follow-ups, reading them back, and testing the Action Gate…";
  try{const report=await api("/api/v1/judge/outcome-gate",
      {method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});
    await refresh();renderOutcome(report);out.textContent=report.expectations_met
      ?"Verified: 3 → 3 executed once; 3 → 2 was blocked."
      :"Workflow finished, but the expected allow-to-block transition was not proven.";
    $("outcome").showModal()}catch(e){out.textContent=`Outcome Gate failed safely: ${e.message}`}
  finally{b.disabled=false}});
$("stageApproval").addEventListener("click",async()=>{const b=$("stageApproval"),out=$("approvalStatus");
  b.disabled=true;out.textContent="Staging one generated local request without executing it…";
  try{const report=await api("/api/v1/judge/approval-center/stage",
      {method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});
    renderApprovalPending(report);out.textContent="Paused safely: review the exact request before anything can execute.";
    $("approval").showModal();await loadApprovals()}
  catch(e){out.textContent=`Approval request failed safely: ${e.message}`}
  finally{b.disabled=false}});
$("refresh").addEventListener("click",()=>Promise.all([refresh(),loadPlatform(),loadApprovals()]));
$("showIngest").addEventListener("click",async()=>{if(!$("receiptJson").value){try{
  const data=await api("/api/v1/samples");$("receiptJson").value=JSON.stringify(data.receipts?.[0]||{},null,2)}catch(_){}}
  $("ingestStatus").textContent="";$("ingest").showModal()});
$("submitReceipt").addEventListener("click",async()=>{const out=$("ingestStatus");out.className="status";out.textContent="Evaluating…";
  try{const result=await api("/api/v1/receipts",{method:"POST",headers:{"Content-Type":"application/json"},
    body:$("receiptJson").value});out.textContent=`Stored: ${result.evaluation.verdict}`;await refresh()}
  catch(e){out.className="status bad";out.textContent=e.message}});
$("inspectClean").addEventListener("click",()=>inspectChallenge("before_tamper"));
$("inspectCaught").addEventListener("click",()=>inspectChallenge("after_tamper"));
$("downloadChallenge").addEventListener("click",downloadChallenge);
$("downloadOutcome").addEventListener("click",downloadOutcome);
$("downloadApproval").addEventListener("click",downloadApproval);
document.querySelectorAll("[data-close]").forEach(b=>b.addEventListener("click",()=>$(b.dataset.close).close()));
Promise.all([refresh(),loadPlatform(),loadApprovals()]);
</script>
</body></html>"""


def _strict_json(data: bytes) -> Any:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(
        data.decode("utf-8"),
        object_pairs_hook=unique_object,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"non-finite number: {value}")
        ),
    )


class TruthHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], service: TruthService) -> None:
        bind_host = str(address[0]).strip().lower()
        try:
            loopback = bind_host == "localhost" or ip_address(bind_host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise ValueError(
                "Globus Truth HTTP is local-only; bind to 127.0.0.1, ::1, "
                "or localhost"
            )
        self.service = service
        super().__init__(address, TruthRequestHandler)


class TruthRequestHandler(BaseHTTPRequestHandler):
    server: TruthHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[truth-http] {self.address_string()} {fmt % args}")

    def _headers(self, status: int, content_type: str, length: int) -> None:
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Connection", "close")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'",
        )
        self.end_headers()

    def _json(self, status: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _host_allowed(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        return (
            host == "localhost"
            or host.startswith("localhost:")
            or host == "127.0.0.1"
            or host.startswith("127.0.0.1:")
            or host == "[::1]"
            or host.startswith("[::1]:")
        )

    def _route(self) -> tuple[str, dict[str, list[str]]]:
        parsed = urlsplit(self.path)
        return parsed.path, parse_qs(parsed.query, keep_blank_values=True)

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_allowed():
            self._error(421, "invalid Host header for a localhost service")
            return
        path, query = self._route()
        if path == "/":
            body = DASHBOARD_HTML.encode("utf-8")
            self._headers(200, "text/html; charset=utf-8", len(body))
            self.wfile.write(body)
            return
        if path == "/favicon.svg":
            self._headers(200, "image/svg+xml; charset=utf-8", len(FAVICON_SVG))
            self.wfile.write(FAVICON_SVG)
            return
        if path == "/favicon.ico":
            self._headers(204, "image/x-icon", 0)
            return
        if path == "/api/v1/summary":
            self._json(200, self.server.service.summary())
            return
        if path == "/api/v1/runs":
            try:
                limit = int(query.get("limit", ["100"])[0])
                offset = int(query.get("offset", ["0"])[0])
                runs = self.server.service.list_runs(limit=limit, offset=offset)
            except (ValueError, TypeError):
                self._error(400, "limit must be 1-500 and offset must be non-negative")
                return
            self._json(200, {"runs": runs, "limit": limit, "offset": offset})
            return
        if path.startswith("/api/v1/runs/"):
            storage_id = unquote(path.removeprefix("/api/v1/runs/"))
            if not _SAFE_STORAGE_ID.fullmatch(storage_id):
                self._error(400, "invalid receipt identifier")
                return
            run = self.server.service.get_run(storage_id)
            if run is None:
                self._error(404, "receipt not found")
            else:
                self._json(200, run)
            return
        if path == "/api/v1/platform/capabilities":
            try:
                capabilities = self.server.service.platform_capabilities()
            except Exception as exc:
                print(
                    "[truth-http] platform inventory failed safely: "
                    f"{type(exc).__name__}"
                )
                self._error(500, "platform capabilities unavailable safely")
                return
            self._json(200, capabilities)
            return
        if path.startswith("/api/v1/gate/decisions/"):
            decision_id = unquote(
                path.removeprefix("/api/v1/gate/decisions/")
            )
            if not _SAFE_DECISION_ID.fullmatch(decision_id):
                self._error(400, "invalid action decision identifier")
                return
            try:
                decision = self.server.service.get_action_decision(decision_id)
            except Exception as exc:
                print(
                    "[truth-http] action decision lookup failed safely: "
                    f"{type(exc).__name__}"
                )
                self._error(500, "action decision unavailable safely")
                return
            if decision is None:
                self._error(404, "action decision not found")
            else:
                self._json(200, decision)
            return
        if path == "/api/v1/approvals":
            try:
                limit = int(query.get("limit", ["100"])[0])
                if not 1 <= limit <= 500:
                    raise ValueError
            except (ValueError, TypeError):
                self._error(400, "limit must be 1-500")
                return
            try:
                proposals = self.server.service.list_approval_proposals(
                    limit=limit
                )
                if not isinstance(proposals, list):
                    raise TypeError("approval proposal list unavailable")
            except Exception as exc:
                print(
                    "[truth-http] approval list failed safely: "
                    f"{type(exc).__name__}"
                )
                self._error(500, "approval proposals unavailable safely")
                return
            self._json(200, {"proposals": proposals, "limit": limit})
            return
        if path.startswith("/api/v1/approvals/"):
            proposal_id = unquote(path.removeprefix("/api/v1/approvals/"))
            if not _SAFE_PROPOSAL_ID.fullmatch(proposal_id):
                self._error(400, "invalid approval proposal identifier")
                return
            try:
                proposal = self.server.service.get_approval_proposal(
                    proposal_id
                )
            except Exception as exc:
                print(
                    "[truth-http] approval lookup failed safely: "
                    f"{type(exc).__name__}"
                )
                self._error(500, "approval proposal unavailable safely")
                return
            if proposal is None:
                self._error(404, "approval proposal not found")
            else:
                self._json(200, proposal)
            return
        if path == "/api/v1/samples":
            self._json(200, {"receipts": self.server.service.samples()})
            return
        self._error(404, "not found")

    def _read_json(self) -> Any:
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            self._error(400, "chunked request bodies are not accepted")
            return _REJECTED_JSON
        length_text = self.headers.get("Content-Length")
        if length_text is None:
            self._error(411, "Content-Length is required")
            return _REJECTED_JSON
        try:
            length = int(length_text)
        except ValueError:
            self._error(400, "invalid Content-Length")
            return _REJECTED_JSON
        if length < 0 or length > MAX_REQUEST_BYTES:
            # Drain one bounded request window before closing. This lets Windows
            # clients receive the 413 instead of seeing a reset while still
            # refusing an unbounded upload.
            if length > 0:
                self.rfile.read(min(length, MAX_REQUEST_BYTES + 1))
            self._error(413, f"request body exceeds {MAX_REQUEST_BYTES} bytes")
            return _REJECTED_JSON
        data = self.rfile.read(length)
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._error(415, "Content-Type must be application/json")
            return _REJECTED_JSON
        try:
            return _strict_json(data)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
            self._error(400, "request body is not strict UTF-8 JSON")
            return _REJECTED_JSON

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_allowed():
            self._error(421, "invalid Host header for a localhost service")
            return
        path, _ = self._route()
        payload = self._read_json()
        if payload is _REJECTED_JSON:
            return
        if path == "/api/v1/receipts":
            if not isinstance(payload, Mapping):
                self._error(400, "receipt must be a JSON object")
                return
            try:
                result = self.server.service.ingest(payload)
            except ReceiptConflict as exc:
                self._error(409, str(exc))
                return
            except (TypeError, ValueError):
                self._error(400, "receipt could not be evaluated")
                return
            self._json(201 if result["created"] else 200, result)
            return
        if path == "/api/v1/approvals":
            required = {
                "proposal_id",
                "storage_id",
                "action_id",
                "policy_id",
                "action_kind",
                "payload_sha256",
                "requested_by",
                "risk",
                "expires_at",
            }
            if not isinstance(payload, Mapping) or set(payload) != required:
                self._error(400, "approval proposal fields are invalid")
                return
            try:
                result = self.server.service.submit_action_proposal(**payload)
            except ValueError:
                self._error(400, "approval proposal could not be validated")
                return
            except Exception as exc:
                print(
                    "[truth-http] approval proposal write failed safely: "
                    f"{type(exc).__name__}"
                )
                self._error(500, "approval proposal unavailable safely")
                return
            self._json(201 if result["created"] else 200, result)
            return
        decision_match = re.fullmatch(
            r"/api/v1/approvals/([^/]+)/decision",
            path,
        )
        if decision_match is not None:
            proposal_id = unquote(decision_match.group(1))
            if not _SAFE_PROPOSAL_ID.fullmatch(proposal_id):
                self._error(400, "invalid approval proposal identifier")
                return
            if (
                not isinstance(payload, Mapping)
                or set(payload) != {"outcome", "decided_by", "reason_code"}
            ):
                self._error(400, "human decision fields are invalid")
                return
            try:
                result = self.server.service.decide_action_proposal(
                    proposal_id,
                    outcome=payload["outcome"],
                    decided_by=payload["decided_by"],
                    reason_code=payload["reason_code"],
                )
            except ApprovalNotFoundError:
                self._error(404, "approval proposal not found")
                return
            except ApprovalCenterError:
                self._error(409, "proposal cannot accept that decision")
                return
            except ValueError:
                self._error(400, "human decision could not be validated")
                return
            except Exception as exc:
                print(
                    "[truth-http] human decision write failed safely: "
                    f"{type(exc).__name__}"
                )
                self._error(500, "human decision unavailable safely")
                return
            self._json(201 if result["created"] else 200, result)
            return
        if path == "/api/v1/samples/load":
            if payload != {}:
                self._error(400, "sample loader accepts only an empty JSON object")
                return
            self._json(200, self.server.service.load_demo())
            return
        if path == "/api/v1/judge/challenge":
            if payload != {}:
                self._error(400, "judge challenge accepts only an empty JSON object")
                return
            try:
                result = self.server.service.run_judge_challenge()
            except Exception as exc:
                print(
                    "[truth-http] judge challenge failed safely: "
                    f"{type(exc).__name__}"
                )
                self._error(500, "judge challenge failed safely")
                return
            self._json(201, result)
            return
        if path == "/api/v1/judge/outcome-gate":
            if payload != {}:
                self._error(400, "outcome gate accepts only an empty JSON object")
                return
            try:
                result = self.server.service.run_outcome_gate_challenge()
            except Exception as exc:
                print(
                    "[truth-http] outcome gate failed safely: "
                    f"{type(exc).__name__}"
                )
                self._error(500, "outcome gate failed safely")
                return
            self._json(201, result)
            return
        if path == "/api/v1/judge/approval-center/stage":
            if payload != {}:
                self._error(
                    400,
                    "approval challenge stage accepts only an empty JSON object",
                )
                return
            try:
                result = self.server.service.stage_approval_challenge()
                if not isinstance(result, Mapping):
                    raise TypeError("approval challenge report unavailable")
            except Exception as exc:
                print(
                    "[truth-http] approval challenge stage failed safely: "
                    f"{type(exc).__name__}"
                )
                self._error(500, "approval challenge failed safely")
                return
            self._json(201, result)
            return
        approval_match = re.fullmatch(
            r"/api/v1/judge/approval-center/([^/]+)/(approve|reject)",
            path,
        )
        if approval_match is not None:
            if payload != {}:
                self._error(
                    400,
                    "approval challenge resolution accepts only an empty JSON object",
                )
                return
            proposal_id = unquote(approval_match.group(1))
            if not _SAFE_PROPOSAL_ID.fullmatch(proposal_id):
                self._error(400, "invalid approval proposal identifier")
                return
            action = approval_match.group(2)
            disposition = "approved" if action == "approve" else "rejected"
            try:
                result = self.server.service.resolve_approval_challenge(
                    proposal_id,
                    disposition=disposition,
                )
                if not isinstance(result, Mapping):
                    raise TypeError("approval challenge report unavailable")
            except Exception as exc:
                print(
                    "[truth-http] approval challenge resolution failed safely: "
                    f"{type(exc).__name__}"
                )
                self._error(500, "approval challenge resolution failed safely")
                return
            self._json(200, result)
            return
        self._error(404, "not found")

    def do_PUT(self) -> None:  # noqa: N802
        self._error(405, "method not allowed")

    def do_DELETE(self) -> None:  # noqa: N802
        self._error(405, "method not allowed")
