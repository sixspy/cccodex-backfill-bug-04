const msg = document.getElementById('msg');
const view = document.getElementById('view');
let windows = [];

async function api(path, opts={}) {
  const r = await fetch(path, { headers:{'Content-Type':'application/json'}, ...opts });
  const j = await r.json().catch(()=>({}));
  if (!r.ok) {
    // 防重拦截也带回芯片/放行号：把结果挂到错误上，页面照样能亮。
    const err = new Error(j.error || ('HTTP '+r.status));
    err.data = j; err.status = r.status;
    throw err;
  }
  return j;
}
function setMsg(t, err=false){ msg.textContent=t||''; msg.className = err?'err':'muted'; }
async function loadWindows(){ windows = (await api('/api/windows')).items || []; }
function chipHtml(text){ return text ? `<span class="tag chip-${text==='放行'?'go':'block'}">${text}</span>` : ''; }

function renderWindows(){
  view.innerHTML = `<div class="wall">${windows.map(w=>`
    <article class="card">
      <h3>${w.table_name}</h3>
      <p class="muted">${w.start_date} → ${w.end_date}</p>
      <span class="tag">${w.status}</span>
      <p class="muted">切片 ${w.slice_count} · 已落库 ${w.loaded_count}</p>
    </article>`).join('')}</div>`;
}

function winOptions(){ return windows.map(w=>`<option value="${w.id}">#${w.id} ${w.table_name}</option>`).join(''); }

function renderPlan(){
  view.innerHTML = `<div class="card">
    <label>窗口<select id="wid">${winOptions()}</select></label>
    <button class="primary" id="go" type="button">生成切片计划</button>
    <pre id="out" class="muted"></pre>
  </div>`;
  document.getElementById('go').onclick = async ()=>{
    try{
      const wid = document.getElementById('wid').value;
      const j = await api(`/api/windows/${wid}/plan`, {method:'POST', body:'{}'});
      document.getElementById('out').textContent = JSON.stringify(j.slices, null, 2);
      setMsg(`已生成 ${j.slices.length} 片`);
      await loadWindows();
    }catch(e){ setMsg(e.message,true); }
  };
}

async function renderReplay(){
  await loadWindows();
  const wid = windows[0]?.id;
  const slices = wid ? (await api(`/api/windows/${wid}/slices`)).items : [];
  view.innerHTML = `<div class="card">
    <label>窗口<select id="wid">${winOptions()}</select></label>
    <div id="list" class="wall" style="margin-top:0.8rem"></div>
  </div>`;
  const draw = async ()=>{
    const id = document.getElementById('wid').value;
    const items = (await api(`/api/windows/${id}/slices`)).items || [];
    document.getElementById('list').innerHTML = items.map(s=>`
      <article class="card">
        <h3>${s.slice_date}</h3>
        <span class="tag">${s.status}</span>
        <p class="muted">行数 ${s.row_count}</p>
        <button data-id="${s.id}" class="primary" type="button" ${s.status!=='待重放'?'disabled':''}>重放</button>
      </article>`).join('') || '<p class="muted">先生成切片计划</p>';
    document.querySelectorAll('#list button[data-id]').forEach(btn=>{
      btn.onclick = async ()=>{
        try{
          const j = await api(`/api/slices/${btn.dataset.id}/replay`, {method:'POST', body:'{}'});
          setMsg(`已落库 ${j.slice.slice_date} · ${j.slice.row_count} 行`);
          await draw(); await loadWindows();
        }catch(e){ setMsg(e.message,true); }
      };
    });
  };
  document.getElementById('wid').onchange = draw;
  await draw();
}

