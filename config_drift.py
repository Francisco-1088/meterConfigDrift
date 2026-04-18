#!/usr/bin/env python3
"""
config_drift.py
===============
Standalone Configuration Drift tool for Meter Networks.

Compares VLAN and SSID configuration between any two networks across
any of the companies in compliance_config.COMPANIES.

Usage:
    python3 config_drift.py           # http://localhost:8083
    PORT=9000 python3 config_drift.py
"""

import os
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import compliance_config as config
import requests
from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────

API_URL   = config.API_URL
API_TOKEN = config.API_TOKEN
COMPANIES = config.COMPANIES

MAX_RETRIES         = 3
PROACTIVE_THRESHOLD = 20

_GQL_HEADERS = {
    "Content-Type":  "application/json",
    "Authorization": f"Bearer {API_TOKEN}",
}

# ── Rate-limit state ───────────────────────────────────────────────────────────

_rl_remaining = None
_rl_reset     = None
_rl_lock      = threading.Lock()


def _parse_rfc1123(value):
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except Exception:
        return None


def _update_rl(headers):
    global _rl_remaining, _rl_reset
    with _rl_lock:
        r = headers.get("X-RateLimit-Remaining")
        if r is not None:
            try:
                _rl_remaining = int(r)
            except ValueError:
                pass
        rs = headers.get("X-RateLimit-Reset")
        if rs:
            _rl_reset = _parse_rfc1123(rs)


def _proactive_sleep():
    with _rl_lock:
        remaining = _rl_remaining
        reset_dt  = _rl_reset
    if remaining is not None and remaining < PROACTIVE_THRESHOLD:
        wait = (max(0.0, (reset_dt - datetime.now(timezone.utc)).total_seconds()) + 1.0
                if reset_dt else 5.0)
        time.sleep(wait)


def gql(query: str) -> dict:
    for attempt in range(1, MAX_RETRIES + 1):
        _proactive_sleep()
        try:
            resp = requests.post(
                API_URL, json={"query": query},
                headers=_GQL_HEADERS, timeout=30
            )
            _update_rl(resp.headers)

            if resp.status_code == 429:
                retry_dt = _parse_rfc1123(resp.headers.get("Retry-After"))
                wait = (max(0.0, (retry_dt - datetime.now(timezone.utc)).total_seconds()) + 1.0
                        if retry_dt else 60.0 * attempt)
                time.sleep(wait)
                continue
            if resp.status_code == 401:
                return {"error": "HTTP 401 Unauthorized"}
            if resp.status_code in (400, 422):
                body = {}
                try:
                    body = resp.json()
                except Exception:
                    pass
                return {"error": f"HTTP {resp.status_code}",
                        "messages": [e.get("message", "") for e in body.get("errors", [])]}
            resp.raise_for_status()

            body = resp.json()
            if "errors" in body and body.get("data") is None:
                codes = [e.get("extensions", {}).get("code", "UNKNOWN") for e in body["errors"]]
                msgs  = [e.get("message") or "" for e in body["errors"]]
                if "UNAUTHORIZED" in codes:
                    return {"error": "UNAUTHORIZED", "messages": msgs}
                return {"error": f"GraphQL {', '.join(codes)}", "messages": msgs}
            return body

        except requests.Timeout:
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
        except requests.ConnectionError as exc:
            return {"error": f"Connection error: {exc}"}

    return {"error": f"All {MAX_RETRIES} attempts failed"}


# ── Network cache (populated at startup) ──────────────────────────────────────

_networks_cache: dict = {}   # { slug: [{ UUID, label }] }
_networks_lock  = threading.Lock()
_networks_ready = False


def _fetch_networks_for_slug(slug: str) -> list:
    r = gql(f"""{{
      networksForCompany(companySlug: "{slug}") {{
        UUID label slug
      }}
    }}""")
    if "error" in r:
        print(f"  [{slug}] Error fetching networks: {r['error']}", flush=True)
        return []
    return (r.get("data") or {}).get("networksForCompany") or []


