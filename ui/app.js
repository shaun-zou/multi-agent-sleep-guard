/* ==========================================================================
   夜间值守控制台 · 交互层
   业务规则与 /api/* 调用完全沿用旧版；本层只负责：视图切换、状态呈现、
   自定义确认弹窗、忙碌态、空状态与提示。
   ========================================================================== */
const $ = id => document.getElementById(id);
let currentSid = null, snapData = null, monitorRunning = false, confirmToken = null;
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const MODE_CN = {sleep:'睡眠', hibernate:'休眠'};
const DECISION_CN = {start:'开始 start', middle:'进行中 middle', end:'结束 end',
                     running:'进行中 running', queued:'排队 queued',
                     completed:'完成 completed', failed:'失败 failed',
                     cancelled:'已取消 cancelled', ignore:'噪声忽略 ignore'};
const TERMINAL_DECISIONS = new Set(['end','completed','failed','cancelled']);
const SUGGEST_CN = {start:'建议:开始', review:'建议:人工复核', ignore:'建议:噪声'};
const VIEW_TITLES = {workbench:'工作台', rules:'规则表', logs:'日志检索', diagnostics:'诊断与文档'};

async function api(path, method='GET', body){
  const opt = {method, headers:{'Content-Type':'application/json'}};
  if(method === 'POST') opt.body = JSON.stringify(body||{});
  const r = await fetch(path, opt);
  return await r.json();
}

/* ---------------- 提示：状态卡留痕 + 全局轻提示 ---------------- */
let toastTimer = null;
function toast(msg, bad=false){
  const t = $('toast');
  t.textContent = msg;
  t.className = 'toast' + (bad ? ' bad' : '');
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 4200);
}
const activeView = () => document.querySelector('.view:not([hidden])')?.dataset.view;
function notice(msg, bad=false){
  $('notice').textContent = msg;
  $('notice').className = 'callout ' + (bad ? 'callout-bad' : 'callout-neutral');
  // 工作台里状态卡已经显示同一条留痕，不再叠一个 toast；
  // 其他视图看不到状态卡，或出错时，才用全局轻提示补上反馈。
  if(bad || activeView() !== 'workbench') toast(msg, bad);
}

/* ---------------- 忙碌态：防重复提交，不篡改 disabled 的业务状态 ---------------- */
const inflight = new Set();
function busyBar(on){ $('busyBar').classList.toggle('on', on); }
function once(btn, fn){
  return async (...a) => {
    if(inflight.has(btn.id)) return;
    inflight.add(btn.id);
    btn.setAttribute('aria-busy','true');
    busyBar(true);
    try{ await fn(...a); }
    finally{
      inflight.delete(btn.id);
      btn.removeAttribute('aria-busy');
      if(!inflight.size) busyBar(false);
    }
  };
}

/* ---------------- 自定义确认弹窗（替代原生 confirm / prompt） ----------------
   用 dialog.showModal()：焦点自动进入对话框、Esc 关闭、关闭后焦点回到触发元素。
   requireText 时返回输入的字符串，否则返回 true/false。 */
let pendingAsk = null;
function ask({title='请确认', body='', okText='确认', danger=false, requireText=null}={}){
  const dlg = $('askDialog');
  if(typeof dlg.showModal !== 'function'){         // 老内核兜底
    return Promise.resolve(window.confirm(body) ? (requireText || true) : false);
  }
  if(dlg.open) return Promise.resolve(false);
  $('askTitle').textContent = title;
  $('askBody').textContent = body;
  $('askOk').textContent = okText;
  dlg.classList.toggle('is-danger', !!danger);
  $('askInputWrap').hidden = !requireText;
  $('askInputLabel').textContent = `请输入 ${requireText} 确认`;
  $('askInput').value = '';
  $('askInput').setAttribute('aria-label', `请输入 ${requireText} 确认`);
  return new Promise(resolve => {
    pendingAsk = {resolve, requireText};
    dlg.showModal();
    (requireText ? $('askInput') : $('askOk')).focus();
  });
}
function finishAsk(value){
  const dlg = $('askDialog');
  const p = pendingAsk;
  pendingAsk = null;
  if(dlg.open) dlg.close();
  if(p) p.resolve(value);
}
$('askOk').addEventListener('click', () => {
  const p = pendingAsk;
  if(!p) return;
  if(p.requireText){
    const v = $('askInput').value.trim();
    if(v !== p.requireText){
      $('askInput').focus();
      $('askInput').setAttribute('aria-invalid','true');
      toast(`确认词不匹配，请输入 ${p.requireText}`, true);
      return;
    }
    $('askInput').removeAttribute('aria-invalid');
    finishAsk(v);
  }else finishAsk(true);
});
$('askInput').addEventListener('input', () => $('askInput').removeAttribute('aria-invalid'));
$('askInput').addEventListener('keydown', e => { if(e.key === 'Enter'){ e.preventDefault(); $('askOk').click(); } });
$('askCancel').addEventListener('click', () => finishAsk(false));
$('askX').addEventListener('click', () => finishAsk(false));
$('askDialog').addEventListener('cancel', e => { e.preventDefault(); finishAsk(false); });
$('askDialog').addEventListener('close', () => {
  if(pendingAsk){ const p = pendingAsk; pendingAsk = null; p.resolve(false); }
});

