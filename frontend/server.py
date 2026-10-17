"""JK BMS web frontend — live status + history charts (one tiny service).

Serves both tabs of the web UI:
  /            portal with two tabs (Status | History)
  /live        the live status page (polls the collector's /api/state)
  /history     history charts: uPlot, adaptive buckets (~180 per range),
               Y-axes auto-zoomed to the data range, and a cursor crosshair
               synchronized across ALL charts (uPlot cursor.sync) with a
               tooltip listing every series value at cursor height.

Data comes straight from OpenSearch (one date_histogram per refresh, bucket
interval computed from the selected range). No dashboard engine, no extra
JVM component — OpenSearch is the only dependency.

Static assets are vendored (uPlot, MIT license) in web/.
Configuration: environment variables (see .env.example / README).
"""

import json
import os
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

OS = os.environ.get("OPENSEARCH_URL", "http://127.0.0.1:9200").rstrip("/")
INDEX = os.environ.get("INDEX", "jkbms-*")
TZ = os.environ.get("CHARTS_TZ", "UTC")
DEVICE = os.environ.get("DEVICE_NAME", "JK BMS")
BACKEND = os.environ.get("BACKEND_URL", "http://127.0.0.1:8099").rstrip("/")
PORT = int(os.environ.get("WEB_PORT", "8098"))
WEB = Path(__file__).parent / "web"

FIELDS = ["cells.v1", "cells.v2", "cells.v3", "cells.v4", "pack.current",
          "soc.remaining_ah", "soc.full_ah", "soc.soc", "pack.voltage",
          "temps.t1", "temps.t2", "temps.mosfet",
          "cells.delta_mv", "cells.worst_deviation_mv"]
AGGS = {f"a{i}": {"avg": {"field": f}} for i, f in enumerate(FIELDS)}

RANGES = {"1h": 3600e3, "6h": 6 * 3600e3, "24h": 24 * 3600e3,
          "7d": 7 * 24 * 3600e3, "30d": 30 * 24 * 3600e3,
          "90d": 90 * 24 * 3600e3, "180d": 180 * 24 * 3600e3}
NICE_MS = [60e3, 120e3, 300e3, 600e3, 900e3, 1800e3, 3600e3, 2 * 3600e3,
           3 * 3600e3, 4 * 3600e3, 6 * 3600e3, 12 * 3600e3, 24 * 3600e3,
           2 * 24 * 3600e3, 7 * 24 * 3600e3]


def nice_interval(span_ms: float) -> int:
    target = span_ms / 180.0
    for step in NICE_MS:
        if step >= target:
            return int(step)
    return int(NICE_MS[-1])


def interval_label(ms: int) -> str:
    if ms < 3600e3:
        n = round(ms / 60e3)
        return f"per {n} minute" + ("s" if n != 1 else "")
    if ms < 86400e3:
        n = round(ms / 3600e3)
        return f"per {n} hour" + ("s" if n != 1 else "")
    n = round(ms / 86400e3)
    return f"per {n} day" + ("s" if n != 1 else "")


