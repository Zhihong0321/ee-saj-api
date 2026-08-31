"""The /accounts control page — manage SAJ portal logins from the browser.

SAJ accounts live in the DB now, not in Railway env vars. This page lists them,
lets you add one, change a password when SAJ rotates it, flip which account is
primary (the one the app + nightly sweep log in as), toggle an account in/out of
the backfill pool, and test a login without leaving the page. Kept as a string
for the same reason as the other pages: no static-file path to get wrong on
Railway.
"""

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SAJ accounts</title>
<style>
  :root{--bg:#0e1116;--card:#161b22;--line:#272e38;--fg:#e6edf3;--dim:#8b949e;
        --ok:#2ea043;--warn:#d29922;--bad:#f85149;--accent:#388bfd}
  @media (prefers-color-scheme: light){
    :root{--bg:#f6f8fa;--card:#fff;--line:#d0d7de;--fg:#1f2328;--dim:#656d76}}
  *{box-sizing:border-box}
  body{margin:0;padding:24px;background:var(--bg);color:var(--fg);
       font:15px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
  .wrap{max-width:920px;margin:0 auto}
  h1{font-size:20px;margin:0 0 4px}
  .sub{color:var(--dim);font-size:13px;margin-bottom:20px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:10px;
        padding:16px;margin-bottom:16px}
  .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  label{font-size:12px;color:var(--dim);display:block;margin-bottom:4px}
  input{padding:9px 11px;border-radius:7px;border:1px solid var(--line);
        background:var(--bg);color:var(--fg);font-size:14px;width:100%}
  .f{flex:1;min-width:150px}
  .f-sm{width:120px;flex:0 0 auto}
  .checks{display:flex;gap:18px;align-items:center;margin:12px 0}
  .checks label{display:flex;gap:6px;align-items:center;margin:0;font-size:13px;
        color:var(--fg);cursor:pointer}
  .checks input{width:auto}
  button{padding:9px 20px;border-radius:7px;border:0;font-size:14px;font-weight:600;
         cursor:pointer;color:#fff;background:var(--ok)}
  button:disabled{opacity:.4;cursor:not-allowed}
  button.ghost{background:transparent;color:var(--accent);
               border:1px solid var(--line);font-weight:500;padding:6px 12px;font-size:13px}
  button.danger{background:transparent;color:var(--bad);
               border:1px solid var(--line);font-weight:500;padding:6px 12px;font-size:13px}
  h2{font-size:13px;text-transform:uppercase;letter-spacing:.5px;color:var(--dim);
     margin:0 0 12px}
  table{width:100%;border-collapse:collapse;font-size:14px}
  th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.5px;
     color:var(--dim);font-weight:600;padding:6px 8px;border-bottom:1px solid var(--line)}
  td{padding:9px 8px;border-bottom:1px solid var(--line);vertical-align:middle}
  .mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:13px}
  .badge{display:inline-block;padding:2px 9px;border-radius:20px;font-size:11px;
         font-weight:700;text-transform:uppercase;letter-spacing:.4px}
  .b-primary{background:rgba(56,139,253,.16);color:var(--accent)}
  .b-active{background:rgba(46,160,67,.16);color:var(--ok)}
  .b-off{background:rgba(139,148,158,.16);color:var(--dim)}
  .b-ok{background:rgba(46,160,67,.16);color:var(--ok)}
  .b-bad{background:rgba(248,81,73,.16);color:var(--bad)}
  .acts{display:flex;gap:6px;justify-content:flex-end;flex-wrap:wrap}
  td.r{text-align:right}
  .err{color:var(--bad);font-size:12px}
  .note{color:var(--dim);font-size:12px;margin-top:10px}
  .msg{font-size:13px;margin-top:10px;min-height:18px}
  .msg.ok{color:var(--ok)} .msg.bad{color:var(--bad)}
</style>
</head>
<body>
<div class="wrap">
  <h1>SAJ accounts</h1>
  <div class="sub">The portal logins the fetcher uses, stored in
    <span class="mono">saj_account</span> &mdash; not in Railway variables. The
    <b>primary</b> account is the one the app, the nightly sweep and fast sync log
    in as; every <b>active</b> account joins the history-backfill pool. Change a
    password here when SAJ rotates it &mdash; takes effect on the next call, no redeploy.</div>

  <div class="card">
    <h2>Add / update account</h2>
    <div class="row">
      <div class="f"><label>Username</label>
        <input id="u" placeholder="operation01" autocomplete="off"></div>
      <div class="f"><label>Password <span id="pwhint" class="dim"></span></label>
        <input id="p" type="password" placeholder="portal password" autocomplete="new-password"></div>
      <div class="f-sm"><label>Org code</label>
        <input id="org" placeholder="OAhz" autocomplete="off"></div>
    </div>
    <div class="row" style="margin-top:10px">
      <div class="f"><label>Remarks (optional)</label>
        <input id="rem" placeholder="e.g. rotated 2026-08-31" autocomplete="off"></div>
    </div>
    <div class="checks">
      <label><input type="checkbox" id="active" checked> Active (in backfill pool)</label>
      <label><input type="checkbox" id="primary"> Primary (app + nightly sweep)</label>
    </div>
    <div class="row">
      <button id="save">Save account</button>
      <button class="ghost" id="clear">Clear</button>
      <input id="token" class="f-sm" style="width:200px;flex:0 0 auto" type="password"
             placeholder="trigger token (if set)" autocomplete="off">
    </div>
    <div id="formmsg" class="msg"></div>
  </div>

  <div class="card">
    <h2>Accounts</h2>
    <table>
      <thead><tr>
        <th>Username</th><th>Org</th><th>Role</th><th>Last login</th><th class="r">Actions</th>
      </tr></thead>
      <tbody id="rows"><tr><td colspan="5" class="dim">Loading&hellip;</td></tr></tbody>
    </table>
    <div id="listmsg" class="msg"></div>
    <div class="note">Passwords are never shown. Editing an account with the
      password left blank keeps the stored one.</div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
const tok = () => $('token').value.trim();
function withTok(url){ const t = tok(); return t ? url + (url.includes('?')?'&':'?') + 'token=' + encodeURIComponent(t) : url; }
function esc(s){ return (s==null?'':String(s)).replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function fmt(ts){ if(!ts) return '&mdash;'; try{ return new Date(ts).toLocaleString('en-GB',{timeZone:'Asia/Kuala_Lumpur',hour12:false,day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'}); }catch(e){ return esc(ts); } }

async function api(method, url, body){
  const opt = { method, headers:{} };
  if(body){ opt.headers['Content-Type']='application/json'; opt.body=JSON.stringify(body); }
  const r = await fetch(withTok(url), opt);
  let d = null; try{ d = await r.json(); }catch(e){}
  if(!r.ok) throw new Error((d && (d.detail||d.error)) || ('HTTP '+r.status));
  return d;
}

function setMsg(el, text, ok){ el.textContent = text||''; el.className = 'msg ' + (text ? (ok?'ok':'bad') : ''); }

async function load(){
  try{
    const d = await api('GET', '/accounts/list');
    const rows = d.accounts || [];
    if(!rows.length){ $('rows').innerHTML = '<tr><td colspan="5" class="dim">No accounts yet. Add one above.</td></tr>'; return; }
    $('rows').innerHTML = rows.map(a => {
      const role = [];
      if(a.is_primary) role.push('<span class="badge b-primary">Primary</span>');
      role.push(a.active ? '<span class="badge b-active">Active</span>' : '<span class="badge b-off">Off</span>');
      let last;
      if(a.last_error) last = '<span class="badge b-bad">Failed</span> <span class="err mono">'+esc(a.last_error)+'</span>';
      else if(a.last_ok_at) last = '<span class="badge b-ok">OK</span> <span class="dim">'+fmt(a.last_ok_at)+'</span>';
      else last = '<span class="dim">never tested</span>';
      const u = esc(a.username);
      return `<tr>
        <td class="mono">${u}${a.has_password?'':' <span class="err">(no password)</span>'}</td>
        <td class="mono">${esc(a.org_code||'')}</td>
        <td>${role.join(' ')}</td>
        <td>${last}</td>
        <td class="r"><div class="acts">
          <button class="ghost" data-act="test" data-u="${u}">Test</button>
          ${a.is_primary?'':'<button class="ghost" data-act="primary" data-u="'+u+'">Make primary</button>'}
          <button class="ghost" data-act="toggle" data-u="${u}" data-active="${a.active?1:0}">${a.active?'Deactivate':'Activate'}</button>
          <button class="ghost" data-act="edit" data-u="${u}" data-org="${esc(a.org_code||'')}" data-rem="${esc(a.remarks||'')}" data-active="${a.active?1:0}" data-primary="${a.is_primary?1:0}">Edit</button>
          <button class="danger" data-act="delete" data-u="${u}">Delete</button>
        </div></td></tr>`;
    }).join('');
  }catch(e){ setMsg($('listmsg'), 'Load failed: '+e.message, false); }
}

$('rows').addEventListener('click', async ev => {
  const b = ev.target.closest('button'); if(!b) return;
  const act = b.dataset.act, u = b.dataset.u;
  setMsg($('listmsg'), '', true);
  try{
    if(act==='test'){ b.disabled=true; b.textContent='Testing…';
      const d = await api('POST', '/accounts/test?username='+encodeURIComponent(u));
      setMsg($('listmsg'), d.ok ? (u+': login OK ('+(d.org_code||'')+')') : (u+': FAILED — '+((d.err_code?d.err_code+': ':'')+(d.error||''))), !!d.ok);
    } else if(act==='primary'){
      await api('POST', '/accounts/primary?username='+encodeURIComponent(u));
      setMsg($('listmsg'), u+' is now the primary account', true);
    } else if(act==='toggle'){
      const on = b.dataset.active==='1';
      await api('POST', '/accounts/active?username='+encodeURIComponent(u)+'&active='+(on?'false':'true'));
      setMsg($('listmsg'), u+(on?' deactivated':' activated'), true);
    } else if(act==='delete'){
      if(!confirm('Delete account '+u+'?')) return;
      await api('POST', '/accounts/delete?username='+encodeURIComponent(u));
      setMsg($('listmsg'), u+' deleted', true);
    } else if(act==='edit'){
      $('u').value=u; $('p').value=''; $('org').value=b.dataset.org||''; $('rem').value=b.dataset.rem||'';
      $('active').checked=b.dataset.active==='1'; $('primary').checked=b.dataset.primary==='1';
      $('pwhint').textContent='(blank = keep current)'; window.scrollTo({top:0,behavior:'smooth'});
      return;
    }
    await load();
  }catch(e){ setMsg($('listmsg'), act+' failed: '+e.message, false); }
  finally{ if(act==='test'){ b.disabled=false; b.textContent='Test'; } }
});

$('save').addEventListener('click', async () => {
  const body = { username:$('u').value.trim(), password:$('p').value,
    org_code:$('org').value.trim(), remarks:$('rem').value.trim(),
    active:$('active').checked, is_primary:$('primary').checked };
  if(!body.username){ setMsg($('formmsg'),'Username is required',false); return; }
  $('save').disabled=true;
  try{
    await api('POST', '/accounts/save', body);
    setMsg($('formmsg'), 'Saved '+body.username, true);
    $('clear').click(); await load();
  }catch(e){ setMsg($('formmsg'), 'Save failed: '+e.message, false); }
  finally{ $('save').disabled=false; }
});

$('clear').addEventListener('click', () => {
  ['u','p','org','rem'].forEach(id=>$(id).value='');
  $('active').checked=true; $('primary').checked=false; $('pwhint').textContent='';
  setMsg($('formmsg'),'',true);
});

load();
</script>
</body>
</html>"""