/* ---------------- 视图切换 ---------------- */
function setView(name, persist=true){
  if(!VIEW_TITLES[name]) name = 'workbench';
  document.querySelectorAll('.view').forEach(v => {
    const on = v.dataset.view === name;
    v.hidden = !on;
    v.classList.toggle('active', on);
  });
  document.querySelectorAll('.nav-item').forEach(b => {
    const on = b.dataset.view === name;
    b.classList.toggle('active', on);
    if(on) b.setAttribute('aria-current','page'); else b.removeAttribute('aria-current');
  });
  $('viewTitle').textContent = VIEW_TITLES[name];
  if(persist){ try{ localStorage.setItem('asg.view', name); }catch(_e){} }
  window.scrollTo({top:0, behavior: prefersReduced() ? 'auto' : 'smooth'});
}
const prefersReduced = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;
document.querySelectorAll('.nav-item').forEach(b => {
  b.addEventListener('click', () => setView(b.dataset.view));
});
/* ---------------- 流程步骤条 + 工作台两个子页面 ----------------
   状态优先级：当前步骤（蓝） > 已执行步骤（浅绿） > 未开始（中性）。
   「执行过」是历史事件：停止监控 / 状态回退时 refresh() 会把当前步推回前面，
   但已完成的步骤不该跟着变灰，所以单独记一个会话内集合。
   子页面：prepare（步骤 1、2 的内容框）/ watch（步骤 3、4 的内容框）。
   手动点步骤标签 = 用户主动查看，锁定子页面；3 秒轮询不抢。
   唯一的自动切换：当前步骤真的跨过 2→3 边界那一刻。 */
const executedSteps = new Set();
const STEP_LABELS = {1:'扫描任务', 2:'人工核对', 3:'开启监控', 4:'早上自检'};
const STAGE_INFO = {
  1:{name:'01 / 扫描任务', desc:'重新扫描本机 Agent，生成这一轮要盯的任务清单。', next:'下一步：人工核对'},
  2:{name:'02 / 人工核对', desc:'勾选保留、剔除误判，核对并锁定这份清单。', next:'下一步：开启监控'},
  3:{name:'03 / 开启监控', desc:'确认待机方式后开启值守；任务全部终结才会请求待机。', next:'下一步：早上自检'},
  4:{name:'04 / 早上自检', desc:'唤醒后生成报告，确认昨晚是否真的进入低功耗。', next:'本轮完成'}
};
let currentStep = 1;
let stepSig = '';
let pageLock = false;           // 手动选择的子页面，别被 3 秒轮询覆盖
let prevStepForPage = null;     // 首次只记录；之后仅在跨 2→3 边界时自动切一次
let sleepQuietUntil = 0;        // 手动直接睡眠后短暂静默轮询，避免挂起瞬间刷出假异常

function setPage(page, byUser=false){
  document.querySelectorAll('.subpage').forEach(p => {
    const on = p.dataset.page === page;
    p.hidden = !on;
    p.classList.toggle('active', on);
  });
  if(byUser) pageLock = true;
}
function markStepDone(n){
  executedSteps.add(Number(n));
  renderSteps();
}
function renderSteps(){
  const sig = `${currentStep}|${[...executedSteps].sort().join(',')}`;
  if(sig === stepSig) return;          // 3 秒轮询只在状态真变化时动 DOM
  stepSig = sig;
  document.querySelectorAll('.step').forEach(el => {
    const i = Number(el.dataset.step);
    const on = i === currentStep;                       // 当前步骤蓝色优先
    const done = !on && (i < currentStep || executedSteps.has(i));
    el.classList.toggle('on', on);
    el.classList.toggle('done', done);
    if(on) el.setAttribute('aria-current','step'); else el.removeAttribute('aria-current');
    const state = on ? '当前步骤' : done ? '已完成' : '未开始';
    el.setAttribute('aria-label', `${STEP_LABELS[i]}，${state}`);
    el.title = `${STEP_LABELS[i]} · ${state}`;
    const badge = el.querySelector('.step-n');
    if(badge) badge.innerHTML = (on || !done)
      ? String(i)
      : '<svg class="ico" aria-hidden="true"><use href="#i-check"/></svg>';
  });
  const st = STAGE_INFO[currentStep] || STAGE_INFO[1];
  $('stageName').textContent = st.name;
  $('stageDesc').textContent = st.desc;
  $('stageNext').textContent = st.next;
}
function setStep(n){
  currentStep = Number(n) || 1;
  renderSteps();
}
/* 只切换子页面，不做滚动定位；也不篡改「当前步骤」：
   蓝色高亮代表系统真实阶段，手动点击只是查看另一页，不该让状态说谎。 */
document.querySelectorAll('.step').forEach(el => {
  el.addEventListener('click', () => setPage(el.dataset.page, true));
});

/* ---------------- 待机方式 ---------------- */
function fmtAge(sec){
  if(sec === null || sec === undefined) return '尚未检测';
  if(sec < 60) return '刚刚检测';
  if(sec < 3600) return Math.round(sec/60) + ' 分钟前检测';
  return (sec/3600).toFixed(1) + ' 小时前检测';
}
let powerState = null, powerSig = '';
function renderPower(pw){
  powerState = pw || {};
  const opts = powerState.options || [];
  const box = $('choices');
  if(!opts.length){
    box.innerHTML = '<div class="hint">尚未检测到电源能力，点「检测可用性」</div>';
    return;
  }
  const rec = powerState.recommend || {};
  // 每 3 秒刷新，只在结构性信息变化时重建 DOM，避免用户正在点单选钮时元素被替换
  const sig = JSON.stringify([powerState.prefer, powerState.locked_by_monitor,
    opts.map(o => [o.value, o.available])]);
  if(sig !== powerSig){
    powerSig = sig;
    const locked = !!powerState.locked_by_monitor;
    box.innerHTML = opts.map(o=>{
      const on = o.value === powerState.prefer;
      const unknown = (o.available === null || o.available === undefined);
      const bad = o.available === false;
      const badge = unknown ? '<span class="badge b-info">未检测</span>'
                  : bad ? '<span class="badge b-warn">不可用</span>'
                        : '<span class="badge b-ok">可用</span>';
      return `<label class="choice${on?' on':''}${bad?' off':''}" data-v="${esc(o.value)}">
        <input type="radio" name="prefer" value="${esc(o.value)}"${on?' checked':''}${(bad||locked)?' disabled':''}>
        <span class="cbody">
          <span class="ctitle">${esc(o.label)} ${badge}</span>
          ${bad ? `<span class="creason">${esc(o.reason || '本机不支持')}</span>` : ''}
        </span></label>`;
    }).join('');
    box.querySelectorAll('input[name=prefer]').forEach(el=>{
      el.addEventListener('change', () => setPrefer(el.value));
    });
  }
  $('probeMeta').textContent =
    fmtAge(powerState.age_seconds) +
    (powerState.stale ? '（可能已过期，建议重新检测）' : '') +
    (powerState.locked_by_monitor ? ' · 监控运行中，暂不可改' : '') +
    (rec.text ? ` · ${rec.text}` : '');
  const cur = opts.find(o => o.value === powerState.prefer) || {};
  if(cur.available === false && (cur.fix || []).length){
    $('fixBox').hidden = false;
    $('fixBox').innerHTML =
      `<b>${esc(cur.label)} 在本机不可用</b>：${esc(cur.reason || '')}<br>` +
      cur.fix.map(x => '· ' + esc(x)).join('<br>');
  }else{
    $('fixBox').hidden = true;
  }
}
async function setPrefer(v){
  notice(`正在切换到${MODE_CN[v] || v}…`);
  const r = await api('/api/sleep/preference','POST',
    {prefer:v, operator:$('operator').value || 'ui'});
  if(!r.ok){ notice('切换失败：' + r.message, true); await refresh(); return; }
  notice(r.message || '已保存待机方式');
  await refresh();
}

