"""The /fast control page — run a fast sync from the browser.

Kept as a string for the same reason as backfill_page: no static-file path to
get wrong on Railway.

The endpoint alone is only usable with curl and a token in a header, which is
not how this gets used in practice. This is the same feature with a box to type
a name into, the run's log rendered as it comes back, and — the part that
matters — clickable candidates when a name is ambiguous, since several customers
share a name outright and no amount of retyping separates them.
"""

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SAJ fast sync</title>
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
  input,select{padding:9px 11px;border-radius:7px;border:1px solid var(--line);
        background:var(--bg);color:var(--fg);font-size:14px}
  input#name{flex:1;min-width:220px}
  input#token{flex:1;min-width:180px}
  input#days{width:74px}
  button{padding:9px 20px;border-radius:7px;border:0;font-size:14px;font-weight:600;
         cursor:pointer;color:#fff;background:var(--ok)}
  button:disabled{opacity:.4;cursor:not-allowed}
  button.ghost{background:transparent;color:var(--accent);
               border:1px solid var(--line);font-weight:500}
  .state{display:inline-block;padding:3px 10px;border-radius:20px;font-size:12px;
         font-weight:700;text-transform:uppercase;letter-spacing:.4px}
  .s-idle{background:rgba(139,148,158,.18);color:var(--dim)}
  .s-running{background:rgba(210,153,34,.18);color:var(--warn)}
  .s-ok{background:rgba(46,160,67,.18);color:var(--ok)}
  .s-ambiguous{background:rgba(210,153,34,.18);color:var(--warn)}
  .s-error{background:rgba(248,81,73,.18);color:var(--bad)}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:14px;
        margin-top:14px}
  .k{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.5px}
  .v{font-size:19px;font-weight:650;font-variant-numeric:tabular-nums}
  table{width:100%;border-collapse:collapse;font-size:13px}
  td{padding:5px 0;border-bottom:1px solid var(--line);vertical-align:top}
  td:last-child{text-align:right;color:var(--dim);font-variant-numeric:tabular-nums}
  .mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px}
  .err{color:var(--bad);font-size:13px;margin-top:8px}
  .note{color:var(--dim);font-size:12px;margin-top:10px}
  h2{font-size:13px;text-transform:uppercase;letter-spacing:.5px;color:var(--dim);
     margin:0 0 10px}
  #log{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;
       line-height:1.65;white-space:pre-wrap;word-break:break-word;margin:0;
       max-height:420px;overflow:auto}
  #log .l-info{color:var(--fg)}
  #log .l-debug{color:var(--dim)}
  #log .l-warn{color:var(--warn)}
  #log .t{color:var(--dim)}
  .pill{display:inline-block;padding:2px 8px;border-radius:20px;font-size:11px;
        background:rgba(56,139,253,.15);color:var(--accent);margin-left:6px}
