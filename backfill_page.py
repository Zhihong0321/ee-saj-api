"""The /backfill control page. Kept as a string so there is no static-file
path to get wrong on Railway."""

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SAJ historical copy</title>
<style>
  :root{--bg:#0e1116;--card:#161b22;--line:#272e38;--fg:#e6edf3;--dim:#8b949e;
        --ok:#2ea043;--warn:#d29922;--bad:#f85149;--accent:#388bfd}
  @media (prefers-color-scheme: light){
    :root{--bg:#f6f8fa;--card:#fff;--line:#d0d7de;--fg:#1f2328;--dim:#656d76}}
  *{box-sizing:border-box}
  body{margin:0;padding:24px;background:var(--bg);color:var(--fg);
       font:15px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
  .wrap{max-width:860px;margin:0 auto}
  h1{font-size:20px;margin:0 0 4px}
  .sub{color:var(--dim);font-size:13px;margin-bottom:20px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:10px;
        padding:16px;margin-bottom:16px}
  .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  input{flex:1;min-width:200px;padding:9px 11px;border-radius:7px;
        border:1px solid var(--line);background:var(--bg);color:var(--fg);font-size:14px}
  button{padding:9px 20px;border-radius:7px;border:0;font-size:14px;font-weight:600;
         cursor:pointer;color:#fff}
  button:disabled{opacity:.4;cursor:not-allowed}
  #start{background:var(--ok)} #stop{background:var(--bad)}
  .state{display:inline-block;padding:3px 10px;border-radius:20px;font-size:12px;
         font-weight:700;text-transform:uppercase;letter-spacing:.4px}
  .s-running{background:rgba(46,160,67,.18);color:var(--ok)}
  .s-syncing{background:rgba(210,153,34,.18);color:var(--warn)}
  .s-stopped,.s-idle{background:rgba(139,148,158,.18);color:var(--dim)}
  .s-stopping{background:rgba(210,153,34,.18);color:var(--warn)}
  .s-done{background:rgba(56,139,253,.18);color:var(--accent)}
  .s-unavailable,.s-failed{background:rgba(248,81,73,.18);color:var(--bad)}
  .bar{height:9px;background:var(--line);border-radius:6px;overflow:hidden;margin:14px 0 6px}
  .bar>i{display:block;height:100%;background:var(--ok);width:0;transition:width .4s}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:14px;
        margin-top:14px}
  .k{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.5px}
  .v{font-size:19px;font-weight:650;font-variant-numeric:tabular-nums}
  table{width:100%;border-collapse:collapse;font-size:13px}
  td{padding:5px 0;border-bottom:1px solid var(--line)}
  td:last-child{text-align:right;color:var(--dim);font-variant-numeric:tabular-nums}
  .mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px}
  .err{color:var(--bad);font-size:13px;margin-top:8px}
  .note{color:var(--dim);font-size:12px;margin-top:10px}
  h2{font-size:13px;text-transform:uppercase;letter-spacing:.5px;color:var(--dim);
     margin:0 0 10px}
</style>
</head>
<body>
<div class="wrap">
  <h1>SAJ catalog sync &amp; historical copy</h1>
  <div class="sub">Synchronizes plants, devices, and customer links before backfilling
    the 5-min feed into <span class="mono">saj_reading</span>.
    Stop any time &mdash; Start resumes where it left off.
    <span id="policy"></span></div>

  <div class="card">
    <div class="row">
      <input id="token" type="password" placeholder="trigger token" autocomplete="off">
      <button id="start">Start</button>
      <button id="redo">Re-pull all</button>
      <button id="stop">Stop</button>
    </div>
    <div id="msg" class="err"></div>
    <div class="note">Progress is saved after every device-day, so a redeploy or crash
      loses at most one day of work.
      <b>Start</b> skips days already stored; <b>Re-pull all</b> fetches every day in
      the window again and overwrites what is there &mdash; use it to repair history
      that was stored wrong.</div>
  </div>

  <div class="card">
    <div class="row" style="justify-content:space-between">
      <span class="state s-idle" id="state">idle</span>
      <span class="mono" id="window" style="color:var(--dim)"></span>
    </div>
    <div class="bar"><i id="fill"></i></div>
    <div class="mono" style="color:var(--dim);font-size:12px" id="pct">0%</div>
    <div class="grid">
      <div><div class="k">Devices</div><div class="v" id="dev">-</div></div>
      <div><div class="k">Device-days</div><div class="v" id="dd">-</div></div>
      <div><div class="k">Rows copied</div><div class="v" id="rows">-</div></div>
      <div><div class="k">Workers</div><div class="v" id="wk">-</div></div>
      <div><div class="k">Elapsed</div><div class="v" id="el">-</div></div>
      <div><div class="k">ETA</div><div class="v" id="eta">-</div></div>
    </div>
    <div class="note" id="jobmsg"></div>
  </div>

  <div class="card">
    <h2>Plant &amp; customer synchronization</h2>
    <div class="grid">
      <div><div class="k">Sync state</div><div class="v" id="syncstate">-</div></div>
      <div><div class="k">Plants / devices</div><div class="v" id="synccatalog">-</div></div>
      <div><div class="k">New links / maps</div><div class="v" id="synclinks">-</div></div>
      <div><div class="k">Needs review</div><div class="v" id="syncreview">-</div></div>
    </div>
    <div class="err" id="syncerror"></div>
  </div>

  <div class="card" id="wcard" style="display:none">
    <h2>Workers</h2><table id="wtab"></table>
  </div>
  <div class="card" id="ecard" style="display:none">
    <h2>Recent errors</h2><table id="etab"></table>
  </div>
</div>
<script>
const $ = id => document.getElementById(id);
const tok = $('token');
tok.value = localStorage.getItem('saj_bf_token') || '';
tok.addEventListener('input', () => localStorage.setItem('saj_bf_token', tok.value));

const nf = n => (n ?? 0).toLocaleString();
function dur(s){
  if (s == null) return '-';
  const h = Math.floor(s/3600), m = Math.floor(s%3600/60);
  return h ? h+'h '+m+'m' : m+'m';
}

async function act(path){
  $('msg').textContent = '';
  try{
    const r = await fetch(path, {method:'POST',
      headers:{'X-Trigger-Token': tok.value}});
    const j = await r.json();
    if(!r.ok) $('msg').textContent = j.detail || ('HTTP '+r.status);
  }catch(e){ $('msg').textContent = String(e); }
  poll();
}
$('start').onclick = () => act('/backfill/start');
$('redo').onclick  = () => {
  if(confirm('Re-pull every day in the window from SAJ, overwriting what is stored?\\n'
             + 'This is one SAJ call per device-day and takes hours.'))
    act('/backfill/start?redo=true');
};
$('stop').onclick  = () => act('/backfill/stop');

async function poll(){
  let s;
  try{ s = await (await fetch('/backfill/status')).json(); }
  catch(e){ return; }
  const st = s.state || 'idle';
  const el = $('state'); el.textContent = st; el.className = 'state s'+'-'+st;
  $('window').textContent = s.window_start ? s.window_start+' \\u2192 '+s.window_end
                                           + '  ('+s.span_days+'d)'
                                           + (s.redo ? '  \\u2014 re-pulling every day' : '')
                                         : '';
  $('policy').textContent = s.policy_months
    ? 'Policy: last '+s.policy_months+' months only \\u2014 nothing before '
      +s.policy_floor+' is captured.' : '';
  $('fill').style.width = (s.pct||0)+'%';
  $('pct').textContent = (s.pct||0)+'%';
  $('dev').textContent = nf(s.devices_done)+' / '+nf(s.devices);
  $('dd').textContent  = nf(s.device_days_done)+' / '+nf(s.device_days_total);
  $('rows').textContent = nf(s.rows_written);
  $('wk').textContent = s.workers_alive+' / '+s.workers_configured;
  $('el').textContent = dur(s.elapsed_seconds);
  $('syncstate').textContent = s.sync_state || 'pending';
  $('synccatalog').textContent = nf(s.sync_plants)+' / '+nf(s.sync_devices);
  $('synclinks').textContent = nf(s.sync_plant_links)+' / '+nf(s.sync_device_maps);
  $('syncreview').textContent = nf((s.sync_unmatched||0) +
    (s.sync_ambiguous||0) + (s.sync_conflicts||0));
  $('syncerror').textContent = s.sync_error || '';

  let eta = '-';
  if (s.elapsed_seconds && s.device_days_done > 0 && st === 'running'){
    const rate = s.device_days_done / s.elapsed_seconds;
    const left = s.device_days_total - s.device_days_done;
    if (rate > 0) eta = dur(Math.round(left / rate));
  }
  $('eta').textContent = eta;
  $('jobmsg').textContent = s.message || '';
  $('start').disabled = (st === 'syncing' || st === 'running' || st === 'stopping');
  $('stop').disabled  = (st !== 'syncing' && st !== 'running' && st !== 'stopping');

  const wn = s.worker_now || {};
  const ks = Object.keys(wn).sort();
  $('wcard').style.display = ks.length ? '' : 'none';
  $('wtab').innerHTML = ks.map(k =>
    '<tr><td class="mono">'+k+'</td><td class="mono">'+wn[k]+'</td></tr>').join('');

  let errs = [];
  try{ errs = await (await fetch('/backfill/errors')).json(); }catch(e){}
  $('ecard').style.display = errs.length ? '' : 'none';
  $('etab').innerHTML = errs.map(e =>
    '<tr><td class="mono">'+e.device_sn+'</td><td>'+
    String(e.error||'').slice(0,90)+'</td></tr>').join('');
}
poll(); setInterval(poll, 3000);
</script>
</body>
</html>"""