def fetch_series(rng: str) -> dict:
    span = RANGES.get(rng, RANGES["24h"])
    now = time.time() * 1000
    lo, hi = int(now - span), int(now)
    iv_ms = nice_interval(span)
    iv = interval_label(iv_ms)
    step = {60e3: "1m", 120e3: "2m", 300e3: "5m", 600e3: "10m", 900e3: "15m",
            1800e3: "30m"}.get(float(iv_ms))
    if step is None:
        h = iv_ms / 3600e3
        step = f"{int(h)}h" if h == int(h) else f"{h}h"
    body = {"size": 0, "track_total_hits": False,
            "query": {"bool": {"filter": [{"range": {"@timestamp": {
                "gte": lo, "lte": hi, "format": "epoch_millis"}}}]}},
            "aggs": {"t": {"date_histogram": {
                "field": "@timestamp", "fixed_interval": step,
                "time_zone": TZ, "min_doc_count": 0,
                "extended_bounds": {"min": lo, "max": hi}},
                "aggs": AGGS}}}
    req = urllib.request.Request(
        f"{OS}/{INDEX}/_search", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        buckets = json.loads(resp.read())["aggregations"]["t"]["buckets"]
    t = [b["key"] for b in buckets]
    series = {f: [b[f"a{i}"]["value"] if b[f"a{i}"]["value"] is not None
                  else None for b in buckets]
              for i, f in enumerate(FIELDS)}
    return {"t": t, "series": series, "interval_label": iv,
            "range_ms": int(span)}


HISTORY_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>JK-BMS · %DEVICE% History</title>
<link rel="stylesheet" href="/web/uplot.min.css">
<script src="/web/uplot.iife.min.js"></script>
<style>
  :root { color-scheme: dark; }
  body { margin:0; background:#0d1117; color:#e6edf3;
         font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif; }
  header { display:flex; align-items:center; gap:14px; padding:14px 20px;
           background:#161b22; border-bottom:1px solid #30363d;
           position:sticky; top:0; z-index:5; }
  header h1 { font-size:17px; margin:0; }
  .live { background:#238636; color:#fff; border-radius:8px; font-size:11px;
          padding:2px 8px; font-weight:700; letter-spacing:.5px; }
  .ranges { margin-left:auto; display:flex; gap:6px; }
  .ranges button { background:#21262d; color:#c9d1d9; border:1px solid #30363d;
                   border-radius:6px; padding:4px 12px; cursor:pointer; font-weight:600; }
  .ranges button.on { background:#1f6feb; border-color:#1f6feb; color:#fff; }
  .grid { display:grid; grid-template-columns:1fr 1fr; gap:12px; padding:12px 20px 40px; }
  .card { background:#161b22; border:1px solid #30363d; border-radius:8px;
          padding:10px 12px 6px; position:relative; }
  .card.wide { grid-column:1 / -1; }
  .card h2 { font-size:13px; margin:0 0 6px; color:#c9d1d9; }
  .card h2 .iv { color:#8b949e; font-weight:400; margin-left:10px; font-size:12px; }
  .legend { display:flex; flex-wrap:wrap; gap:4px 14px; margin:2px 0 4px;
            font-size:12px; color:#8b949e; }
  .legend b { color:#e6edf3; font-weight:600; margin-left:4px; }
  .legend i { width:9px; height:9px; border-radius:2px; display:inline-block; }
  .uplot, .u-wrap { width:100%; }
  .tt { position:absolute; pointer-events:none; background:rgba(22,27,34,.96);
        border:1px solid #30363d; border-radius:6px; padding:6px 10px;
        font-size:12px; z-index:3; display:none; min-width:170px; }
  .tt .tm { color:#8b949e; margin-bottom:4px; font-weight:600; }
  .tt .row { display:flex; align-items:center; gap:6px; margin:1px 0; }
  .tt .row i { width:8px; height:8px; border-radius:2px; }
  .tt .row span { color:#c9d1d9; }
  .tt .row b { margin-left:auto; color:#fff; font-variant-numeric:tabular-nums; }
  .foot { text-align:center; color:#484f58; font-size:12px; padding-bottom:18px; }
</style></head><body>
<header><span class="live">LIVE</span><h1>JK-BMS · %DEVICE% — History</h1>
  <div class="ranges" id="ranges"></div></header>
<div class="grid" id="grid"></div>
<div class="foot">1-minute raw data in OpenSearch · buckets adapt to the selected range
 · crosshair synced across all charts · %TZ%</div>
<script>
"use strict";
const PANELS = [
 {id:"cells", wide:true, title:"Cell Voltages", unit:"V", digits:3,
  series:[["cells.v1","Cell 1","#ff7b45"],["cells.v2","Cell 2","#58a6ff"],
          ["cells.v3","Cell 3","#bc8cff"],["cells.v4","Cell 4","#ffd33d"]]},
 {id:"amps", title:"Amps in/out", unit:"A", digits:2, zero:true,
  series:[["pack.current","Current (+chg / −dsg)","#58a6ff"]]},
 {id:"capacity", title:"Battery Capacity", unit:"Ah", digits:1,
  series:[["soc.remaining_ah","Remaining","#3fb950"],
          ["soc.full_ah","Full charge","#bc8cff"]]},
 {id:"socv", title:"SOC % vs Pack Voltage (LFP)", unit:"%", digits:1, dual:"V",
  series:[["soc.soc","SOC","#3fb950","%",1],
          ["pack.voltage","Voltage","#ff7b45","V",3]]},
 {id:"temps", title:"Temperatures", unit:"°C", digits:1,
  series:[["temps.t1","Battery T1","#58a6ff"],["temps.t2","Battery T2","#ffd33d"],
          ["temps.mosfet","MOSFET","#f85149"]]},
 {id:"badcell", wide:true, title:"Bad-Cell Early Warning (Δ / worst deviation)",
  unit:"mV", digits:1,
  series:[["cells.delta_mv","Δ max−min","#ffd33d"],
          ["cells.worst_deviation_mv","Worst-cell deviation","#ff4d9d"]]},
];
const TZ = "%TZ%";
let data = null, charts = [], range = "24h", refreshing = true, hovering = false;

function fmtT(ms, span) {
  const o = {timeZone: TZ, hour:"2-digit", minute:"2-digit", hour12:false};
  if (span > 2*24*3600e3) { o.day="2-digit"; o.month="2-digit"; }
  return new Date(ms).toLocaleString("de-DE", o);
}

function build() {
  const grid = document.getElementById("grid");
  const rb = document.getElementById("ranges");
  for (const r of ["1h","6h","24h","7d","30d","90d","180d"]) {
    const b = document.createElement("button");
    b.textContent = r; b.onclick = () => { range = r; load(); };
    b.dataset.r = r; rb.appendChild(b);
  }
  for (const p of PANELS) {
    const card = document.createElement("div");
    card.className = "card" + (p.wide ? " wide" : "");
    card.innerHTML = `<h2>${p.title}<span class="iv" id="iv-${p.id}"></span></h2>
      <div class="legend" id="lg-${p.id}"></div>
      <div class="uplot" id="ch-${p.id}"></div><div class="tt" id="tt-${p.id}"></div>`;
    grid.appendChild(card);
  }
}

function legendHTML(p) {
  return p.series.map(s => `<span><i style="background:${s[2]}"></i>${s[1]}<b id="v-${p.id}-${s[0].replace('.','_')}">–</b></span>`).join("");
}

function unitOf(p, s) { return s[3] || p.unit; }
function digitsOf(p, s) { return s[4] != null ? s[4] : p.digits; }
function colsOf(p, d) {
  return [d.t].concat(p.series.map(s => d.series[s[0]]));
}

function makeCharts() {
  charts.forEach(c => c.destroy());
  charts = [];
  for (const p of PANELS) {
    document.getElementById("lg-" + p.id).innerHTML = legendHTML(p);
    const axes = [
      {stroke:"#8b949e", grid:{stroke:"#21262d"}, ticks:{stroke:"#30363d"},
       labelColor:"#8b949e", font:"11px sans-serif",
       values: (u, ticks) => ticks.map(v => fmtT(v, data.range_ms))}];
    if (p.dual) axes.push({scale:"v2", side:1, stroke:"#ff7b45",
      grid:{show:false}, ticks:{show:false}, labelColor:"#8b949e",
      font:"11px sans-serif"});
    const yRange = (u, min, max) => {
      const pad = (max - min) * 0.08 || Math.abs(max) * 0.05 || 0.1;
      return [min - pad, max + pad];
    };
    const scales = {x: {time: true}, y: {range: yRange}};
    if (p.dual) scales.v2 = {range: yRange};
    const opts = {
      width: 600, height: p.wide ? 260 : 200, padding:[6,10,0,0],
      legend: {show:false},
      series: [{}].concat(p.series.map(s => ({
        label: s[1], stroke: s[2], width: 2,
        points: {show: false}, spanGaps: false,
        scale: (p.dual && s[0] === "pack.voltage") ? "v2" : "y"}))),
      cursor: {show:true, x:true, y:false,
               drag:{x:true, y:false, setScale:false},
               sync: {key: "jkbms", setScale: false}},
      scales, axes,
      hooks: {draw: [u => { if (!p.zero) return;
        const y0 = u.valToPos(0, "y", true);
        if (y0 == null || y0 < 0 || y0 > u.bbox.height + u.bbox.top + 20) return;
        const ctx = u.ctx; ctx.save(); ctx.strokeStyle = "#8b949e";
        ctx.setLineDash([4,4]); ctx.lineWidth = 1;
        const {left, width} = u.bbox;
        ctx.beginPath(); ctx.moveTo(left, y0); ctx.lineTo(left+width, y0);
        ctx.stroke(); ctx.restore();}]},
    };
    const el = document.getElementById("ch-" + p.id);
    opts.width = el.clientWidth;
    const u = new uPlot(opts, colsOf(p, data), el);
    u.root.id = "u-" + p.id;
    charts.push({u, p, el, tt: document.getElementById("tt-" + p.id)});
    u.over.addEventListener("pointerenter", () => hovering = true);
    u.over.addEventListener("pointerleave", () => {
      hovering = false; p.tt_hide && p.tt_hide(); });
  }
  window.addEventListener("resize", () => {
    for (const c of charts) c.u.setSize({width: c.el.clientWidth});
  });
}

function attachTips() {
  for (const c of charts) {
    const {u, p, tt} = c;
    const hide = () => tt.style.display = "none";
    p.tt_hide = hide;
    u.over.addEventListener("pointermove", () => {
      const idx = u.cursor.idx;
      if (idx == null || !data) return hide();
      tt.innerHTML = `<div class="tm">${fmtT(data.t[idx], data.range_ms)}</div>` +
        p.series.map(s => {
          const v = data.series[s[0]][idx];
          return `<div class="row"><i style="background:${s[2]}"></i>
            <span>${s[1]}</span><b>${v == null ? "–" : v.toFixed(digitsOf(p, s)) + " " + unitOf(p, s)}</b></div>`;
        }).join("");
      tt.style.display = "block";
      const w = tt.offsetWidth, cw = u.root.clientWidth;
      let x = u.cursor.left + 14;
      if (x + w > cw - 6) x = u.cursor.left - w - 14;
      tt.style.left = Math.max(4, x) + "px";
      tt.style.top = Math.min(Math.max(4, u.cursor.top - 20),
                              u.root.clientHeight - tt.offsetHeight - 6) + "px";
    });
    u.over.addEventListener("pointerleave", hide);
  }
}

function paint() {
  for (const c of charts) {
    const {u, p} = c;
    const cols = colsOf(p, data);
    if (!u.data) u.setData(cols); else u.setData(cols);
    document.getElementById("iv-" + p.id).textContent = data.interval_label;
    for (const s of p.series) {
      const cell = document.getElementById("v-" + p.id + "-" + s[0].replace(".","_"));
      if (cell) {
        const arr = data.series[s[0]];
        let last = null; for (let i = arr.length-1; i >= 0; i--)
          if (arr[i] != null) { last = arr[i]; break; }
        cell.textContent = last == null ? "–" : last.toFixed(digitsOf(p, s)) + " " + unitOf(p, s);
      }
    }
  }
  for (const b of document.querySelectorAll("#ranges button"))
    b.classList.toggle("on", b.dataset.r === range);
}

async function load() {
  try {
    const r = await fetch("/api/series?range=" + range);
    data = await r.json();
    if (!charts.length) { makeCharts(); attachTips(); }
    paint();
  } catch (e) { console.error(e); }
}

build();
load();
setInterval(() => { if (!hovering && refreshing) load(); }, 60000);
</script></body></html>"""




STATUS_HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>%DEVICE% &middot; JK BMS</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--border:#2d333b;--text:#e6edf3;--dim:#8b949e;
--green:#3fb950;--red:#f85149;--blue:#58a6ff;--yellow:#d29922;--orange:#ff7b45}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font:14px/1.45 -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;padding:18px}
h1{font-size:20px;font-weight:600} h1 small{color:var(--dim);font-weight:400;font-size:13px;margin-left:8px}
.sub{color:var(--dim);font-size:12px;margin:2px 0 14px}
.pill{display:inline-block;padding:2px 10px;border-radius:999px;font-size:12px;font-weight:600}
.pill.live{background:#1a3a1f;color:var(--green)} .pill.stale{background:#3a2a1a;color:var(--orange)}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));margin-bottom:12px}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px}
.card h2{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);margin-bottom:10px}
.hero{display:flex;gap:26px;flex-wrap:wrap;align-items:baseline}
.hero .big{font-size:52px;font-weight:700;line-height:1}
.hero .val{font-size:26px;font-weight:600}
.lbl{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.05em}
.kv{display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid #21262d;font-size:13px}
.kv:last-child{border-bottom:none}
.kv .k{color:var(--dim)} .kv .v{font-variant-numeric:tabular-nums}
.pos{color:var(--green)} .neg{color:var(--red)} .warn{color:var(--yellow)}
.cells{display:flex;gap:8px;flex-wrap:wrap}
.cell{background:#0d1117;border:1px solid var(--border);border-radius:8px;padding:8px 10px;min-width:96px;text-align:center}
.cell.min{border-color:var(--blue)} .cell.max{border-color:var(--orange)}
.cell .n{color:var(--dim);font-size:11px}
.cell .v{font-size:17px;font-weight:600;font-variant-numeric:tabular-nums;margin:2px 0}
.cell .r{color:var(--dim);font-size:11px;font-variant-numeric:tabular-nums}
.flags span{display:inline-block;padding:3px 10px;border-radius:6px;background:#21262d;color:var(--dim);
font-size:12px;font-weight:600;margin:0 6px 6px 0}
.flags span.on{background:#1a3a1f;color:var(--green)} .flags span.onw{background:#3a2a1a;color:var(--yellow)}
table{width:100%;border-collapse:collapse;font-size:13px}
td{padding:3px 4px;border-bottom:1px solid #21262d}
td:first-child{color:var(--dim)} td:last-child{text-align:right;font-variant-numeric:tabular-nums}
a{color:var(--blue)}
footer{color:var(--dim);font-size:12px;margin-top:14px}
</style></head><body>
<h1><span id="devname">&hellip;</span><small>JK BMS &middot; <span id="devaddr"></span></small></h1>
<div class="sub"><span id="pill" class="pill stale">connecting&hellip;</span>
 &nbsp;last frame: <span id="lastframe">-</span>
 &nbsp;&middot;&nbsp; frames: <span id="frames">0</span>
 &nbsp;&middot;&nbsp; hw <span id="hw">-</span> / sw <span id="sw">-</span> / sn <span id="sn">-</span></div>

<div class="grid">
  <div class="card"><h2>State of charge</h2>
    <div class="hero"><div><span class="big" id="soc">-</span><span class="big" style="font-size:26px">%</span></div>
      <div><div class="lbl">remaining</div><div class="val" id="rem">- Ah</div></div>
      <div><div class="lbl">full</div><div class="val" id="full">- Ah</div></div>
      <div><div class="lbl">SOH</div><div class="val" id="soh">- %</div></div>
      <div><div class="lbl" id="eta_lbl">time</div><div class="val" id="eta">–</div></div></div></div>
  <div class="card"><h2>Pack</h2>
    <div class="hero"><div><span class="big" id="pv">-</span><span style="font-size:24px"> V</span></div>
      <div><span class="val" id="pa">-</span><span style="font-size:16px"> A</span></div>
      <div><span class="val" id="pw">-</span><span style="font-size:16px"> W</span></div></div>
    <div class="sub" id="dir" style="margin-top:8px"></div></div>
</div>

<div class="card" style="margin-bottom:12px"><h2>Cells <span id="cellmeta" style="text-transform:none;letter-spacing:0"></span></h2>
  <div class="cells" id="cells"></div>
  <div class="sub" style="margin-top:8px" id="cellsum"></div></div>

<div class="grid">
  <div class="card"><h2>Status flags</h2><div class="flags" id="flags"></div></div>
  <div class="card"><h2>Temperatures</h2>
    <div class="kv"><span class="k">Probe 1</span><span class="v" id="t1">-</span></div>
    <div class="kv"><span class="k">Probe 2</span><span class="v" id="t2">-</span></div>
    <div class="kv"><span class="k">MOSFET</span><span class="v" id="tmos">-</span></div>
    <div class="kv"><span class="k">Heating current</span><span class="v" id="heatcur">-</span></div></div>
  <div class="card"><h2>Counters</h2>
    <div class="kv"><span class="k">Cycles</span><span class="v" id="cycles">-</span></div>
    <div class="kv"><span class="k">Total cycle capacity</span><span class="v" id="cycap">-</span></div>
    <div class="kv"><span class="k">Runtime</span><span class="v" id="runtime">-</span></div>
    <div class="kv"><span class="k">Charge status</span><span class="v" id="cstat">-</span></div>
    <div class="kv"><span class="k">Errors bitmask</span><span class="v" id="errmask">-</span></div></div>
</div>

<div class="card"><h2>Configured limits (read from BMS settings frame)</h2>
  <div class="grid" style="margin:0"><table id="settings"></table></div></div>

<footer>Read-only BLE client &middot; protocol: syssi/esphome-jk-bms &middot;
<a href="/api/state">/api/state</a> &middot; data indexed to OpenSearch every minute</footer>

<script>
const $=id=>document.getElementById(id);
const fmt=(v,d=2)=>v==null?"-":Number(v).toFixed(d);
function pill(st){const live=st.streaming&&st.last_frame_age_s!=null&&st.last_frame_age_s<10;
  $("pill").className="pill "+(live?"live":"stale");
  $("pill").textContent=(live?"● LIVE":"○ "+(st.status||"stale"));}
async function tick(){
 try{
  const s=await (await fetch("/api/state")).json();
  pill(s.live);
  $("devname").textContent=s.device.name||"JK BMS";
  $("devaddr").textContent=s.device.address;
  $("hw").textContent=s.device.hardware||"-";
  $("sw").textContent=s.device.software||"-";
  $("sn").textContent=s.device.serial||"-";
  $("lastframe").textContent=(s.live.last_frame||"-").replace("T"," ").replace("+00:00"," UTC");
  $("frames").textContent=s.live.frames_total+" (cell "+s.live.frames_cell_info+")";
  if(s.soc.soc!=null){$("soc").textContent=s.soc.soc;
    $("rem").textContent=fmt(s.soc.remaining_ah,1)+" Ah";
    $("full").textContent=fmt(s.soc.full_ah,1)+" Ah";
    $("soh").textContent=s.soc.soh+" %";
    const fmtH=h=>h>=100?fmt(h,0)+" h":(Math.floor(h))+"h "+String(Math.round((h%1)*60)).padStart(2,"0")+"m";
    if(s.soc.eta_empty_h!=null){$("eta_lbl").textContent="until empty";
      $("eta").textContent="~"+fmtH(s.soc.eta_empty_h); $("eta").className="val neg";}
    else if(s.soc.eta_full_h!=null){$("eta_lbl").textContent="until full";
      $("eta").textContent="~"+fmtH(s.soc.eta_full_h); $("eta").className="val pos";}
    else{$("eta_lbl").textContent="time"; $("eta").textContent="idle"; $("eta").className="val";}}
  if(s.pack.voltage!=null){$("pv").textContent=fmt(s.pack.voltage,3);
    const a=s.pack.current;$("pa").textContent=(a>0?"+":"")+fmt(a,2);
    $("pa").className="val "+(a>0.05?"pos":a<-0.05?"neg":"");
    $("pw").textContent=(s.pack.power>0?"+":"")+fmt(s.pack.power,0);
    $("pw").className="val "+(s.pack.power>0.5?"pos":s.pack.power<-0.5?"neg":"");
    const discharging=a<-0.2, charging=a>0.2;
    $("dir").textContent=charging?"⚡ charging":discharging?"🔋 discharging":"idle";}
  if(s.cells.voltages){
    const c=s.cells,n=c.count_active||0;
    $("cellmeta").textContent=`(${n} active / ${c.count_config} config)`;
    let html="";
    for(let i=0;i<c.count_config;i++){
      const v=c.voltages[i]; if(!(v>0.5)) continue;
      const cls=i+1===c.min_cell?"min":i+1===c.max_cell?"max":"";
      html+=`<div class="cell ${cls}"><div class="n">Cell ${i+1}</div><div class="v">${v.toFixed(3)} V</div>
             <div class="r">${fmt(c.resistances[i],3)} &Omega;</div></div>`;}
    $("cells").innerHTML=html;
    $("cellsum").textContent=`min ${fmt(c.min_voltage,3)} V (cell ${c.min_cell}) · max ${fmt(c.max_voltage,3)} V (cell ${c.max_cell}) · avg ${fmt(c.avg_voltage,3)} V`;
    const dv=c.delta_voltage, dcls=dv==null?"":dv>=0.05?"neg":dv>=0.02?"warn":"pos";
    $("cellsum").innerHTML+=` · Δ <span class="${dcls}" style="font-weight:700">${fmt(dv,3)} V</span>`;}
  const f=s.status||{},fl=[],cur=s.pack.current;
  const chgAct=cur>0.2,dsgAct=cur<-0.2;  // raw BMS flags unreliable -> derive from current
  fl.push(["charging",chgAct,"on"],["discharging",dsgAct,"on"],
          ["precharging",f.precharging,"on"],["balancing",f.balancing,"onw"],
          ["heating",s.temps.heating,"onw"]);
  $("flags").innerHTML=fl.map(([n,on,c])=>`<span class="${on?c:""}">${n}${on?" ✓":""}</span>`).join("")
    +`<div class="sub" style="margin-top:6px">balancing current: ${fmt(f.balancing_current,3)} A</div>`;
  $("t1").textContent=fmt(s.temps.t1,1)+" °C";
  $("t2").textContent=fmt(s.temps.t2,1)+" °C";
  $("tmos").textContent=fmt(s.temps.mosfet,1)+" °C";
  $("heatcur").textContent=fmt(s.temps.heating_current,3)+" A";
  $("cycles").textContent=s.soc.cycles??"-";
  $("cycap").textContent=fmt(s.soc.total_cycle_capacity_ah,1)+" Ah";
  $("runtime").textContent=fmt((s.soc.runtime_seconds||0)/86400,1)+" d";
  $("cstat").textContent=s.pack.current>0.2?"Charging":s.pack.current<-0.2?"Discharging":"Idle";
  $("errmask").textContent=f.errors_mask?("0x"+f.errors_mask.toString(16)):"none";
  const st=s.settings||{};
  if(st.cell_count!=null){
    const rows=[["Cell count",st.cell_count],["Total capacity",fmt(st.total_capacity,1)+" Ah"],
      ["Max charge current",fmt(st.max_charge_current,2)+" A"],["Max discharge current",fmt(st.max_discharge_current,2)+" A"],
      ["OV protection",fmt(st.ov_protection,3)+" V"],["OV recovery",fmt(st.ov_recovery,3)+" V"],
      ["UV protection",fmt(st.uv_protection,3)+" V"],["UV recovery",fmt(st.uv_recovery,3)+" V"],
      ["Balance trigger",fmt(st.balance_trigger_voltage,3)+" V"],["Balance start",fmt(st.balance_start_voltage,3)+" V"],
      ["Max balance current",fmt(st.max_balance_current,3)+" A"],
      ["Charge OC delay / recovery",st.charge_oc_delay+" s / "+st.charge_oc_recovery_time+" s"],
      ["Discharge OC delay / recovery",st.discharge_oc_delay+" s / "+st.discharge_oc_recovery_time+" s"],
      ["Charge OT prot / recovery",fmt(st.charge_overtemp_protection,1)+" °C / "+fmt(st.charge_overtemp_recovery,1)+" °C"],
      ["Discharge OT prot / recovery",fmt(st.discharge_overtemp_protection,1)+" °C / "+fmt(st.discharge_overtemp_recovery,1)+" °C"],
      ["Charge UT prot / recovery",fmt(st.charge_undertemp_protection,1)+" °C / "+fmt(st.charge_undertemp_recovery,1)+" °C"],
      ["Discharge UT prot / recovery",fmt(st.discharge_undertemp_protection,1)+" °C / "+fmt(st.discharge_undertemp_recovery,1)+" °C"],
      ["MOSFET OT prot / recovery",fmt(st.mosfet_overtemp_protection,1)+" °C / "+fmt(st.mosfet_overtemp_recovery,1)+" °C"],
      ["SC delay",st.short_circuit_delay+" µs"],["SC recovery",st.short_circuit_recovery_time+" s"],["Precharge time",st.precharge_time+" s"],
      ["Charge request V",fmt(st.charge_request_voltage,3)+" V"],["Float request V",fmt(st.float_request_voltage,3)+" V"],
      ["Power-off V",fmt(st.power_off_voltage,3)+" V"],["Smart sleep V / delay",fmt(st.smart_sleep_voltage,3)+" V / "+st.smart_sleep_delay+" s"],
      ["Heating start / stop",st.heating_start_temp+" °C / "+st.heating_stop_temp+" °C"],
      ["Switch: charge / discharge / bal",flag(st.switch_charging)+" / "+flag(st.switch_discharging)+" / "+flag(st.switch_balancer)],
      ["Switch: heating / float / smart sleep",flag(st.switch_heating)+" / "+flag(st.switch_float_mode)+" / "+flag(st.switch_smart_sleep)]];
    $("settings").innerHTML=rows.map(([k,v])=>`<tr><td>${k}</td><td>${v}</td></tr>`).join("");}}
 catch(e){$("pill").className="pill stale";$("pill").textContent="daemon unreachable";}
}
const flag=b=>b?"<span style='color:var(--green)'>on</span>":"<span style='color:var(--dim)'>off</span>";
tick();setInterval(tick,2000);
</script>
</body></html>"""



def _tpl(html: str) -> str:
    """Substitute device name / timezone into a page template."""
    return html.replace("%DEVICE%", DEVICE).replace("%TZ%", TZ)

PORTAL_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>JK-BMS · %DEVICE%</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; background:#0d1117; color:#e6edf3;
         font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;
         height:100vh; display:flex; flex-direction:column; }
  header { display:flex; align-items:center; gap:14px; padding:10px 18px;
           background:#161b22; border-bottom:1px solid #30363d; flex:0 0 auto; }
  header h1 { font-size:16px; margin:0; }
  .live { background:#238636; color:#fff; border-radius:8px; font-size:11px;
          padding:2px 8px; font-weight:700; letter-spacing:.5px; }
  nav { margin-left:18px; display:flex; gap:8px; }
  nav button { background:transparent; color:#8b949e; border:1px solid transparent;
               border-radius:6px; padding:6px 16px; cursor:pointer;
               font-size:14px; font-weight:600; }
  nav button.on { background:#1f6feb; border-color:#1f6feb; color:#fff; }
  nav button:not(.on):hover { color:#e6edf3; background:#21262d; }
  iframe { flex:1 1 auto; width:100%; border:0; background:#0d1117;
           display:none; min-height:0; }
  iframe.on { display:block; }
  body { overflow:hidden; }
</style></head><body>
<header><span class="live">LIVE</span><h1>JK-BMS · %DEVICE%</h1>
  <nav>
    <button id="tab-live" class="on">Status (realtime)</button>
    <button id="tab-hist">History</button>
  </nav></header>
<iframe id="f-live" class="on" src="/live"></iframe>
<iframe id="f-hist" src="about:blank"></iframe>
<script>
const tabs = {live: ["tab-live", "f-live", "/live"],
              hist: ["tab-hist", "f-hist", "/history"]};
function show(which) {
  for (const k of Object.keys(tabs)) {
    const [tid, fid, src] = tabs[k];
    document.getElementById(tid).classList.toggle("on", k === which);
    const f = document.getElementById(fid);
    f.classList.toggle("on", k === which);
    if (k === which && f.getAttribute("src") === "about:blank")
      f.setAttribute("src", src);
  }
  history.replaceState(null, "", "#" + which);
}
document.getElementById("tab-live").onclick = () => show("live");
document.getElementById("tab-hist").onclick = () => show("hist");
show(location.hash === "#history" || location.hash === "#hist" ? "hist" : "live");
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    # neutral banner: never advertise framework/Python versions publicly
    server_version = "JK-BMS"
    sys_version = ""

    def log_message(self, *_):
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self, path_query: str) -> None:
        try:
            req = urllib.request.Request(BACKEND + path_query)
            with urllib.request.urlopen(req, timeout=10) as r:
                body = r.read()
                ctype = r.headers.get("Content-Type", "application/json")
                self._send(r.status, body, ctype)
        except Exception as exc:
            self._send(502, json.dumps({"error": str(exc)}).encode(),
                       "application/json")

    def do_GET(self) -> None:
        u = urlparse(self.path)
        if u.path == "/":
            self._send(200, _tpl(PORTAL_PAGE).encode(), "text/html; charset=utf-8")
        elif u.path == "/history":
            self._send(200, _tpl(HISTORY_PAGE).encode(), "text/html; charset=utf-8")
        elif u.path == "/live":
            self._send(200, _tpl(STATUS_HTML).encode(), "text/html; charset=utf-8")
        elif u.path in ("/api/state", "/api/health"):
            # the live page polls the collector through this proxy (same origin)
            self._proxy(u.path + (("?" + u.query) if u.query else ""))
        elif u.path.startswith("/web/"):
            # containment: resolve and refuse anything outside web/ (service
            # is public -> raw "../" paths must never leak sources/home files)
            rel = u.path.removeprefix("/web/").split("?")[0]
            f = (WEB / rel).resolve()
            if f.is_file() and f.is_relative_to(WEB.resolve()):
                ctype = ("text/javascript" if f.suffix == ".js"
                         else "text/css" if f.suffix == ".css"
                         else "text/html; charset=utf-8" if f.suffix == ".html"
                         else "application/octet-stream")
                self._send(200, f.read_bytes(), ctype)
            else:
                self._send(404, b"not found", "text/plain")
        elif u.path == "/api/series":
            rng = parse_qs(u.query).get("range", ["24h"])[0]
            try:
                self._send(200, json.dumps(fetch_series(rng)).encode(),
                           "application/json")
            except Exception as exc:
                self._send(502, json.dumps({"error": str(exc)}).encode(),
                           "application/json")
        elif u.path == "/api/health":
            self._send(200, b'{"status":"ok"}', "application/json")
        else:
            self._send(404, b"not found", "text/plain")


if __name__ == "__main__":
    print(f"jk-bms web frontend on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