def _preload_networks() -> None:
    global _networks_ready
    print("Preloading network lists…", flush=True)
    for slug in COMPANIES:
        nets = _fetch_networks_for_slug(slug)
        with _networks_lock:
            _networks_cache[slug] = nets
        print(f"  [{slug}] {len(nets)} network(s)", flush=True)
    _networks_ready = True
    print("Network lists ready.", flush=True)


# ── Data fetch ─────────────────────────────────────────────────────────────────

def fetch_network_data(nid: str) -> dict:
    r = gql(f"""{{
      vlans(networkUUID: "{nid}") {{
        UUID name vlanID isEnabled
        ipV4ClientGateway ipV4ClientPrefixLength ipV4ClientAssignmentProtocol
      }}
      ssids: ssidsForNetwork(networkUUID: "{nid}") {{
        UUID ssid isEnabled encryptionProtocol
      }}
    }}""")
    if "error" in r:
        return {"error": r.get("error"), "vlans": [], "ssids": []}
    d = r.get("data") or {}
    return {
        "vlans": d.get("vlans") or [],
        "ssids": d.get("ssids") or [],
    }


# ── Flask routes ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/api/networks")
def api_networks():
    with _networks_lock:
        companies = [
            {"slug": slug, "networks": _networks_cache.get(slug, [])}
            for slug in COMPANIES
        ]
    return jsonify({"companies": companies, "ready": _networks_ready})


@app.route("/api/networks/refresh", methods=["POST"])
def api_networks_refresh():
    threading.Thread(target=_preload_networks, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/compare", methods=["POST"])
def api_compare():
    body  = request.get_json() or {}
    uuid_a = body.get("uuidA", "")
    uuid_b = body.get("uuidB", "")
    if not uuid_a or not uuid_b:
        return jsonify({"error": "uuidA and uuidB are required"}), 400

    results = {}
    errors  = []

    def _fetch(key, nid):
        results[key] = fetch_network_data(nid)

    ta = threading.Thread(target=_fetch, args=("a", uuid_a))
    tb = threading.Thread(target=_fetch, args=("b", uuid_b))
    ta.start(); tb.start()
    ta.join();  tb.join()

    return jsonify({"a": results.get("a", {}), "b": results.get("b", {})})


# ── HTML template ──────────────────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Meter — Configuration Drift</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg-base:#0f1117;
  --bg-sidebar:#13151f;
  --bg-section:#1a1c27;
  --bg-card:#1e2030;
  --bg-card-hover:#252840;
  --bg-active:#2d3480;
  --border:#232538;
  --border2:#2a2d42;
  --text-primary:#e8e9f2;
  --text-secondary:#8b8fa8;
  --text-muted:#555870;
  --green:#3ecf6e;
  --red:#f05252;
  --yellow:#f59e0b;
  --blue:#6e80f8;
  --radius:6px;
  --topbar-h:44px;
}
body{font-family:'Suisse Int\'l',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  background:var(--bg-base);color:var(--text-primary);
  display:flex;flex-direction:column;height:100vh;overflow:hidden;font-size:13px;line-height:1.5}

/* ── Light mode ── */
body.light{
  --bg-base:#f3f4f8;--bg-sidebar:#ffffff;--bg-section:#eef0f6;
  --bg-card:#ffffff;--bg-card-hover:#e8eaf2;--bg-active:#dde2ff;
  --border:#dde0ec;--border2:#cdd0e0;
  --text-primary:#1a1c2e;--text-secondary:#555870;--text-muted:#9099b8;
}

/* ── Topbar ── */
.topbar{display:flex;align-items:center;padding:0 20px;height:var(--topbar-h);flex-shrink:0;
  border-bottom:1px solid var(--border);background:var(--bg-sidebar);gap:10px}