/* ---------------- 主状态刷新 ---------------- */
async function refresh(){
  if(Date.now() < sleepQuietUntil) return;   // 系统正在进入低功耗，别发注定失败的请求
  try{
    const r = await api('/api/state');
    if(!r.ok){ notice('状态读取失败: ' + r.message, true); return; }
    const d = r.data;
    monitorRunning = d.monitor.running;
    confirmToken = (d.confirm || {}).token || null;
    const sel = $('snapSel');
    const prev = sel.value;
    sel.innerHTML = '';
    (d.snapshots||[]).forEach(s => {
      const o = document.createElement('option');
      o.value = s.snapshot_id;
      o.textContent = `${s.snapshot_id}  v${s.version}  ${s.status}  ${s.task_count}项${s.verified?'  已核对':''}`;
      sel.appendChild(o);
    });
    if(prev && [...sel.options].some(o=>o.value===prev)) sel.value = prev;
    else if(!currentSid && sel.options.length) currentSid = sel.value;
    if(currentSid && [...sel.options].some(o=>o.value===currentSid)) sel.value = currentSid;

    const lockTxt = d.lock ? `${d.lock.status} (pid ${d.lock.pid})` : '无锁';
    const pw0 = d.power || {};
    const curOpt = (pw0.options||[]).find(o=>o.value===pw0.prefer) || {};
    const availTxt = curOpt.available === true ? '本机可用'
                   : curOpt.available === false ? '本机不可用' : '未检测';
    const row = (k,v) => `<dt>${esc(k)}</dt><dd>${v}</dd>`;
    $('kv').innerHTML =
      row('值守模式', '真实（开启监控时确认一次）') +
      row('一次确认令牌', confirmToken ? '已就绪（用后即焚）' : '未就绪') +
      row('待机方式', `${esc(MODE_CN[pw0.prefer] || pw0.prefer || 'sleep')}（只发一条，不补发）· ${esc(availTxt)}`) +
      row('电源探测', `${esc(fmtAge(pw0.age_seconds))} · 待机=${esc((pw0.capabilities||{}).sleep_kind || ((pw0.capabilities||{}).sleep_available?'可用':'不可用'))} · 休眠=${(pw0.capabilities||{}).hibernate_available?'可用':'不可用'}`) +
      row('监控进程', monitorRunning ? `运行中 pid ${d.monitor.pid} · ${d.monitor.dry_run===false?`将请求${MODE_CN[d.monitor.requested_mode]||'睡眠'}`:'演练'}` : '未运行') +
      (d.monitor.note ? row('状态提示', esc(d.monitor.note)) : '') +
      row('锁状态', esc(lockTxt)) +
      row('最近快照', esc(d.snapshots[0]?.snapshot_id || '无')) +
      row('最近睡眠记录', esc(d.sleep_intent?.snapshot_id || '无') + ' ' + (d.sleep_intent?.dry_run?'(演练)':'(非演练)')) +
      row('快照总数', `${(d.snapshots||[]).length} 份`);

    // Hero：全页唯一的主状态，一眼看清「今晚要不要管、管什么」
    $('heroStatus').textContent = monitorRunning ? '监控运行中' : '待命';
    $('heroStatus').classList.toggle('is-running', monitorRunning);
    $('dot').classList.toggle('is-running', monitorRunning);
    $('heroDesc').textContent = monitorRunning
      ? `已绑定 ${d.monitor.snapshot_id || currentSid || '快照'}；任务全部终结并连续稳定后请求${MODE_CN[d.monitor.requested_mode||pw0.prefer]||'睡眠'}。关闭本页不会中断。`
      : (snapData?.verified ? '清单已锁定，确认待机方式后即可开启监控。' : '先扫描并核对任务清单，再开启监控。');
    $('heroMetricTasks').textContent = snapData ? `${snapData.tasks.length} 项` :
      (d.snapshots[0] ? `${d.snapshots[0].task_count} 项` : '未扫描');
    $('heroMetricMode').textContent = MODE_CN[pw0.prefer] || pw0.prefer || 'sleep';
    $('heroMetricProc').textContent = monitorRunning ? `pid ${d.monitor.pid}` : '未运行';

    $('stateText').textContent = monitorRunning
      ? `监控运行中 · ${d.monitor.dry_run===false?`将请求${MODE_CN[d.monitor.requested_mode]||'睡眠'}`:'演练'}`
      : (d.monitor.pid_reused ? '待命 · 已自动复位被复用的旧监控状态' : '待命 · 点「开启监控」并确认后值守');
    $('confirmHint').textContent = (d.confirm || {}).note ||
      '开启监控时确认一次即可；确认令牌用后即焚，不留持久开关';
    $('powerBadge').textContent = '待机方式：' + (MODE_CN[pw0.prefer] || pw0.prefer || 'sleep');
    $('powerBadge').className = 'badge ' + (curOpt.available === false ? 'b-warn' : 'b-info');
    if(pw0.effect) $('powerHint').textContent = pw0.effect;
    renderPower(pw0);
    $('btnStop').disabled = !monitorRunning;
    $('btnMonitor').disabled = monitorRunning;
    // 直接睡眠与值守流程互斥：监控还在跑就睡，唤醒后监控可能再发一条命令
    $('btnSleepNow').disabled = monitorRunning;
    $('btnSleepNow').title = monitorRunning
      ? '监控运行中：请先「优雅停止」再直接睡眠，避免唤醒后被再次触发睡眠'
      : '不经过任务终态判定，确认后立刻让电脑进入低功耗';
    // 早上自检做过之后就停在第 4 步（蓝），前面 1–3 保持浅绿；
    // 只有重新开启监控才把当前步推回第 3 步。
    const derived = monitorRunning ? 3 : (snapData?.verified ? 3 : (snapData ? 2 : 1));
    setStep((executedSteps.has(4) && !monitorRunning) ? 4 : derived);
    // 唯一的自动切换：当前步骤真的从 ≤2 跨到 ≥3 那一刻切到 watch。
    // 一旦用户手动点过步骤标签（pageLock=true）就不再抢——否则点步骤 1 想看
    // 任务清单/时间线，下一轮轮询又被推回值守页。开启监控/自检成功会显式
    // setPage('watch') 并解锁，不依赖这里。
    if(prevStepForPage !== null && prevStepForPage <= 2 && currentStep >= 3 && !pageLock){
      setPage('watch');
    }
    prevStepForPage = currentStep;
  }catch(e){ notice('状态刷新异常: ' + e, true); }
}

