"""A small FastAPI dashboard (blueprint F): Atlas heatmap, islands, the Pareto front,
lineage, the cascade funnel and the hack-alert queue, for a (possibly live) run.

It reads the program store and the telemetry JSONL of a run and serves:

* ``/``            - single-page live view (polls the JSON endpoints)
* ``/api/report``  - the full report (colloid.services.report)
* ``/api/events``  - recent telemetry events (tail)
* ``/api/atlas``   - Atlas nodes with opportunity / leverage for the heatmap
* ``/healthz``

No build step; the page is a single self-contained HTML string with vanilla JS polling.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _events_path(run: str) -> Path:
    if Path(run).suffix == ".jsonl":
        return Path(run)
    return Path("runs") / run / "events.jsonl"


def build_app(run: str) -> Any:
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse

    from colloid.adapters.store.sql_store import open_store
    from colloid.adapters.telemetry.jsonl import read_events
    from colloid.services.report import _store_url, build_report

    app = FastAPI(title=f"Colloid — {run}")

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/report")
    def report() -> JSONResponse:
        return JSONResponse(build_report(run))

    @app.get("/api/events")
    def events(limit: int = 300) -> JSONResponse:
        evs = read_events(_events_path(run))
        return JSONResponse(evs[-limit:])

    @app.get("/api/atlas")
    def atlas() -> JSONResponse:
        store = open_store(_store_url(run))
        try:
            a = store.get_atlas()
            if a is None:
                return JSONResponse({"nodes": []})
            nodes = []
            for u in a.units.values():
                if u.kind.value in ("function", "knob", "query"):
                    nodes.append({"name": u.name, "layer": u.layer.value, "kind": u.kind.value,
                                  "hotness": a.tag(u.id, "hotness", 0.0), "leverage": a.tag(u.id, "causal_leverage"),
                                  "latency_share": a.tag(u.id, "latency_share", 0.0)})
            return JSONResponse({"nodes": nodes})
        finally:
            store.close()

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return PAGE.replace("__RUN__", run)

    return app


def serve(run: str, host: str = "0.0.0.0", port: int = 8080) -> None:
    import uvicorn

    uvicorn.run(build_app(run), host=host, port=port, log_level="warning")


PAGE = """<!doctype html><html><head><meta charset=utf-8><title>Colloid — __RUN__</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0f1117;--panel:#171a23;--line:#262b38;--ink:#e6e9ef;--mut:#8b93a7;--ok:#4ea1ff;--good:#49d18b;--warn:#ffb454;--bad:#ff6b6b}
@media(prefers-color-scheme:light){:root{--bg:#f6f7f9;--panel:#fff;--line:#e3e6ec;--ink:#1a1d24;--mut:#5b6476;--ok:#1a6fd6}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;gap:16px;align-items:baseline;flex-wrap:wrap}
h1{font-size:16px;margin:0;font-weight:650}.sub{color:var(--mut)}
main{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px;padding:16px;max-width:1500px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px}
.card h2{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--mut);margin:0 0 10px}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{text-align:left;padding:3px 6px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mut);font-weight:600}.good{color:var(--good)}.bad{color:var(--bad)}.warn{color:var(--warn)}.mut{color:var(--mut)}
.pill{display:inline-block;padding:1px 7px;border-radius:999px;background:var(--line);font-size:11px}
.bar{height:7px;border-radius:4px;background:linear-gradient(90deg,var(--ok),var(--good))}
code{font:12px ui-monospace,Menlo,monospace;color:var(--mut)}
.log{font:12px ui-monospace,monospace;max-height:340px;overflow:auto}.log div{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.big{font-size:26px;font-weight:680}
</style></head><body>
<header><h1>Colloid</h1><span class=sub id=run>__RUN__</span><span class=sub id=status>connecting…</span></header>
<main>
 <div class=card><h2>Run</h2><div id=kpis></div></div>
 <div class=card><h2>Cascade funnel</h2><table id=funnel></table></div>
 <div class=card><h2>Best programs (Pareto)</h2><div id=best></div></div>
 <div class=card><h2>Islands</h2><table id=islands></table></div>
 <div class=card><h2>Atlas — leverage &amp; hotness</h2><table id=atlas></table></div>
 <div class=card><h2>Operator credit (bandit)</h2><table id=arms></table></div>
 <div class=card><h2>Epistasis</h2><table id=epi></table></div>
 <div class=card><h2>Alerts</h2><div id=alerts></div></div>
 <div class=card style=grid-column:1/-1><h2>Event stream</h2><div class=log id=log></div></div>
</main>
<script>
const $=id=>document.getElementById(id); const pct=x=>x==null?'—':(x>=0?'+':'')+x.toFixed(1)+'%';
async function j(u){const r=await fetch(u);if(!r.ok)throw 0;return r.json()}
function esc(s){return (''+s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
async function tick(){
 try{
  const rep=await j('/api/report'); $('status').textContent='live · '+new Date().toLocaleTimeString();
  const res=rep.result||{}, best=(res.best_fitness_pct||{});
  $('kpis').innerHTML=`<div class=big class=good>${best.cost!=null?pct(best.cost):'—'}</div>
   <div class=mut>cost vs baseline · best program</div>
   <table><tr><td>rate</td><td>${(rep.setup.rate_rps||0).toFixed(0)} rps</td></tr>
   <tr><td>evaluated</td><td>${rep.program_counts.evaluated||res.programs_evaluated||0}</td></tr>
   <tr><td>promoted</td><td>${(rep.promoted||[]).length}</td></tr>
   <tr><td>verified (held)</td><td>${(rep.verified||[]).length}</td></tr>
   <tr><td>A/A gate</td><td class=${rep.aa_test&&rep.aa_test.promotions_allowed?'good':'bad'}>${rep.aa_test?(rep.aa_test.promotions_allowed?'pass':'fail'):'—'}</td></tr>
   <tr><td>LLM cost</td><td>$${(rep.llm.cost_usd||0).toFixed(3)} (${rep.llm.calls} calls)</td></tr></table>`;
  let f='<tr><th>stage</th><th>pass</th><th>fail</th><th>susp</th><th>err</th></tr>';
  for(const [s,c] of Object.entries(rep.cascade_funnel)){f+=`<tr><td>${s}</td><td class=good>${c.pass||0}</td><td class=bad>${c.fail||0}</td><td class=warn>${c.suspicious||0}</td><td class=mut>${c.error||0}</td></tr>`}
  $('funnel').innerHTML=f;
  $('best').innerHTML=(rep.best_programs||[]).slice(0,8).map(p=>{
   const g=p.gains_pct||{}; const c=g.cost?g.cost.pct:null;
   return `<div style=margin-bottom:10px><span class=pill>${p.island}</span> <code>${p.id.slice(0,10)}</code> <span class=mut>gen ${p.generation} · ${p.operator} · ${p.status}</span>
    <div><b class=${c>0?'good':'bad'}>${pct(c)}</b> cost ${g.p50?'· '+pct(g.p50.pct)+' p50':''} ${g.mem?'· '+pct(g.mem.pct)+' mem':''}</div>
    <div class=mut>${(p.genes||[]).map(esc).join('<br>')}</div></div>`}).join('')||'<span class=mut>none yet</span>';
  let il='<tr><th>island</th><th>best</th><th>cells</th><th>cov</th><th>T</th><th>stag</th></tr>';
  const evs=await j('/api/events'); const last={}; for(const e of evs){if(e.kind==='islands')for(const i of e.islands)last[i.name]=i}
  for(const i of Object.values(last)){il+=`<tr><td>${i.name}</td><td>${i.best_score.toFixed(3)}</td><td>${i.cells}</td><td>${(i.coverage*100).toFixed(0)}%</td><td>${i.temperature.toFixed(3)}</td><td>${i.stagnation_events}</td></tr>`}
  $('islands').innerHTML=il;
  const at=await j('/api/atlas'); const ns=at.nodes.filter(n=>n.leverage!=null||n.latency_share>0).sort((a,b)=>(b.leverage||b.latency_share)-(a.leverage||a.latency_share)).slice(0,12);
  $('atlas').innerHTML='<tr><th>unit</th><th>layer</th><th>leverage</th><th>lat%</th></tr>'+ns.map(n=>`<tr><td>${esc(n.name).slice(0,34)}</td><td>${n.layer}</td><td>${n.leverage!=null?n.leverage.toFixed(2):'—'}</td><td>${(n.latency_share*100).toFixed(0)}%</td></tr>`).join('');
  const arms=(res.arms||[]).filter(a=>a.pulls>0).sort((a,b)=>b.posterior_mean-a.posterior_mean);
  $('arms').innerHTML='<tr><th>operator</th><th>model</th><th>pulls</th><th>reward</th></tr>'+arms.map(a=>`<tr><td>${a.operator}</td><td class=mut>${esc(a.model||'')} ${esc(a.template||'')}</td><td>${a.pulls}</td><td>${(a.posterior_mean*100).toFixed(1)}%</td></tr>`).join('');
  $('epi').innerHTML='<tr><th>a</th><th>b</th><th>ε</th><th></th></tr>'+(rep.epistasis||[]).map(e=>`<tr><td><code>${e.gene_a}</code></td><td><code>${e.gene_b}</code></td><td class=${e.epsilon>0?'good':'bad'}>${e.epsilon.toFixed(3)}</td><td>${e.kind}</td></tr>`).join('')||'<tr><td class=mut>none</td></tr>';
  $('alerts').innerHTML=(rep.alerts||[]).map(a=>`<div class=${a.severity==='critical'?'bad':'warn'}>[${a.kind}] ${esc(a.message)}</div>`).join('')||'<span class=mut>none</span>';
  $('log').innerHTML=evs.slice(-120).reverse().map(e=>`<div><span class=mut>${(e.kind||'').padEnd(18)}</span> ${esc(JSON.stringify(Object.fromEntries(Object.entries(e).slice(4,9))))}</div>`).join('');
 }catch(e){$('status').textContent='waiting for run…'}
}
tick(); setInterval(tick,3000);
</script></body></html>"""