.logo-mark{width:24px;height:24px;background:var(--blue);border-radius:7px;
  display:flex;align-items:center;justify-content:center;
  font-weight:800;font-size:12px;color:#fff;flex-shrink:0}
.topbar-title{font-size:13px;font-weight:600;color:var(--text-primary);letter-spacing:-.1px}
.topbar-sub{font-size:11px;color:var(--text-muted)}
.topbar-right{margin-left:auto;display:flex;align-items:center;gap:8px}
.pulse-wrap{display:flex;align-items:center;gap:5px;padding:3px 9px;
  background:var(--bg-section);border:1px solid var(--border);border-radius:5px;
  font-size:11px;color:var(--text-secondary)}
.pulse{width:7px;height:7px;border-radius:50%;background:var(--green);
  animation:pulse 2.2s ease-in-out infinite;flex-shrink:0}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
.theme-toggle{display:flex;align-items:center;justify-content:center;
  width:30px;height:30px;border-radius:6px;cursor:pointer;
  background:var(--bg-section);border:1px solid var(--border2);
  color:var(--text-secondary);font-size:15px;transition:background .15s}
.theme-toggle:hover{background:var(--bg-card-hover)}

/* ── Content ── */
.content-area{flex:1;overflow-y:auto;background:var(--bg-base)}
.content-area::-webkit-scrollbar{width:5px}
.content-area::-webkit-scrollbar-thumb{background:var(--border2);border-radius:3px}
.sec-inner{padding:24px 28px;max-width:1400px}
.sec-hdr{margin-bottom:20px}
.sec-hdr h2{font-size:15px;font-weight:600}
.sec-hdr p{font-size:12px;color:var(--text-secondary);margin-top:3px}

/* ── Buttons ── */
.btn{display:inline-flex;align-items:center;gap:5px;background:var(--bg-card);
  border:1px solid var(--border2);border-radius:var(--radius);
  color:var(--text-secondary);padding:5px 12px;cursor:pointer;
  font-size:12px;font-family:inherit;white-space:nowrap;transition:color .1s,background .1s}
.btn:hover{background:var(--bg-card-hover);color:var(--text-primary)}
.btn-primary{background:rgba(110,128,248,.12);border-color:rgba(110,128,248,.3);color:var(--blue)}
.btn-primary:hover{background:var(--blue);color:#fff;border-color:var(--blue)}

/* ── Loading / empty ── */
.loading,.empty-state{padding:48px;text-align:center;color:var(--text-muted)}
.spinner{width:18px;height:18px;border:2px solid var(--border2);
  border-top-color:var(--blue);border-radius:50%;
  animation:spin .7s linear infinite;margin:0 auto 12px}
@keyframes spin{to{transform:rotate(360deg)}}
.loading-title,.empty-state-title{font-size:13px;font-weight:500;color:var(--text-secondary);margin-bottom:4px}
.empty-state-sub{font-size:12px}
.error-banner{background:rgba(240,82,82,.1);border:1px solid rgba(240,82,82,.25);
  border-radius:var(--radius);padding:10px 14px;font-size:12px;color:var(--red);margin-bottom:16px}

/* ── Drift controls ── */
.drift-controls{display:flex;gap:10px;align-items:center;margin-bottom:20px;flex-wrap:wrap}
.drift-select{background:var(--bg-section);border:1px solid var(--border);border-radius:var(--radius);
  color:var(--text-primary);padding:6px 10px;font-size:12px;font-family:inherit;outline:none;min-width:160px}
.drift-select:focus{border-color:var(--blue)}
.drift-vs{color:var(--text-muted);font-size:13px;font-weight:600;padding:0 2px;flex-shrink:0}
.drift-pair{display:flex;gap:8px;align-items:center}

/* ── Summary counters ── */
.drift-summary-row{display:flex;gap:10px;margin-bottom:20px;flex-wrap:wrap}
.drift-summ{flex:1;min-width:100px;background:var(--bg-card);border:1px solid var(--border);
  border-radius:var(--radius);padding:10px 14px;text-align:center}
.drift-summ-num{font-size:22px;font-weight:700;line-height:1}
.drift-summ-lbl{font-size:10px;text-transform:uppercase;letter-spacing:.05em;
  color:var(--text-muted);margin-top:3px}

/* ── Results grid ── */
.drift-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px}
@media(max-width:900px){.drift-grid{grid-template-columns:1fr}}
.drift-card{background:var(--bg-card);border:1px solid var(--border);border-radius:8px;overflow:hidden}
.drift-card-hdr{padding:10px 14px;border-bottom:1px solid var(--border);font-weight:600;font-size:12px;
  display:flex;align-items:center;gap:8px;color:var(--text-primary)}
.drift-card-count{font-size:11px;font-weight:400;color:var(--text-muted);margin-left:auto}

/* ── Row items ── */
.drift-row-wrap:last-child .drift-item{border-bottom:none}
.drift-item{padding:7px 14px;border-bottom:1px solid var(--border);font-size:12px;
  display:flex;gap:8px;align-items:center;cursor:pointer;user-select:none}
.drift-item:hover{background:rgba(255,255,255,.02)}
body.light .drift-item:hover{background:rgba(0,0,0,.03)}
.drift-toggle{font-size:9px;color:var(--text-muted);transition:transform .15s;
  display:inline-block;margin-right:2px;flex-shrink:0}
.drift-row-wrap.open .drift-toggle{transform:rotate(90deg)}
.drift-detail{display:none;padding:10px 14px 12px;
  background:var(--bg-section);border-bottom:1px solid var(--border)}
.drift-row-wrap.open .drift-detail{display:block}
.drift-match{color:var(--green);font-size:11px;flex-shrink:0}
.drift-miss{color:var(--red);font-size:11px;flex-shrink:0}
.drift-diff-badge{color:var(--yellow);font-size:10px;flex-shrink:0}

/* ── Comparison table inside expanded row ── */
.drift-cmp-tbl{width:100%;border-collapse:collapse;font-size:11px}
.drift-cmp-tbl td{padding:4px 8px;border-bottom:1px solid rgba(255,255,255,.04);vertical-align:top}
.drift-cmp-tbl tr:last-child td{border-bottom:none}
.drift-cmp-tbl td:first-child{color:var(--text-muted);width:90px;font-size:10px;white-space:nowrap}
.drift-cmp-tbl td.val-diff{color:var(--yellow)}
.drift-cmp-tbl th{padding:4px 8px;font-size:10px;font-weight:600;
  color:var(--text-secondary);text-align:left;border-bottom:1px solid var(--border2)}
body.light .drift-cmp-tbl td{border-bottom-color:rgba(0,0,0,.06)}

/* ── Status chips ── */
.chip{display:inline-flex;align-items:center;gap:3px;
  font-size:10px;padding:1px 6px;border-radius:4px;font-weight:600}
.chip.green{background:rgba(62,207,110,.12);color:var(--green)}
.chip.red{background:rgba(240,82,82,.12);color:var(--red)}
.chip.yellow{background:rgba(245,158,11,.12);color:var(--yellow)}
.chip.dim{background:rgba(139,143,168,.1);color:var(--text-secondary)}
</style>
</head>
<body>

<div class="topbar">
  <div class="logo-mark">M</div>
  <span class="topbar-title">Configuration Drift</span>
  <span class="topbar-sub">Compare VLAN &amp; SSID config across networks</span>
  <div class="topbar-right">
    <button class="btn" id="refresh-btn" onclick="refreshNetworks()" style="font-size:11px;padding:4px 10px">↺ Refresh</button>
    <button class="theme-toggle" id="theme-btn" onclick="toggleTheme()" title="Toggle light/dark mode">🌙</button>
    <div class="pulse-wrap">
      <div class="pulse"></div>
      <span id="status-text">Loading…</span>
    </div>
  </div>
</div>

<div class="content-area">
  <div class="sec-inner">
    <div class="sec-hdr">
      <h2>Network Comparison</h2>
      <p>Select a network on each side to compare their VLAN and SSID configurations. Differences are highlighted in yellow.</p>
    </div>

    <div class="drift-controls">
      <div class="drift-pair">
        <select class="drift-select" id="co-a" onchange="onCoChange('a')">
          <option value="">— Company A —</option>
        </select>
        <select class="drift-select" id="net-a" onchange="onNetChange()">
          <option value="">— Network A —</option>
        </select>
      </div>
      <span class="drift-vs">vs</span>
      <div class="drift-pair">
        <select class="drift-select" id="co-b" onchange="onCoChange('b')">
          <option value="">— Company B —</option>
        </select>
        <select class="drift-select" id="net-b" onchange="onNetChange()">
          <option value="">— Network B —</option>
        </select>
      </div>
    </div>

    <div id="summary-row" class="drift-summary-row" style="display:none"></div>
    <div id="drift-body"></div>
  </div>
</div>

<script>
// ── Theme ──────────────────────────────────────────────────────────────────────
(function(){
  if(localStorage.getItem('drift-theme')==='light') document.body.classList.add('light');
})();
function toggleTheme(){
  const light=document.body.classList.toggle('light');
  localStorage.setItem('drift-theme',light?'light':'dark');
  document.getElementById('theme-btn').textContent=light?'☀️':'🌙';
}
(function(){
  if(document.body.classList.contains('light'))
    document.getElementById('theme-btn').textContent='☀️';
})();

// ── State ──────────────────────────────────────────────────────────────────────
let companiesData = [];   // [{ slug, networks: [{UUID, label}] }]
let _comparing    = false;

function esc(s){
  return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function setStatus(msg){ document.getElementById('status-text').textContent=msg; }

// ── Load networks ──────────────────────────────────────────────────────────────
async function loadNetworks(){
  setStatus('Loading networks…');
  try{
    const r=await fetch('/api/networks');
    const j=await r.json();
    companiesData=j.companies||[];
    populateSelects();
    const total=companiesData.reduce((s,c)=>s+c.networks.length,0);
    setStatus(`${total} network(s) across ${companiesData.length} company(s)`);
    if(!j.ready) setTimeout(loadNetworks, 3000);
  } catch(e){
    setStatus('Error loading networks');
    document.getElementById('drift-body').innerHTML=
      `<div class="error-banner">Failed to load network list: ${esc(e.message)}</div>`;
  }
}

async function refreshNetworks(){
  setStatus('Refreshing…');
  const btn=document.getElementById('refresh-btn');
  btn.disabled=true;
  try{
    await fetch('/api/networks/refresh',{method:'POST'});
    await new Promise(r=>setTimeout(r,800));
    await loadNetworks();
  } finally {
    btn.disabled=false;
  }
}

// ── Dropdowns ──────────────────────────────────────────────────────────────────
function populateSelects(){
  ['a','b'].forEach(side=>{
    const sel=document.getElementById('co-'+side);
    const prev=sel.value;
    sel.innerHTML='<option value="">— Company —</option>'+
      companiesData.map(c=>`<option value="${esc(c.slug)}"${prev===c.slug?' selected':''}>${esc(c.slug)}</option>`).join('');
    populateNetSelect(side);
  });
}

function populateNetSelect(side){
  const slug=document.getElementById('co-'+side).value;
  const co=companiesData.find(c=>c.slug===slug);
  const nets=co?co.networks:[];
  const sel=document.getElementById('net-'+side);
  const prev=sel.value;
  sel.innerHTML='<option value="">— Network —</option>'+
    nets.map(n=>`<option value="${esc(n.UUID)}"${prev===n.UUID?' selected':''}>${esc(n.label)}</option>`).join('');
}

function onCoChange(side){
  populateNetSelect(side);
  onNetChange();
}

function onNetChange(){
  const uuidA=document.getElementById('net-a').value;
  const uuidB=document.getElementById('net-b').value;
  if(uuidA && uuidB) compare(uuidA, uuidB);
  else {
    document.getElementById('drift-body').innerHTML='';
    document.getElementById('summary-row').style.display='none';
  }
}

// ── Compare ────────────────────────────────────────────────────────────────────
async function compare(uuidA, uuidB){
  if(_comparing) return;
  _comparing=true;
  const body=document.getElementById('drift-body');
  const sumRow=document.getElementById('summary-row');
  body.innerHTML='<div class="loading"><div class="spinner"></div><div class="loading-title">Fetching configuration…</div></div>';
  sumRow.style.display='none';
  setStatus('Comparing…');

  try{
    const r=await fetch('/api/compare',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({uuidA,uuidB})
    });
    const j=await r.json();
    if(j.error){
      body.innerHTML=`<div class="error-banner">${esc(j.error)}</div>`;
      setStatus('Error');
      return;
    }
    renderDrift(j.a||{}, j.b||{}, uuidA, uuidB);
    setStatus('Done');
  } catch(e){
    body.innerHTML=`<div class="error-banner">Compare failed: ${esc(e.message)}</div>`;
    setStatus('Error');
  } finally {
    _comparing=false;
  }
}

// ── Helpers ────────────────────────────────────────────────────────────────────
function getNetLabel(uuid, side){
  const slug=document.getElementById('co-'+side).value;
  const co=companiesData.find(c=>c.slug===slug);
  if(!co) return uuid;
  return (co.networks.find(n=>n.UUID===uuid)||{label:uuid}).label;
}

function cmpRow(label, va, vb){
  const diff=String(va??'')!==String(vb??'');
  return `<tr>
    <td>${esc(label)}</td>
    <td>${va??'—'}</td>
    <td class="${diff?'val-diff':''}">${vb??'—'}</td>
  </tr>`;
}

function toggleRow(id){
  const w=document.getElementById('drift-r-'+id);
  if(w) w.classList.toggle('open');
}

// ── Render ─────────────────────────────────────────────────────────────────────
function renderDrift(dataA, dataB, uuidA, uuidB){
  const labelA=getNetLabel(uuidA,'a');
  const labelB=getNetLabel(uuidB,'b');

  const vlansA=dataA.vlans||[], vlansB=dataB.vlans||[];
  const ssidsA=dataA.ssids||[], ssidsB=dataB.ssids||[];

  const errBanners=[];
  if(dataA.error) errBanners.push(`<div class="error-banner">Network A error: ${esc(dataA.error)}</div>`);
  if(dataB.error) errBanners.push(`<div class="error-banner">Network B error: ${esc(dataB.error)}</div>`);

  // ── VLANs ──
  const allVlanIDs=[...new Set([...vlansA.map(v=>v.vlanID),...vlansB.map(v=>v.vlanID)])].sort((a,b)=>a-b);
  let vlanMatch=0, vlanMiss=0, vlanFieldDiff=0;
  const vlanRows=allVlanIDs.map(id=>{
    const a=vlansA.find(v=>v.vlanID===id);
    const b=vlansB.find(v=>v.vlanID===id);
    const inBoth=!!(a&&b);
    if(inBoth) vlanMatch++; else vlanMiss++;
    const diffCls=inBoth?'drift-match':'drift-miss';
    const diffLbl=inBoth?'✓ Both':(a?'← A only':'→ B only');
    const hasDiffs=inBoth&&(
      a.name!==b.name||
      (a.ipV4ClientGateway||'')!==(b.ipV4ClientGateway||'')||
      a.ipV4ClientPrefixLength!==b.ipV4ClientPrefixLength||
      (a.ipV4ClientAssignmentProtocol||'')!==(b.ipV4ClientAssignmentProtocol||'')||
      a.isEnabled!==b.isEnabled
    );
    if(hasDiffs) vlanFieldDiff++;
    const rowId='vlan-'+id;
    const detail=`<div class="drift-detail">
      <table class="drift-cmp-tbl">
        <tr>
          <th></th>
          <th>${esc(labelA)}</th>
          <th>${esc(labelB)}</th>
        </tr>
        ${cmpRow('Name', a?esc(a.name):'—', b?esc(b.name):'—')}
        ${cmpRow('Gateway', a?(a.ipV4ClientGateway||'—'):'—', b?(b.ipV4ClientGateway||'—'):'—')}
        ${cmpRow('Prefix', a?(a.ipV4ClientPrefixLength!=null?'/'+a.ipV4ClientPrefixLength:'—'):'—', b?(b.ipV4ClientPrefixLength!=null?'/'+b.ipV4ClientPrefixLength:'—'):'—')}
        ${cmpRow('DHCP', a?(a.ipV4ClientAssignmentProtocol||'—'):'—', b?(b.ipV4ClientAssignmentProtocol||'—'):'—')}
        ${cmpRow('Enabled', a?(a.isEnabled?'Yes':'No'):'—', b?(b.isEnabled?'Yes':'No'):'—')}
      </table>
    </div>`;
    return `<div class="drift-row-wrap" id="drift-r-${rowId}">
      <div class="drift-item" onclick="toggleRow('${rowId}')">
        <span class="drift-toggle">▶</span>
        <span style="min-width:42px;font-weight:600;color:var(--text-primary)">ID ${id}</span>
        <span style="flex:1;color:var(--text-secondary)">${a?esc(a.name):'—'} / ${b?esc(b.name):'—'}</span>
        ${hasDiffs?`<span class="drift-diff-badge">≠ diff</span>`:''}
        <span class="${diffCls}">${diffLbl}</span>
      </div>${detail}
    </div>`;
  }).join('');

  // ── SSIDs ──
  const allSSIDs=[...new Set([...ssidsA.map(s=>s.ssid),...ssidsB.map(s=>s.ssid)])].sort();
  let ssidMatch=0, ssidMiss=0, ssidFieldDiff=0;
  const ssidRows=allSSIDs.map(name=>{
    const a=ssidsA.find(s=>s.ssid===name);
    const b=ssidsB.find(s=>s.ssid===name);
    const inBoth=!!(a&&b);
    if(inBoth) ssidMatch++; else ssidMiss++;
    const diffCls=inBoth?'drift-match':'drift-miss';
    const diffLbl=inBoth?'✓ Both':(a?'← A only':'→ B only');
    const hasDiffs=inBoth&&(a.isEnabled!==b.isEnabled||(a.encryptionProtocol||'')!==(b.encryptionProtocol||''));
    if(hasDiffs) ssidFieldDiff++;
    const rowId='ssid-'+btoa(unescape(encodeURIComponent(name))).replace(/[^a-zA-Z0-9]/g,'').slice(0,14);
    const detail=`<div class="drift-detail">
      <table class="drift-cmp-tbl">
        <tr>
          <th></th>
          <th>${esc(labelA)}</th>
          <th>${esc(labelB)}</th>
        </tr>
        ${cmpRow('SSID', a?esc(a.ssid):'—', b?esc(b.ssid):'—')}
        ${cmpRow('Encryption', a?(a.encryptionProtocol||'Open'):'—', b?(b.encryptionProtocol||'Open'):'—')}
        ${cmpRow('Enabled', a?(a.isEnabled?'Yes':'No'):'—', b?(b.isEnabled?'Yes':'No'):'—')}
      </table>
    </div>`;
    return `<div class="drift-row-wrap" id="drift-r-${rowId}">
      <div class="drift-item" onclick="toggleRow('${rowId}')">
        <span class="drift-toggle">▶</span>
        <span style="flex:1;color:var(--text-secondary);font-weight:500">${esc(name)}</span>
        ${hasDiffs?`<span class="drift-diff-badge">≠ diff</span>`:''}
        <span class="${diffCls}">${diffLbl}</span>
      </div>${detail}
    </div>`;
  }).join('');

  // ── Summary bar ──
  const totalMiss=vlanMiss+ssidMiss;
  const totalFieldDiff=vlanFieldDiff+ssidFieldDiff;
  const sumRow=document.getElementById('summary-row');
  sumRow.style.display='flex';
  sumRow.innerHTML=`
    <div class="drift-summ">
      <div class="drift-summ-num" style="color:${vlanMatch?'var(--green)':'var(--text-muted)'}">${vlanMatch}</div>
      <div class="drift-summ-lbl">VLANs match</div>
    </div>
    <div class="drift-summ">
      <div class="drift-summ-num" style="color:${vlanMiss?'var(--red)':'var(--text-muted)'}">${vlanMiss}</div>
      <div class="drift-summ-lbl">VLAN only-one</div>
    </div>
    <div class="drift-summ">
      <div class="drift-summ-num" style="color:${vlanFieldDiff?'var(--yellow)':'var(--text-muted)'}">${vlanFieldDiff}</div>
      <div class="drift-summ-lbl">VLAN field diffs</div>
    </div>
    <div class="drift-summ">
      <div class="drift-summ-num" style="color:${ssidMatch?'var(--green)':'var(--text-muted)'}">${ssidMatch}</div>
      <div class="drift-summ-lbl">SSIDs match</div>
    </div>
    <div class="drift-summ">
      <div class="drift-summ-num" style="color:${ssidMiss?'var(--red)':'var(--text-muted)'}">${ssidMiss}</div>
      <div class="drift-summ-lbl">SSID only-one</div>
    </div>
    <div class="drift-summ">
      <div class="drift-summ-num" style="color:${ssidFieldDiff?'var(--yellow)':'var(--text-muted)'}">${ssidFieldDiff}</div>
      <div class="drift-summ-lbl">SSID field diffs</div>
    </div>
    <div class="drift-summ">
      <div class="drift-summ-num" style="color:${(totalMiss+totalFieldDiff)>0?'var(--red)':'var(--green)'}">
        ${totalMiss+totalFieldDiff}
      </div>
      <div class="drift-summ-lbl">Total diffs</div>
    </div>`;

  // ── Grid ──
  document.getElementById('drift-body').innerHTML=
    errBanners.join('')+`
    <div class="drift-grid">
      <div class="drift-card">
        <div class="drift-card-hdr">
          VLANs
          <span class="chip ${vlanMiss||vlanFieldDiff?'yellow':'green'}" style="font-size:10px">
            ${allVlanIDs.length} total
          </span>
          <span class="drift-card-count">${esc(labelA)} vs ${esc(labelB)}</span>
        </div>
        ${vlanRows||'<div class="drift-item" style="color:var(--text-muted);cursor:default">No VLANs found</div>'}
      </div>
      <div class="drift-card">
        <div class="drift-card-hdr">
          SSIDs
          <span class="chip ${ssidMiss||ssidFieldDiff?'yellow':'green'}" style="font-size:10px">
            ${allSSIDs.length} total
          </span>
          <span class="drift-card-count">${esc(labelA)} vs ${esc(labelB)}</span>
        </div>
        ${ssidRows||'<div class="drift-item" style="color:var(--text-muted);cursor:default">No SSIDs found</div>'}
      </div>
    </div>`;
}

// ── Init ───────────────────────────────────────────────────────────────────────
loadNetworks();
</script>
</body>
</html>
"""


# ── Startup ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    threading.Thread(target=_preload_networks, daemon=True).start()
    port = int(os.environ.get("PORT", 8083))
    print(f"Config Drift tool running at http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