/* ---------------- 快照与任务清单 ---------------- */
async function loadSnapshot(sid){
  sid = sid || $('snapSel').value;
  if(!sid){ notice('请先扫描生成快照', true); return; }
  currentSid = sid;
  const r = await api('/api/snapshot?id=' + encodeURIComponent(sid));
  if(!r.ok){ notice('读取快照失败: ' + r.message, true); return; }
  snapData = r.data;
  const s = snapData;
  const changes = (s.change_log||[]).map(c=>`v${c.from_version}→v${c.to_version} 增:${(c.added||[]).length} 删:${(c.removed||[]).length}`).join(' | ');
  $('snapMeta').textContent =
    `快照 ${s.snapshot_id} · v${s.version} · 来源 ${s.source} · ${s.tasks.length} 项\n` +
    `状态 ${s.status} · 已核对 ${s.verified} · 已锁定 ${s.locked} · 重试决定 ${(s.retry_risk||{}).decision || '未记录'}\n` +
    (changes ? `版本变更: ${changes}` : '版本变更: 无');
  const tb = $('tasks').querySelector('tbody');
  tb.innerHTML = '';
  if(!(s.tasks||[]).length){
    tb.innerHTML = '<tr class="empty-row"><td colspan="6">这份快照里没有任务。可点「重新扫描」重取一次清单。</td></tr>';
  }else{
    (s.tasks||[]).forEach(t => {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td><input type="checkbox" class="keep" value="${esc(t.task_id)}" aria-label="保留任务 ${esc(t.task_id)}" checked></td>` +
        `<td>${esc(t.task_id)}</td><td>${esc(t.agent)}</td><td>${esc(t.start_time)}</td>` +
        `<td class="s-${esc(t.status)}">${esc(t.status)}</td><td>${esc(t.evidence||'-')}</td>`;
      tb.appendChild(tr);
    });
  }
  // 时间线下拉与当前快照保持同步；保留用户上一次选中的任务
  const tlSel = $('timelineTask');
  const prevTid = tlSel.value;
  tlSel.innerHTML = (s.tasks||[]).map(t =>
    `<option value="${esc(t.task_id)}">${esc(t.task_id)}　·　${esc(t.status)}</option>`).join('');
  if(prevTid && [...tlSel.options].some(o=>o.value===prevTid)) tlSel.value = prevTid;
  if($('timelineBox').open) loadTimeline();
  const editable = s.status === 'draft';
  document.querySelectorAll('.keep').forEach(c => c.disabled = !editable);
  $('btnVerify').disabled = !editable;
  setStep(editable ? 2 : 3);
  notice(`已读取 ${s.snapshot_id}：${s.tasks.length} 项任务` + (editable ? '，可勾选核对' : '（已锁定，不可再修订）'));
  await refresh();
}
/* 快照载入全部自动触发：下拉框 change / 扫描完成 / 核对锁定后 / 首屏初始化。
   不再提供「读取清单」按钮（选同一个版本重读没有实际价值）。 */
$('snapSel').addEventListener('change', () => loadSnapshot($('snapSel').value));

/* ---------------- 运行期状态时间线（只读，与冻结快照分开） ---------------- */
const TL_LABEL = {STATUS_CHANGE:'状态变化', TASK_TERMINAL:'到达终态', RETRY_CHAIN_NOTE:'重试链留痕'};
async function loadTimeline(){
  const tid = $('timelineTask').value;
  if(!tid){ $('timelineBody').innerHTML = '<p class="hint">这份快照里没有任务。</p>'; return; }
  $('timelineBody').innerHTML = '<p class="hint">读取轨迹…</p>';
  const scope = $('timelineAll').checked ? 'all' : 'snapshot';
  const r = await api('/api/task-timeline?task_id=' + encodeURIComponent(tid) +
                      '&snapshot=' + encodeURIComponent(currentSid || '') +
                      '&scope=' + scope);
  if(!r.ok){
    $('timelineBody').innerHTML = '<div class="callout callout-bad">' + esc(r.message) + '</div>';
    return;
  }
  renderTimeline(r.data);
}
function renderTimeline(d){
  const rows = [];
  const b = d.baseline;
  if(b){
    rows.push(`<li class="tl-item tl-baseline">
      <span class="tl-dot" aria-hidden="true"></span>
      <div class="tl-card">
        <div class="tl-head"><time>${esc(b.created_at || '—')}</time><span class="tl-badge b-info">扫描时基线</span></div>
        <p class="tl-main">状态 <strong>${esc(b.status || '—')}</strong>（冻结于 ${esc(b.snapshot_id || '—')}）</p>
        <p class="tl-sub">依据 ${esc(b.evidence || '—')} · 开始 ${esc(b.start_time || '—')}</p>
      </div></li>`);
  }
  (d.events || []).forEach(e => {
    const terminal = e.event === 'TASK_TERMINAL';
    const label = TL_LABEL[e.event] || e.event;
    const trans = (e.from_status && e.to_status)
      ? `${esc(e.from_status)} <span class="tl-arrow">→</span> ${esc(e.to_status)}`
      : (terminal ? `终态 <strong>${esc(e.status || '—')}</strong>` : esc(e.msg || ''));
    rows.push(`<li class="tl-item ${terminal ? 'tl-terminal' : 'tl-change'}">
      <span class="tl-dot" aria-hidden="true"></span>
      <div class="tl-card">
        <div class="tl-head"><time>${esc(e.ts)}</time><span class="tl-badge ${terminal ? 'b-ok' : 'b-info'}">${esc(label)}</span></div>
        <p class="tl-main">${trans}</p>
        <p class="tl-sub">${esc(e.evidence || e.msg || '')} · ${esc(e.file || '')}</p>
      </div></li>`);
  });
  let body = `<ol class="timeline-list">${rows.join('')}</ol>`;
  if(!(d.events || []).length){
    body += `<div class="callout callout-neutral">该任务在监控运行期<strong>没有状态迁移记录</strong>。
      只有监控进程在跑时才会每 30 秒回查并写出 STATUS_CHANGE / TASK_TERMINAL；重新扫描生成的是另一份快照，不会回写这里。</div>`;
  }
  if(d.truncated) body += `<p class="hint">事件过多，仅显示 ${d.events.length} / ${d.total} 条。</p>`;
  if(d.scope === 'snapshot' && d.other_runs > 0){
    body += `<div class="callout callout-neutral">该任务 ID 在<strong>其他 ${esc(String(d.other_runs))} 次值守</strong>里也有记录（ID 会跨轮复用）。勾选上方「包含其他值守历史」可查看完整轨迹。</div>`;
  }
  body += `<p class="hint">扫描 ${esc(String(d.files_scanned || 0))} 份 monitor 日志 · ${esc(d.note || '')}</p>`;
  $('timelineBody').innerHTML = body;
}
$('timelineTask').addEventListener('change', loadTimeline);
$('timelineAll').addEventListener('change', loadTimeline);
$('btnTimeline').addEventListener('click', loadTimeline);
$('timelineBox').addEventListener('toggle', () => { if($('timelineBox').open) loadTimeline(); });

async function doScan(auto){
  notice(auto ? '正在自动发现并扫描…' : '正在扫描…');
  const r = await api('/api/scan', 'POST', {auto_discover: auto});
  if(!r.ok){ notice('扫描失败: ' + r.message, true); return; }
  const sid = r.data.snapshot_id;
  await refresh();
  if(sid){ currentSid = sid; $('snapSel').value = sid; await loadSnapshot(sid); }
  markStepDone(1);
  notice(`扫描完成，快照 ${sid || '（见日志）'}`);
}
$('btnScan').addEventListener('click', once($('btnScan'), () => doScan(false)));
$('btnScanAuto').addEventListener('click', once($('btnScanAuto'), () => doScan(true)));

$('btnVerify').addEventListener('click', once($('btnVerify'), async () => {
  if(!snapData){ notice('请先读取快照', true); return; }
  const sid = snapData.snapshot_id;
  const removed = [...document.querySelectorAll('.keep')].filter(c=>!c.checked).map(c=>c.value);
  const decision = (document.querySelector('input[name=rr]:checked') || {}).value || 'reuse';
  const watch = [...document.querySelectorAll('.keep')].filter(c=>c.checked).map(c=>c.value).filter(v=>v.startsWith('wb-session'));
  let target = sid;
  if(removed.length){
    const r = await api('/api/revise','POST',{snapshot:sid,removed,reason:$('reason').value,operator:$('operator').value});
    if(!r.ok){ notice('修正清单失败: ' + r.message, true); return; }
    target = r.data.snapshot_id;
  }
  const v = await api('/api/verify','POST',{snapshot:target,decision,watch_tasks:watch,operator:$('operator').value});
  if(!v.ok){ notice('核对锁定失败: ' + v.message, true); return; }
  currentSid = target;
  await refresh(); $('snapSel').value = target; await loadSnapshot(target);
  markStepDone(2);
  // 核对锁定完成，当前步骤进入 STEP 03 —— 自动落到子页面二（值守与自检），
  // 让用户紧接着就能开启监控；同时解除手动锁，避免后续轮询把页面拉回准备清单。
  setPage('watch');
  pageLock = false;
  prevStepForPage = currentStep;
  notice(`已核对锁定 ${target}（重试风险采用默认：沿用原任务 ID，无额外处理）`);
}));

/* ---------------- 直接睡眠（不经监控，确认后立刻发一条命令） ---------------- */
$('btnSleepNow').addEventListener('click', once($('btnSleepNow'), async () => {
  const mode = (powerState || {}).prefer || 'sleep';
  const modeCn = MODE_CN[mode] || mode;
  const ok = await ask({
    title:'立即让电脑睡眠',
    body:`这会立刻发出一条按「${modeCn}」的命令，只发一条、不补发。\n\n` +
         `请先保存正在编辑的文件；正在运行的 Agent 任务会被暂停。\n` +
         `不经过任务终态判定，也不会启动监控；唤醒后可照常使用本控制台。\n` +
         `确认现在就睡？`,
    okText:'立即睡眠'});
  if(!ok) return;
  if(!confirmToken){
    notice('确认令牌未就绪，请稍等状态刷新后重试', true);
    await refresh();
    return;
  }
  const r = await api('/api/sleep/now','POST',
    {confirm_token:confirmToken, operator:$('operator').value || 'ui'});
  if(!r.ok){ notice('直接睡眠失败：' + r.message, true); await refresh(); return; }
  sleepQuietUntil = Date.now() + 30000;       // 唤醒后自动恢复轮询
  const done = `已发出${modeCn}命令（pid ${r.data.pid}）。电脑即将进入低功耗，唤醒后回来即可。`;
  notice(done);
  toast(done);                                 // 系统即将挂起，全局轻提示比右列留痕更醒目
}));

/* ---------------- 开启 / 停止监控 ---------------- */
$('btnMonitor').addEventListener('click', once($('btnMonitor'), async () => {
  const sid = $('snapSel').value;
  if(!sid){ notice('请先选择快照', true); return; }
  const n = snapData && snapData.snapshot_id === sid ? snapData.tasks.length : '?';
  const pw = powerState || {};
  const mode = pw.prefer || 'sleep';
  const cur = (pw.options||[]).find(o => o.value === mode) || {};
  const modeLine = `待机方式：${MODE_CN[mode] || mode}（${cur.label || mode}）` +
    (cur.available === false ? ' — 本机不可用，可能不会真的进入低功耗' : '');
  const msg = `快照 ${sid}（${n} 项任务）\n${modeLine}\n\n` +
              `任务全部完成并连续稳定后，电脑会真的请求该待机方式。\n` +
              `监控在独立进程运行，关闭本页不会中断；早上可回来跑自检。`;
  const ok = await ask({title:'开启夜间值守', body:msg, okText:'确认开启'});
  if(!ok) return;
  if(!confirmToken){ notice('确认令牌未就绪，请稍等状态刷新后重试', true); await refresh(); return; }
  notice('正在启动监控…');
  const r = await api('/api/monitor/start','POST',
    {snapshot:sid, confirm_token:confirmToken, operator:$('operator').value||'ui'});
  if(!r.ok){ notice('启动失败: ' + r.message, true); await refresh(); return; }
  notice((r.message ? r.message + '\n' : '') +
    `监控已启动 pid=${r.data.pid}，绑定 ${sid}。关闭本页不影响监控。`);
  markStepDone(3);
  // 开启监控成功 = 当前步骤进入 STEP 03，明确切到子页面二（不依赖轮询跨越检测）
  setStep(3);
  setPage('watch');
  pageLock = false;
  prevStepForPage = 3;
  await refresh();
}));

$('btnStop').addEventListener('click', once($('btnStop'), async () => {
  const ok = await ask({title:'停止监控', body:'监控会优雅收尾并放弃睡眠，不会发出任何睡眠命令。', okText:'停止监控'});
  if(!ok) return;
  const r = await api('/api/monitor/stop','POST',{});
  notice(r.message || (r.ok ? '已请求停止' : '停止失败'), !r.ok);
  await refresh();
}));

$('btnSelfcheck').addEventListener('click', once($('btnSelfcheck'), async () => {
  const r = await api('/api/selfcheck','POST',{});
  if(!r.ok){ notice('自检失败: ' + r.message, true); return; }
  $('report').textContent = r.data.report || '';
  $('reportBox').hidden = false;
  setStep(4);                 // 先置当前（蓝），再标记已执行：蓝色优先不被浅绿覆盖
  markStepDone(4);
  setPage('watch');           // 自检报告属于子页面二，确保它可见
  pageLock = false;
  prevStepForPage = 4;
  notice('自检报告已生成');
  $('reportBox').scrollIntoView({behavior: prefersReduced() ? 'auto' : 'smooth', block:'nearest'});
}));

/* ---------------- 电源能力检测（结果留在本卡片，不再跳到隐藏视图） ---------------- */
$('btnProbe').addEventListener('click', once($('btnProbe'), async () => {
  $('probeMeta').textContent = '正在检测 powercfg /a …';
  const r = await api('/api/powercap','POST',{});
  if(!r.ok){ notice('电源能力探测失败: ' + r.message, true); return; }
  const d = r.data, h = d.hints || {}, c = d.capabilities || {};
  const lines = (d.options||[]).map(o => {
    const mark = o.available === true ? '[可用]' : (o.available === false ? '[不可用]' : '[未检测]');
    return `${mark} ${o.label}` + (o.reason ? ` — ${o.reason}` : '');
  });
  $('powerProbe').hidden = false;
  $('powerProbe').textContent =
    d.summary + '\n\n' + lines.join('\n') + '\n\n' +
    '选择建议: ' + ((d.recommend||{}).text || '-') + '\n\n' +
    d.effect + '\n\n' +
    '坚持只 sleep:\n' + (h.force_sleep||[]).map(x=>'  - '+x).join('\n') + '\n\n' +
    '首选 hibernate:\n' + (h.prefer_hibernate||[]).map(x=>'  - '+x).join('\n') + '\n\n' +
    'powercfg /a (探测于 ' + (c.probed_at||'-') + '):\n' +
    (c.powercfg_a||'').split('\n').slice(0,12).join('\n') +
    (c.blockers && c.blockers.length
      ? '\n\npowercfg /requests 里持有电源请求的项:\n' + c.blockers.map(x=>'  - '+x).join('\n')
      : '');
  await refresh();
  notice('电源能力已刷新（只影响「请求哪一种」，不改变只发一条/不补发）');
}));

/* ---------------- 规则表 ---------------- */
async function loadRules(){
  const r = await api('/api/rules');
  if(!r.ok){ $('rulesMeta').textContent = '规则读取失败: ' + r.message; return; }
  renderRules(r.data);
}
function renderRules(d){
  const box = $('rulesPending');
  const pending = d.pending || [];
  const suggestions = {ignore: pending.filter(p=>p.suggestion==='ignore').length};
  const total = d.pending_total ?? pending.length;
  $('rulesMeta').textContent = `指纹 ${d.fingerprint} · 待确认 ${total} 条` +
    (d.classified_events !== undefined ? ` · 已归类 ${d.classified_events}` : '');
  const badge = $('navRulesCount');
  badge.hidden = !total;
  badge.textContent = String(total);
  badge.setAttribute('aria-label', `${total} 条待确认草案`);
  $('btnRulesNoise').disabled = !suggestions.ignore;

  if(!pending.length){
    box.innerHTML = '<div class="callout callout-neutral">没有待确认草案；新事件出现后先「重新采样」再看。</div>';
  }else{
    box.innerHTML = pending.map(p => {
      const def = p.suggestion === 'ignore' ? 'ignore' : 'middle';
      const opts = Object.entries(DECISION_CN)
        .map(([v,l]) => `<option value="${v}"${v===def?' selected':''}>${l}</option>`).join('');
      return `<div class="draft" data-a="${esc(p.adapter)}" data-k="${esc(p.key)}">
        <span class="dkey">${esc(p.adapter)}:${esc(p.key)}</span>
        <span class="dmeta">×${p.count} ${esc(SUGGEST_CN[p.suggestion] || '')}</span>
        <select aria-label="确认 ${esc(p.adapter)}:${esc(p.key)} 的判定">${opts}</select>
        <button type="button" class="btn btn-secondary draft-ok">确认</button>
      </div>`;
    }).join('');
    box.querySelectorAll('.draft').forEach(el => {
      el.querySelector('.draft-ok').addEventListener('click', () => approveDraft(el));
    });
  }

  const pats = d.ignore_patterns || [];
  const adapters = [...new Set(pats.map(p=>p.adapter).concat(['autoclaw_log','workbuddy_local','trae_agent']))];
  $('patAdapter').innerHTML = adapters.map(a=>`<option>${esc(a)}</option>`).join('');
  $('patList').innerHTML = pats.length
    ? '<div class="chips">' + pats.map(p =>
        `<span class="chip"><span class="adp">${esc(p.adapter)}:</span>${esc(p.pattern)}` +
        `<button type="button" data-a="${esc(p.adapter)}" data-p="${esc(p.pattern)}" aria-label="删除前缀 ${esc(p.adapter)}:${esc(p.pattern)}" title="删除该前缀，恢复保守判定">×</button></span>`
      ).join('') + '</div>'
    : '<div class="hint">暂无前缀忽略规则</div>';
  $('patList').querySelectorAll('button').forEach(btn => {
    btn.addEventListener('click', async () => {
      const a = btn.dataset.a, p = btn.dataset.p;
      const ok = await ask({title:'移除前缀忽略规则',
        body:`移除 ${a}: ${p}？\n该命名空间下的新事件将恢复「未归类按进行中」的保守判定。`,
        okText:'移除'});
      if(!ok) return;
      const r = await api('/api/rules/pattern','POST',
        {action:'remove', adapter:a, pattern:p, operator:$('operator').value||'ui'});
      notice(r.message || (r.ok?'已移除':'失败'), !r.ok);
      await loadRules();
    });
  });
}
async function approveDraft(el){
  const adapter = el.dataset.a, key = el.dataset.k;
  const decision = el.querySelector('select').value;
  const risk = TERMINAL_DECISIONS.has(decision)
    ? `\n注意：判为「${DECISION_CN[decision]}」意味着该值一出现就视任务结束；若 Agent 实际仍在运行，会提前判完成并可能提前睡眠。`
    : decision === 'ignore' ? '\n该事件将作为噪声忽略，不再参与任务判定。' : '';
  const ok = await ask({title:'确认规则语义',
    body:`${adapter}:${key} → ${DECISION_CN[decision]}${risk}`, okText:'确认规则'});
  if(!ok) return;
  const r = await api('/api/rules/approve','POST',
    {adapter, key, decision, operator:$('operator').value||'ui'});
  notice(r.message || (r.ok?'已确认':'失败'), !r.ok);
  await loadRules();
}
$('btnRulesLoad').addEventListener('click', once($('btnRulesLoad'), loadRules));
$('btnRulesSample').addEventListener('click', once($('btnRulesSample'), async () => {
  const r = await api('/api/rules/sample','POST',{});
  notice(r.ok ? `已重新采样，指纹 ${r.data.fingerprint}` : '采样失败: ' + r.message, !r.ok);
  await loadRules();
}));
$('btnRulesNoise').addEventListener('click', once($('btnRulesNoise'), async () => {
  const ok = await ask({title:'批量忽略噪声草案',
    body:'把所有建议为噪声(ignore)的草案批量确认忽略？\n其余草案仍需逐条人工判断。',
    okText:'批量确认'});
  if(!ok) return;
  const r = await api('/api/rules/approve-noise','POST', {operator:$('operator').value||'ui'});
  notice(r.message || (r.ok?'已批量忽略':'失败'), !r.ok);
  await loadRules();
}));
$('btnPatAdd').addEventListener('click', once($('btnPatAdd'), async () => {
  const pattern = $('patInput').value.trim();
  const adapter = $('patAdapter').value;
  if(!pattern){ notice('请输入前缀模式，如 zwork.foo.*', true); return; }
  const ok = await ask({title:'新增前缀忽略规则',
    body:`新增 ${adapter}: ${pattern}？\n命中的事件将全部视为背景噪声。`});
  if(!ok) return;
  const r = await api('/api/rules/pattern','POST',
    {action:'add', adapter, pattern, operator:$('operator').value||'ui'});
  notice(r.message || (r.ok?'已新增':'失败'), !r.ok);
  if(r.ok) $('patInput').value = '';
  await loadRules();
}));

/* ---------------- 日志检索 ---------------- */
let logFilesLoaded = false;
async function loadLogFiles(keepSelection){
  const r = await api('/api/logs');
  if(!r.ok){ $('logMeta').textContent = '日志清单读取失败: ' + r.message; return; }
  const sel = $('logFile');
  const prev = keepSelection ? sel.value : '';
  const files = r.data.files || [];
  sel.innerHTML = files.map(f =>
    `<option value="${esc(f.name)}">${esc(f.name)} (${(f.size/1024).toFixed(1)}KB)</option>`).join('');
  const latest = files.find(f=>f.name.startsWith('events-')) || files[0];
  if(prev && [...sel.options].some(o=>o.value===prev)) sel.value = prev;
  else if(latest) sel.value = latest.name;
  logFilesLoaded = true;
  refreshLogs();
}
async function refreshLogs(){
  if(!logFilesLoaded) return;
  const file = $('logFile').value;
  if(!file) return;
  const q = $('logQuery').value.trim();
  const tail = $('logTail').value;
  const r = await api(`/api/logs/read?file=${encodeURIComponent(file)}&q=${encodeURIComponent(q)}&tail=${tail}`);
  if(!r.ok){ $('logs').textContent = '读取失败: ' + r.message; return; }
  const d = r.data;
  $('logMeta').textContent =
    `${d.name} · 共 ${d.total_lines} 行` +
    (q ? ` · 匹配「${q}」${d.matched} 行` : '') +
    (d.truncated ? ` · 只显示末 ${d.shown} 行` : ` · 显示 ${d.shown} 行`);
  $('logs').textContent = d.lines.join('\n') || '（无匹配行）';
}
$('btnLogSearch').addEventListener('click', once($('btnLogSearch'), refreshLogs));
$('logFile').addEventListener('change', refreshLogs);
$('logTail').addEventListener('change', refreshLogs);
$('logQuery').addEventListener('keydown', e => { if(e.key === 'Enter') refreshLogs(); });

/* ---------------- 诊断与文档 ---------------- */
$('btnDiscover').addEventListener('click', once($('btnDiscover'), async () => {
  const r = await api('/api/discover');
  $('side').textContent = r.ok ? r.data.text : ('发现失败: ' + r.message);
}));
$('btnPrivacy').addEventListener('click', once($('btnPrivacy'), async () => {
  const r = await api('/api/privacy');
  if(!r.ok){ $('side').textContent = '隐私说明读取失败: ' + r.message; return; }
  const p = r.data.purge_preview || {};
  $('side').textContent = r.data.text +
    `\n\n【可归档生成物】 ${p.file_count} 个文件（${(p.dirs||[]).join(', ')}）` +
    `\n规则表、配置文件、源码与 data/ 均不清空；归档而非删除，可找回。` +
    (p.monitor_running ? '\n[注意] 当前有监控运行中，归档清空会被拒绝。' : '');
  notice('已显示隐私与数据边界说明');
}));
$('btnCases').addEventListener('click', once($('btnCases'), async () => {
  const r = await api('/api/cases');
  if(!r.ok){ $('side').textContent = '用例集读取失败: ' + r.message; return; }
  $('side').textContent = r.data.text;
  notice(`已载入 ${r.data.count} 条验收用例`);
}));
$('btnMetrics').addEventListener('click', once($('btnMetrics'), async () => {
  const r = await api('/api/metrics');
  if(!r.ok){ $('side').textContent = '指标读取失败: ' + r.message; return; }
  $('side').textContent = r.data.text;
  notice('已显示产品指标（近 7 天）');
}));
$('btnPurge').addEventListener('click', once($('btnPurge'), async () => {
  const w = await ask({title:'归档清空生成物', danger:true, requireText:'PURGE',
    body:'生成物会被移入 archive-purge-* 目录（不删除，可找回）。\n配置文件、规则表、源码与 data/ 均不动。\n请输入大写 PURGE 确认。',
    okText:'确认归档'});
  if(w !== 'PURGE'){ notice('已取消，未改动任何文件。'); return; }
  const r = await api('/api/privacy/purge','POST',
                      {confirm:'PURGE', operator:$('operator').value||'ui'});
  notice(r.message || (r.ok ? '已归档' : '失败'), !r.ok);
  await refresh();
}));

/* ---------------- 启动 ---------------- */
try{ setView(localStorage.getItem('asg.view') || 'workbench', false); }catch(_e){ setView('workbench', false); }
refresh()
  .then(() => { if($('snapSel').options.length) return loadSnapshot(); })
  .then(() => {
    // 初始化收尾：按真实当前步骤落位，避免「首次刷新就在第 3 步、没发生跨越」
    // 导致明明在值守却停在准备清单页（用户会找不到开启监控/直接睡眠按钮）。
    // 同时把基线对齐，后续只认真正发生的 2→3 跨越。
    if(currentStep >= 3) setPage('watch');
    prevStepForPage = currentStep;
    pageLock = false;
  });
setInterval(refresh, 3000);
loadRules();
loadLogFiles();
setInterval(() => { if($('logAuto').checked) refreshLogs(); }, 3000);
setInterval(() => loadLogFiles(true), 60000);
