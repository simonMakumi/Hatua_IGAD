"""The officials' dashboard.

Built for a county disaster officer deciding where to send a water truck, not
for a climate scientist. Three deliberate choices:

**Blocked advisories are shown, not hidden.** A verification layer nobody can
inspect is just a claim. The blocked tab is the evidence that the guardrail
does something, and it is the first thing worth showing a sceptic.

**Every score is clickable through to its derivation.** The /explain endpoint
returns the full arithmetic — weights, multipliers, thresholds. An analyst who
disagrees with a warning needs something specific to disagree with.

**Data gaps are rendered as gaps.** Eritrea and Djibouti appear with "no data"
rather than an invented score. Fabricated coverage in a life-safety system is
worse than absent coverage, because it is trusted.

Single self-contained file, MapLibre from CDN (open source, no Mapbox token),
no build step.
"""

from __future__ import annotations


def render_dashboard() -> str:
    return _HTML


_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HATUA — Early Warning Operations</title>
<link href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css" rel="stylesheet">
<script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
<style>
  :root{
    --bg:#0d1117; --panel:#161b22; --line:#26303d; --ink:#e6edf3;
    --muted:#8b949e; --accent:#2f81f7;
    --s1:#3fb950; --s2:#d29922; --s3:#f85149; --s4:#a371f7;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:14px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
  header{padding:14px 20px;border-bottom:1px solid var(--line);
         display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}
  h1{font-size:17px;margin:0;letter-spacing:.02em}
  .tag{color:var(--muted);font-size:12px}
  .status{margin-left:auto;font-size:12px;color:var(--muted)}
  .dot{display:inline-block;width:7px;height:7px;border-radius:50%;
       background:var(--s1);margin-right:5px}
  .wrap{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(0,1fr);
        gap:1px;background:var(--line);height:calc(100vh - 52px)}
  .col{background:var(--bg);overflow:auto;padding:16px}
  #map{height:340px;border-radius:6px;border:1px solid var(--line);margin-bottom:14px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:6px;
        padding:12px 14px;margin-bottom:12px}
  .card h2{font-size:12px;text-transform:uppercase;letter-spacing:.08em;
           color:var(--muted);margin:0 0 10px}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th{text-align:left;color:var(--muted);font-weight:500;font-size:11px;
     text-transform:uppercase;letter-spacing:.05em;padding:5px 6px;
     border-bottom:1px solid var(--line)}
  td{padding:6px;border-bottom:1px solid #1c242e;vertical-align:top}
  tr.clickable:hover{background:#1c2430;cursor:pointer}
  .bar{height:5px;border-radius:3px;background:#21262d;overflow:hidden;min-width:52px}
  .bar i{display:block;height:100%}
  .pill{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;
        border:1px solid transparent}
  .advisory{padding:9px 0;border-bottom:1px solid #1c242e}
  .advisory:last-child{border-bottom:0}
  .body{background:#0b0f14;border:1px solid var(--line);border-radius:5px;
        padding:9px 11px;margin:7px 0;font-size:13px;white-space:pre-wrap}
  .meta{font-size:11px;color:var(--muted);display:flex;gap:12px;flex-wrap:wrap}
  .check{font-size:11px;padding:1px 0;color:var(--muted)}
  .check b{color:var(--s3);font-weight:600}
  .pass{color:var(--s1)} .fail{color:var(--s3)}
  .tabs{display:flex;gap:5px;margin-bottom:10px}
  .tabs button{background:#0b0f14;border:1px solid var(--line);color:var(--muted);
    padding:5px 11px;border-radius:5px;cursor:pointer;font-size:12px}
  .tabs button.on{background:var(--accent);border-color:var(--accent);color:#fff}
  .kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:9px}
  .kpi{background:#0b0f14;border:1px solid var(--line);border-radius:5px;padding:9px}
  .kpi b{display:block;font-size:20px;font-weight:600}
  .kpi span{font-size:10px;color:var(--muted);text-transform:uppercase;
            letter-spacing:.05em}
  pre{background:#0b0f14;border:1px solid var(--line);border-radius:5px;
      padding:11px;overflow:auto;font-size:11.5px;margin:0;color:#c9d1d9}
  .gap{color:var(--s2);font-size:11px}
  code{background:#0b0f14;padding:1px 4px;border-radius:3px;font-size:11px}
  a{color:var(--accent)}
</style>
</head>
<body>
<header>
  <h1>HATUA</h1>
  <span class="tag">From warning to action — Greater Horn of Africa</span>
  <span class="status" id="status"><span class="dot"></span>loading…</span>
</header>

<div class="wrap">
  <div class="col">
    <div id="map"></div>
    <div class="card">
      <h2>Compound risk ranking</h2>
      <table id="ranking"><thead><tr>
        <th>District</th><th>Risk</th><th>Conf.</th><th>Hazard</th>
        <th>IPC</th><th>Triggers</th>
      </tr></thead><tbody></tbody></table>
    </div>
    <div class="card">
      <h2>Score derivation <span style="text-transform:none;letter-spacing:0">
        — click any district above</span></h2>
      <pre id="explain">Select a district to see how its score was computed.</pre>
    </div>
  </div>

  <div class="col">
    <div class="card">
      <h2>Warning to action</h2>
      <div class="kpis">
        <div class="kpi"><b id="k-districts">–</b><span>Districts</span></div>
        <div class="kpi"><b id="k-sent">–</b><span>Verified</span></div>
        <div class="kpi"><b id="k-blocked" style="color:var(--s3)">–</b>
          <span>Blocked</span></div>
        <div class="kpi"><b id="k-acted">–</b><span>Acted</span></div>
      </div>
    </div>

    <div class="card">
      <h2>Advisory queue</h2>
      <div class="tabs">
        <button class="on" data-f="all">All</button>
        <button data-f="passed">Verified</button>
        <button data-f="blocked">Blocked</button>
      </div>
      <div id="advisories"></div>
    </div>
  </div>
</div>

<script>
const SEV={advisory:'--s1',watch:'--s2',warning:'--s3',emergency:'--s4'};
let ADV=[], FILTER='all';

const map=new maplibregl.Map({
  container:'map',
  style:{version:8,sources:{osm:{type:'raster',
    tiles:['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],tileSize:256,
    attribution:'© OpenStreetMap'}},
    layers:[{id:'osm',type:'raster',source:'osm',
      paint:{'raster-opacity':0.42,'raster-saturation':-0.85}}]},
  center:[38,6], zoom:3.4
});

function riskColour(r){
  if(r>=0.55) return '#f85149';
  if(r>=0.40) return '#f0883e';
  if(r>=0.25) return '#d29922';
  return '#3fb950';
}

async function load(){
  const [health,districts,advisories,feedback]=await Promise.all([
    fetch('/health').then(r=>r.json()),
    fetch('/api/districts').then(r=>r.json()),
    fetch('/api/advisories').then(r=>r.json()),
    fetch('/api/feedback').then(r=>r.json())
  ]);
  ADV=advisories;

  document.getElementById('status').innerHTML =
    '<span class="dot" style="background:'+(health.running?'#d29922':'#3fb950')+
    '"></span>'+(health.running?'refreshing…':'live')+
    ' · '+(health.last_run? new Date(health.last_run).toUTCString().slice(5,22)+' UTC':'—')+
    ' · '+Object.keys(health.llm_providers_configured||{}).join(',')+
    ' · SMS '+health.sms_provider+(health.dry_run?' (dry run)':'');

  document.getElementById('k-districts').textContent=districts.length;
  document.getElementById('k-sent').textContent=
    advisories.filter(a=>a.dispatchable).length;
  document.getElementById('k-blocked').textContent=
    advisories.filter(a=>!a.dispatchable).length;
  document.getElementById('k-acted').textContent=feedback.acted||0;

  // --- ranking table ---
  const tb=document.querySelector('#ranking tbody'); tb.innerHTML='';
  districts.forEach(d=>{
    const gaps = d.vulnerability.ipc_phase==null;
    const tr=document.createElement('tr');
    tr.className='clickable';
    tr.onclick=()=>explain(d.pcode);
    tr.innerHTML=
      '<td><b>'+d.name+'</b><br><span class="tag">'+d.country+'</span></td>'+
      '<td><div class="bar"><i style="width:'+(d.compound_risk*100).toFixed(0)+
        '%;background:'+riskColour(d.compound_risk)+'"></i></div>'+
        '<span class="tag">'+d.compound_risk.toFixed(3)+'</span></td>'+
      '<td>'+(d.confidence*100).toFixed(0)+'%</td>'+
      '<td>'+(d.dominant_hazard? d.dominant_hazard.replace(/_/g,' '):'—')+'</td>'+
      '<td>'+(gaps? '<span class="gap">no data</span>' : d.vulnerability.ipc_phase)+'</td>'+
      '<td>'+(d.triggers.length? d.triggers.map(t=>
        '<span class="pill" style="border-color:var('+(SEV[t.max_severity]||'--muted')+
        ');color:var('+(SEV[t.max_severity]||'--muted')+')">'+
        t.name.replace(/_/g,' ')+'</span>').join(' ') : '<span class="tag">none</span>')+
      '</td>';
    tb.appendChild(tr);

    if(d.lat&&d.lon){
      const el=document.createElement('div');
      const size=14+d.compound_risk*22;
      el.style.cssText='width:'+size+'px;height:'+size+'px;border-radius:50%;'+
        'background:'+riskColour(d.compound_risk)+';opacity:.75;cursor:pointer;'+
        'border:2px solid rgba(255,255,255,.35)';
      new maplibregl.Marker({element:el}).setLngLat([d.lon,d.lat])
        .setPopup(new maplibregl.Popup({offset:12}).setHTML(
          '<div style="color:#111;font:13px system-ui"><b>'+d.name+'</b><br>'+
          'risk '+d.compound_risk.toFixed(3)+' · conf '+(d.confidence*100).toFixed(0)+'%<br>'+
          (d.dominant_hazard||'')+'</div>'))
        .addTo(map);
      el.addEventListener('click',()=>explain(d.pcode));
    }
  });

  renderAdvisories();
}

function renderAdvisories(){
  const box=document.getElementById('advisories');
  const list=ADV.filter(a=>FILTER==='all'||
    (FILTER==='passed'? a.dispatchable : !a.dispatchable));
  if(!list.length){ box.innerHTML='<span class="tag">Nothing here yet.</span>'; return; }
  box.innerHTML=list.map(a=>{
    const checks=(a.verification.checks||[]).map(c=>
      '<div class="check"><span class="'+(c.passed?'pass':'fail')+'">'+
      (c.passed?'PASS':'FAIL')+'</span> '+c.name.replace(/_/g,' ')+
      (c.passed?'':' — <b>'+esc(c.detail)+'</b>')+'</div>').join('');
    return '<div class="advisory">'+
      '<div><span class="pill" style="border-color:var('+(SEV[a.severity]||'--muted')+
        ');color:var('+(SEV[a.severity]||'--muted')+')">'+a.severity+'</span> '+
      '<b>'+a.district+'</b> <span class="tag">'+a.hazard.replace(/_/g,' ')+
      ' · '+a.language+' · '+a.channel+'</span></div>'+
      '<div class="body">'+esc(a.body)+'</div>'+
      '<div class="meta">'+
        '<span>'+a.characters+' chars</span>'+
        '<span>'+(a.encoding||'—')+'</span>'+
        '<span>'+(a.segments||'—')+' segment(s)</span>'+
        '<span>conf '+(a.confidence*100).toFixed(0)+'%</span>'+
        '<span>'+(a.action_ids||[]).join(', ')+'</span>'+
      '</div>'+ checks +'</div>';
  }).join('');
}

function esc(s){return (s||'').replace(/[<>&]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]));}

async function explain(pcode){
  const el=document.getElementById('explain');
  el.textContent='loading…';
  el.textContent=await fetch('/api/districts/'+pcode+'/explain').then(r=>r.text());
}

document.querySelectorAll('.tabs button').forEach(b=>{
  b.onclick=()=>{
    document.querySelectorAll('.tabs button').forEach(x=>x.classList.remove('on'));
    b.classList.add('on'); FILTER=b.dataset.f; renderAdvisories();
  };
});

load();
setInterval(load, 30000);
</script>
</body>
</html>"""
