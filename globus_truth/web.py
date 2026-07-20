"""Local-only stdlib HTTP dashboard and JSON API."""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlsplit

from .service import TruthService
from .storage import ReceiptConflict

MAX_REQUEST_BYTES = 64 * 1024
_SAFE_STORAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
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
  <title>Globus Truth Layer</title>
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
    textarea{width:100%;min-height:220px;resize:vertical;background:#080e19;color:#dbe5ff;
      border:1px solid var(--line);border-radius:10px;padding:13px;font-family:ui-monospace,monospace;font-size:12px}
    .status{min-height:22px;color:var(--muted);margin-top:8px}.status.bad{color:var(--bad)}
    footer{color:var(--muted);font-size:12px;margin-top:20px;text-align:center}
    @media(max-width:760px){header{display:block}.pulse{margin-top:18px}.lab{grid-template-columns:1fr}
      .challenge-flow{grid-template-columns:1fr 1fr}.summary{grid-template-columns:1fr 1fr}
      .metric.hero{grid-column:1/-1}.toolbar{align-items:flex-start;flex-direction:column}.detail-grid{grid-template-columns:1fr}}
  </style>
</head>
<body>
<main class="shell">
  <header>
    <div><div class="eyebrow">Evidence before confidence</div><h1>Globus Truth Layer</h1>
      <p class="lede">A local judge for agent-run claims. Every green light is backed by
      measurements, every quiet run proves it actually ran, and contradictions stay visible.</p></div>
    <div class="pulse"><i></i><span>localhost · private by default</span></div>
  </header>
  <section class="lab panel">
    <div><div class="eyebrow">60-second evidence lab</div><h2>Change one byte. Watch Globus catch it.</h2>
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
const $=id=>document.getElementById(id), state={runs:[],challenge:null};
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
$("refresh").addEventListener("click",refresh);
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
document.querySelectorAll("[data-close]").forEach(b=>b.addEventListener("click",()=>$(b.dataset.close).close()));
refresh();
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
        bound = str(self.server.server_address[0]).lower()
        if bound not in {"127.0.0.1", "::1", "localhost"}:
            return True
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
        self._error(404, "not found")

    def do_PUT(self) -> None:  # noqa: N802
        self._error(405, "method not allowed")

    def do_DELETE(self) -> None:  # noqa: N802
        self._error(405, "method not allowed")