</style>
</head>
<body>
<div class="wrap">
  <h1>SAJ fast sync</h1>
  <div class="sub">Sync one named customer or plant into
    <span class="mono">saj_reading</span> &mdash; seconds, instead of the ~20 minute
    nightly sweep. Names are matched against our own mirror first, so nothing
    touches the SAJ portal until the readings pull.</div>

  <div class="card">
    <div class="row">
      <select id="kind">
        <option value="customer">Customer</option>
        <option value="plant">Plant</option>
      </select>
      <input id="name" placeholder="name, e.g. Chen Wei Fung" autocomplete="off">
      <input id="days" type="number" min="1" max="14" value="1" title="days back">
      <button id="run">Sync</button>
    </div>
    <div class="row" style="margin-top:10px">
      <input id="token" type="password" placeholder="trigger token" autocomplete="off">
      <label class="note" style="margin:0"><input type="checkbox" id="refresh"
        style="vertical-align:-2px"> re-read plant details from SAJ</label>
    </div>
    <div id="msg" class="err"></div>
    <div class="note">Always a full pull &mdash; no freshness gate. An unlinked plant
      whose name matches exactly one customer is linked on the way past.</div>
  </div>

  <div class="card">
    <div class="row" style="justify-content:space-between">
      <span class="state s-idle" id="state">idle</span>
      <span class="mono" id="matched" style="color:var(--dim)"></span>
    </div>
    <div class="grid">
      <div><div class="k">Plants</div><div class="v" id="plants">-</div></div>
      <div><div class="k">Devices</div><div class="v" id="devices">-</div></div>
      <div><div class="k">Rows</div><div class="v" id="rows">-</div></div>
      <div><div class="k">SAJ calls</div><div class="v" id="saj">-</div></div>
      <div><div class="k">Failed</div><div class="v" id="failed">-</div></div>
      <div><div class="k">Elapsed</div><div class="v" id="elapsed">-</div></div>
    </div>
  </div>

  <div class="card" id="choicecard" style="display:none">
    <h2>Which one?</h2>
    <div class="note" style="margin:0 0 10px">That name matches more than one record,
      so nothing was synced. Pick the one you meant.</div>
    <div class="row" id="choices"></div>
  </div>

  <div class="card" id="plantcard" style="display:none">
    <h2>Plants synced</h2><table id="planttab"></table>
  </div>

  <div class="card" id="errcard" style="display:none">
    <h2>Device errors</h2><table id="errtab"></table>
  </div>

  <div class="card" id="logcard" style="display:none">
    <h2>Run log</h2><pre id="log"></pre>
  </div>

  <div class="card">
    <h2>Recent runs on this instance</h2>
    <table id="recent"></table>
    <div class="note">In memory only &mdash; a redeploy clears it.</div>
  </div>
</div>
<script>
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const nf = n => (n ?? 0).toLocaleString();

// Same storage key as /backfill: one token, both pages.
const tok = $('token');
tok.value = localStorage.getItem('saj_bf_token') || '';
tok.addEventListener('input', () => localStorage.setItem('saj_bf_token', tok.value));
$('kind').value = localStorage.getItem('saj_fast_kind') || 'customer';
$('kind').addEventListener('change',
  () => localStorage.setItem('saj_fast_kind', $('kind').value));
$('name').addEventListener('keydown', e => { if (e.key === 'Enter') go(); });

function setState(s){
  const el = $('state'); el.textContent = s; el.className = 'state s-' + s;
}

function renderLog(lines){
  $('logcard').style.display = lines && lines.length ? '' : 'none';
  $('log').innerHTML = (lines || []).map(l => {
    const m = l.match(/^\\[\\s*([\\d.]+)s\\]\\s+(\\w+)\\s+(.*)$/s);
    if (!m) return '<span class="l-info">' + esc(l) + '</span>';
    return '<span class="t">' + m[1].padStart(6) + 's </span>' +
           '<span class="l-' + m[2] + '">' + esc(m[3]) + '</span>';
  }).join('\\n');
}

function renderChoices(choices){
  const box = $('choices');
  $('choicecard').style.display = choices && choices.length ? '' : 'none';
  box.innerHTML = '';
  (choices || []).forEach(c => {
    const b = document.createElement('button');
    b.className = 'ghost';
    b.textContent = c.label || c.name;
    if (c.customer_id) {
      const s = document.createElement('span');
      s.className = 'mono'; s.style.color = 'var(--dim)';
      s.textContent = '  ' + c.customer_id.slice(0, 12) + '…';
      b.appendChild(s);
    }
    // A shared name can only be separated by id, so send that when we have it.
    b.onclick = () => go(c.customer_id ? {customer_id: c.customer_id}
                                       : {[$('kind').value]: c.name || c.label});
    box.appendChild(b);
  });
}

