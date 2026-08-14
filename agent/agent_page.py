# PROTOTYPE — throwaway. Hardcoded, no error handling. Do not ship.
"""The /agent chat page. Kept as a string so there is no static-file path to
get wrong on Railway — same as backfill_page.py."""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SAJ fleet chat</title>
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
  input,textarea{flex:1;min-width:200px;padding:9px 11px;border-radius:7px;
        border:1px solid var(--line);background:var(--bg);color:var(--fg);
        font-size:14px;font-family:inherit}
  textarea{resize:vertical;min-height:44px}
  button{padding:9px 20px;border-radius:7px;border:0;font-size:14px;font-weight:600;
         cursor:pointer;color:#fff;background:var(--accent)}
  button.ghost{background:transparent;color:var(--dim);border:1px solid var(--line)}
  button:disabled{opacity:.4;cursor:not-allowed}
  #log{min-height:120px}
  .msg{margin-bottom:16px}
  .who{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;
       color:var(--dim);margin-bottom:5px}
  .you .bubble{background:rgba(56,139,253,.10);border-left:3px solid var(--accent);
       padding:9px 12px;border-radius:0 7px 7px 0;white-space:pre-wrap}
  .bot .bubble>*:first-child{margin-top:0}
  .bot .bubble>*:last-child{margin-bottom:0}
  .bubble p{margin:0 0 10px}
  .bubble h3,.bubble h4,.bubble h5{margin:16px 0 6px;font-size:15px}
  .bubble ul{margin:0 0 10px;padding-left:20px}
  .bubble li{margin-bottom:3px}
  .bubble code{background:var(--bg);border:1px solid var(--line);border-radius:4px;
       padding:1px 5px;font-size:12.5px}
  .bubble table{border-collapse:collapse;margin:0 0 12px;font-size:13.5px;
       display:block;overflow-x:auto;max-width:100%}
  .bubble th,.bubble td{border:1px solid var(--line);padding:5px 10px;text-align:left;
       white-space:nowrap}
  .bubble th{background:var(--bg);font-weight:700}
  .meta{margin-top:8px;font-size:12px;color:var(--dim)}
  .meta button{background:transparent;color:var(--dim);border:0;padding:0;
       font-size:12px;font-weight:400;text-decoration:underline;cursor:pointer}
  pre.sql{background:var(--bg);border:1px solid var(--line);border-radius:7px;
       padding:10px;margin:8px 0 0;font-size:12px;overflow-x:auto;color:var(--dim);
       white-space:pre-wrap}
  .err{color:var(--bad)}
  .dots::after{content:'';animation:d 1.2s steps(4,end) infinite}
  @keyframes d{0%{content:''}25%{content:'.'}50%{content:'..'}75%{content:'...'}}
</style>
</head>
<body>
<div class="wrap">
  <h1>SAJ fleet chat</h1>
  <div class="sub">Ask about the inverter fleet. Read-only — the agent can
    only run SELECTs against the <code>saj_*</code> tables.</div>

  <div class="card">
    <div class="row">
      <input id="token" type="password" placeholder="trigger token" autocomplete="off">
      <button class="ghost" id="reset">New chat</button>
    </div>
  </div>

  <div class="card" id="log"></div>

  <div class="card">
    <div class="row">
      <textarea id="q" rows="2" placeholder="Which inverters produced nothing yesterday?"></textarea>
      <button id="send">Send</button>
    </div>
    <div class="meta" id="status">Enter sends &middot; Shift+Enter for a new line</div>
  </div>
</div>
<script>
const $ = id => document.getElementById(id);
const tok = $('token'), log = $('log'), q = $('q'), send = $('send'), status = $('status');
let session = null, spent = 0;

tok.value = localStorage.getItem('saj_bf_token') || '';
tok.addEventListener('input', () => localStorage.setItem('saj_bf_token', tok.value));

const esc = s => s.replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const inline = s => esc(s)
  .replace(/`([^`]+)`/g, '<code>$1</code>')
  .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
  .replace(/\*([^*<>]+)\*/g, '<em>$1</em>');

function md(text){
  const L = text.split('\n'), out = [];
  let i = 0;
  const isRow = s => /^\s*\|/.test(s), isLi = s => /^\s*[-*]\s+/.test(s),
        isH  = s => /^#{1,5}\s/.test(s);
  while (i < L.length){
    if (isRow(L[i])){
      const rows = [];
      while (i < L.length && isRow(L[i])) rows.push(L[i++]);
      const body = rows.filter(r => !/^\s*\|[\s:|-]+\|\s*$/.test(r));
      const cells = r => r.trim().replace(/^\|/,'').replace(/\|$/,'').split('|');
      out.push('<table>' + body.map((r, n) => {
        const t = n === 0 ? 'th' : 'td';
        return '<tr>' + cells(r).map(c => `<${t}>${inline(c.trim())}</${t}>`).join('') + '</tr>';
      }).join('') + '</table>');
      continue;
    }
    if (isLi(L[i])){
      const items = [];
      while (i < L.length && isLi(L[i])) items.push(L[i++].replace(/^\s*[-*]\s+/, ''));
      out.push('<ul>' + items.map(t => `<li>${inline(t)}</li>`).join('') + '</ul>');
      continue;
    }
    if (isH(L[i])){
      const m = L[i++].match(/^(#{1,5})\s+(.*)/);
      out.push(`<h${Math.min(m[1].length + 2, 6)}>${inline(m[2])}</h${Math.min(m[1].length + 2, 6)}>`);
      continue;
    }
    if (!L[i].trim()){ i++; continue; }
    const p = [];
    while (i < L.length && L[i].trim() && !isRow(L[i]) && !isLi(L[i]) && !isH(L[i])) p.push(L[i++]);
    out.push('<p>' + inline(p.join(' ')) + '</p>');
  }
  return out.join('');
}

function bubble(who, cls){
  const d = document.createElement('div');
  d.className = 'msg ' + cls;
  d.innerHTML = `<div class="who">${who}</div><div class="bubble"></div>`;
  log.appendChild(d);
  d.scrollIntoView({block:'end', behavior:'smooth'});
  return d.querySelector('.bubble');
}

async function ask(){
  const text = q.value.trim();
  if (!text) return;
  bubble('You', 'you').textContent = text;
  q.value = '';
  send.disabled = true;

  const b = bubble('Agent', 'bot');
  const t0 = Date.now();
  b.innerHTML = '<span class="dim dots">thinking</span>';
  const tick = setInterval(() => {
    status.textContent = `running — ${((Date.now()-t0)/1000).toFixed(0)}s`;
  }, 500);

  try {
    const url = '/agent/ask?q=' + encodeURIComponent(text)
              + (session ? '&session=' + encodeURIComponent(session) : '');
    const r = await fetch(url, {method:'POST', headers:{'X-Trigger-Token': tok.value}});
    const body = await r.json();
    if (!r.ok) throw new Error(body.detail || r.status);
    session = body.session_id;
    spent += body.cost_usd;
    const secs = ((Date.now()-t0)/1000).toFixed(0);
    b.innerHTML = md(body.answer);
    const m = document.createElement('div');
    m.className = 'meta';
    m.innerHTML = `${body.sql.length} quer${body.sql.length===1?'y':'ies'} &middot; ${secs}s
                   &middot; $${body.cost_usd.toFixed(4)}
                   ${body.sql.length ? '&middot; <button>show SQL</button>' : ''}`;
    if (body.sql.length){
      const pre = document.createElement('pre');
      pre.className = 'sql';
      pre.style.display = 'none';
      pre.textContent = body.sql.join('\n\n');
      m.querySelector('button').onclick = () => {
        const on = pre.style.display === 'none';
        pre.style.display = on ? 'block' : 'none';
        m.querySelector('button').textContent = on ? 'hide SQL' : 'show SQL';
      };
      m.appendChild(pre);
    }
    b.parentNode.appendChild(m);
  } catch (e) {
    b.innerHTML = `<span class="err">${esc(String(e.message || e))}</span>`;
  } finally {
    clearInterval(tick);
    send.disabled = false;
    status.textContent = `session total $${spent.toFixed(4)}`;
    q.focus();
  }
}

send.onclick = ask;
q.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); ask(); }
});
$('reset').onclick = () => { session = null; spent = 0; log.innerHTML = '';
                             status.textContent = 'new chat'; q.focus(); };
q.focus();
</script>
</body>
</html>
"""