async function renderCoverage(){
  const items = (await api('/api/coverage-reports')).items || [];
  view.innerHTML = `<div class="card">
    <label>窗口<select id="wid">${winOptions()}</select></label>
    <button class="primary" id="go" type="button">跑覆盖对账</button>
    <div class="wall" style="margin-top:0.8rem">${items.map(r=>`
      <article class="card"><h3>窗口 #${r.window_id}</h3><p>${r.summary}</p></article>`).join('')}</div>
  </div>`;
  document.getElementById('go').onclick = async ()=>{
    try{
      const j = await api('/api/coverage-reports', {method:'POST', body: JSON.stringify({window_id: Number(document.getElementById('wid').value)})});
      setMsg(j.report.summary);
      await renderCoverage();
    }catch(e){ setMsg(e.message,true); }
  };
}


function checkStrip(j){
  // 防重键 / 放行号 / 覆盖条数同屏：一次核对，条数只认一次成功。
  const card = j.card || {}; const a = j.attach || {};
  return `<div class="card msg-row">
      ${chipHtml(card.chip)}
      <span class="muted">放行号 ${card.pass_no || '—'}</span>
      <span class="muted">覆盖条数 ${a.rows ?? 0}</span>
      <span class="muted">后到 ${a.rejects ?? 0} 行（只读历史）</span>
      <span class="tag ${a.doubled ? 'chip-block' : ''}">条数翻倍 ${a.doubled ? '是' : '否'}</span>
    </div>`;
}
async function render_queue(){
  const j = await api('/api/idem/panels');
  const card = j.card || {};
  view.innerHTML = `${checkStrip(j)}
    <div class="wall">${(j.queue||[]).map(r=>`<article class="card"><h3>${r.slice_key}</h3><p>${r.row_payload}</p>${chipHtml(r.chip)}</article>`).join('')||'<p class="muted">空</p>'}
    <article class="card"><button class="primary" id="r" type="button">纠偏重放同片</button></article></div>
    ${(j.history||[]).length ? `<div class="card"><p class="muted">后到行只读历史</p>
      <div class="wall">${j.history.map(h=>`<article class="card"><h3>${h.slice_key}</h3><p>${h.row_payload}</p><p class="muted">${h.reason}</p>${chipHtml(h.chip)}</article>`).join('')}</div></div>` : ''}`;
  const btn = document.getElementById('r');
  const go = async ()=>{
    // 页上先收口：连点时按钮不再派发第二次请求。
    btn.disabled = true;
    try{
      const r = await api('/api/idem/replay',{method:'POST',body:'{"slice_key":"S1"}'});
      setMsg(`放行 ${r.pass_no} · 条数 ${r.count}`);
    }catch(e){
      const d = e.data || {};
      setMsg((d.error || e.message) + (d.chip ? ` · ${d.chip}` : ''), true);
    }finally{
      await render_queue();
    }
  };
  btn.onclick = go;
}
async function render_detail(){
  const j = await api('/api/idem/panels');
  const d = j.detail || {};
  view.innerHTML = `<div class="card">
      <p>片键现行 ${d.live || '—'} · 放行门槛 ${d.gate || '—'}</p>
      <p class="muted">唯一片键索引 ${d.index || '缺失'} · 同源 ${d.same_source ? '是' : '否（双键即拦）'}</p>
    </div>`;
}
async function render_card(){
  const j = await api('/api/idem/panels');
  const c = j.card || {};
  view.innerHTML = `<div class="card">
      ${chipHtml(c.chip)} <span class="tag">${c.fate}</span>
      <p>放行号 ${c.pass_no || '—'}</p><p class="muted">${c.note || ''}</p>
    </div>`;
}
async function render_attach(){
  const j = await api('/api/idem/panels');
  const a = j.attach || {};
  view.innerHTML = `<div class="card">
      <p>覆盖条数 ${a.rows}（成功 ${a.success} 次 · 后到 ${a.rejects} 行留只读历史）</p>
      <p class="muted">条数翻倍 ${a.doubled ? '是' : '否'}</p>
    </div>`;
}

async function show(tab){
  setMsg(''); await loadWindows();
  if (tab==='queue') await render_queue();
  if (tab==='detail') await render_detail();
  if (tab==='card') await render_card();
  if (tab==='attach') await render_attach();
}
document.querySelectorAll('.chips button').forEach(btn=>{
  btn.onclick=()=>{ document.querySelectorAll('.chips button').forEach(b=>b.classList.remove('on')); btn.classList.add('on'); show(btn.dataset.tab); };
});
document.getElementById('btn-logout').onclick = async ()=>{ await api('/api/logout',{method:'POST',body:'{}'}); location.href='/login'; };
show('queue');