// A failed run must not leave the previous run's numbers on screen — "ambiguous,
// nothing synced" sitting above "129 rows" reads as if something was synced.
function clearSummary(){
  ['plants','devices','rows','saj','failed','elapsed']
    .forEach(k => $(k).textContent = '-');
  $('matched').textContent = '';
  $('plantcard').style.display = 'none';
  $('errcard').style.display = 'none';
  renderLog([]);
}

function showSummary(j){
  $('plants').textContent  = nf(j.plant_count);
  $('devices').textContent = nf(j.device_count);
  $('rows').textContent    = nf(j.rows_written);
  $('saj').textContent     = nf(j.debug && j.debug.saj_calls);
  $('failed').textContent  = nf(j.err);
  $('elapsed').textContent = (j.elapsed_s ?? '-') + 's';
  $('matched').textContent = j.target
    ? j.target.kind + ' → ' + (j.target.matched ?? '') : '';

  const ps = j.plants || [];
  $('plantcard').style.display = ps.length ? '' : 'none';
  $('planttab').innerHTML = ps.map(p =>
    '<tr><td>' + esc(p.plant_name) + '<br><span class="mono" style="color:var(--dim)">'
    + esc(p.plant_uid) + '</span>' + (p.linked_now
        ? '<span class="pill">linked now</span>' : '') +
    '</td><td>' + nf(p.devices.length) + ' dev · ' + nf(p.rows_written) +
    ' rows</td></tr>').join('');

  const es = j.errors || [];
  $('errcard').style.display = es.length ? '' : 'none';
  $('errtab').innerHTML = es.map(e =>
    '<tr><td class="mono">' + esc(e.device_sn) + '</td><td>' +
    esc(String(e.error || '').slice(0, 120)) + '</td></tr>').join('');
}

async function go(override){
  const days = $('days').value || 1;
  let q;
  if (override && override.customer_id) {
    q = 'customer_id=' + encodeURIComponent(override.customer_id);
  } else {
    const kind = override ? Object.keys(override)[0] : $('kind').value;
    const name = override ? override[kind] : $('name').value.trim();
    if (!name) { $('msg').textContent = 'Type a name first.'; return; }
    $('name').value = name; $('kind').value = kind;
    q = kind + '=' + encodeURIComponent(name);
  }
  if ($('refresh').checked) q += '&refresh_catalog=true';

  $('msg').textContent = '';
  renderChoices([]);
  clearSummary();
  $('run').disabled = true;
  setState('running');
  try{
    const r = await fetch('/sync/fast?' + q + '&days=' + days,
      {method:'POST', headers:{'X-Trigger-Token': tok.value}});
    const j = await r.json();
    if (r.ok){
      setState('ok');
      showSummary(j);
      renderLog(j.log);
    } else {
      const d = (j && j.detail) || {};
      setState(d.error === 'ambiguous' ? 'ambiguous' : 'error');
      $('msg').textContent = typeof d === 'string' ? d
        : (d.detail || d.error || ('HTTP ' + r.status));
      renderChoices(d.choices || (d.candidates || []).map(c => ({label:c})));
      renderLog(d.log);
    }
  }catch(e){
    setState('error');
    $('msg').textContent = String(e);
  }
  $('run').disabled = false;
  recent();
}
$('run').onclick = () => go();

async function recent(){
  let d;
  try{ d = await (await fetch('/sync/fast/log?limit=8')).json(); }catch(e){ return; }
  $('recent').innerHTML = (d.runs || []).map(r =>
    '<tr><td>' + esc(r.target && r.target.matched || r.target && r.target.query) +
    '<br><span class="mono" style="color:var(--dim)">' + esc(r.at) + '</span></td>' +
    '<td>' + nf(r.rows_written) + ' rows · ' + nf(r.device_count) + ' dev · ' +
    (r.elapsed_s ?? '-') + 's' + (r.err ? ' · <span style="color:var(--bad)">' +
    r.err + ' failed</span>' : '') + '</td></tr>').join('')
    || '<tr><td class="note">Nothing yet.</td></tr>';
}
recent();
</script>
</body>
</html>"""
