'use strict';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const badge = (text, color = '', attributes = '') => `<span class="badge ${color}" ${attributes}>${escapeHTML(text)}</span>`;
const statCard = (label, value) => `<div class="stat"><div><strong>${escapeHTML(value)}</strong><small>${escapeHTML(label)}</small></div></div>`;
// Presentation only: route entries animate once; polling never calls this helper.
function enterPage() {
  if(matchMedia('(prefers-reduced-motion: reduce)').matches)return;
  $$('#main > .page-heading,#main > .card,#main > .workspace-nav,#main > .workbench-hero').forEach(el=>el.classList.add('page-enter'));
}
function setButtonBusy(button,busy) {
  if(button)button.setAttribute('aria-busy',String(busy));
}
function actionErrorHost(button) {
  const host=button?.closest('form,.review-card,.grading-status,.card');
  return host?.isConnected?host:$('#main');
}
function clearActionError(button) {
  actionErrorHost(button)?.querySelector('[data-action-error]')?.remove();
}
function showActionError(button,error) {
  const host=actionErrorHost(button);if(!host)return;
  clearActionError(button);
  const message=document.createElement('p');message.className='notice error';message.dataset.actionError='';message.setAttribute('role','alert');message.textContent=error.message;
  if(host===$('#main'))host.prepend(message);else host.append(message);
}
function updateVisualNumber(el,value) {
  if(!el||el.textContent===String(value))return;
  el.textContent=value;
  if(!matchMedia('(prefers-reduced-motion: reduce)').matches)el.animate([{opacity:.5},{opacity:1}],{duration:200,easing:'ease-out'});
}
function markChangedNumbers(previous,next) {
  if(!previous||!next)return;
  const before=new Map($$('[data-visual-number]',previous).map(el=>[el.dataset.visualNumber,el.textContent]));
  $$('[data-visual-number]',next).forEach(el=>{if(before.has(el.dataset.visualNumber)&&before.get(el.dataset.visualNumber)!==el.textContent)el.classList.add('number-updated');});
}
function replaceVisualContent(el,html) {
  if(el.innerHTML===html)return;
  const tpl=document.createElement('template');tpl.innerHTML=html;
  markChangedNumbers(el,tpl.content);el.replaceChildren(tpl.content);
}
const loadingHTML = message => `<div class="loading" role="status" aria-live="polite"><p>${escapeHTML(message)}</p><div class="skeleton" aria-hidden="true"><span class="skeleton-line skeleton-title"></span><span class="skeleton-line"></span><span class="skeleton-line"></span><span class="skeleton-line"></span></div></div>`;
function uploadZone({id,multiple=false,required=false,label='选择学生答卷照片',note=''}) {
  return `<label class="upload-zone"><input id="${id}" class="upload-input" type="file" accept=".jpg,.jpeg,.png" ${multiple?'multiple':''} ${required?'required':''} aria-label="${escapeHTML(label)}"><span class="upload-icon" aria-hidden="true">↑</span><strong>点击选择照片</strong><span class="upload-note">JPG / PNG · 每张不超过 10 MB${multiple?' · 可选择多张':''}</span><span class="upload-files" data-files-for="${id}" role="status" aria-live="polite">尚未选择照片</span>${note?`<span class="upload-note">${escapeHTML(note)}</span>`:''}</label>`;
}
function updateUploadFiles(input) {
  const label=$(`[data-files-for="${input.id}"]`);if(!label)return;
  label.textContent=input.files.length?`${input.files.length} 张照片：${[...input.files].map(file=>file.name).join('、')}`:'尚未选择照片';
}
const kindNames = {word:'单词', phrase:'短语', sentence:'句子',grammar:'语法填空'};
const state = {submission:null,pageId:null,activeQuestion:null,selected:new Set(),drafts:new Map(),reviewSave:null,filter:'all',reviewOrder:'priority',zoom:100,draw:false,busy:false,newTaskDirty:false,ocrProgress:null,routeVersion:0,pageUpload:null};
let toastTimer, gradingPollTimer;
const photoRotations = new Map();
let attentionQueue = null, attentionContext = null, attentionLoadError = '';
let lastReviewUndo = null;
try { lastReviewUndo=JSON.parse(sessionStorage.getItem('review-undo') || 'null'); } catch {}

function renderReviewUndo() {
  const panel=$('#review-undo');if(!panel)return;
  panel.hidden=!lastReviewUndo;
  panel.innerHTML=lastReviewUndo ? `<span>${escapeHTML(lastReviewUndo.name)} · 第 ${Number(lastReviewUndo.number)} 题已保存</span><button class="btn small" data-action="undo-review">撤销上一步</button><button class="text-button" data-action="dismiss-undo" aria-label="关闭撤销提示">关闭</button>` : '';
  updateReviewLock();
}
function setReviewUndo(value) {
  lastReviewUndo=value;
  // Browser persistence is optional; saved grades and undo events live on the server.
  try { if(value)sessionStorage.setItem('review-undo',JSON.stringify(value));else sessionStorage.removeItem('review-undo'); } catch {}
  renderReviewUndo();
}
renderReviewUndo();

function answerLayoutField(id) {
  return `<label class="field">照片作答形式<select id="${id}"><option value="inline">原文作答 / 普通作业本</option><option value="separate">独立答题卡（按题号作答）</option></select><span class="muted tiny">只作用于本次上传；独立答题卡请保留题号、学号和姓名。</span></label>`;
}

const recognitionViews = {all:'全部',attention:'待核对',uncertain:'识别不确定',failed:'批改失败',processing:'处理中',ungraded:'待批改',not_uploaded:'未交',completed:'已计分且无异常'};
const recognitionRank = {failed:0,uncertain:1,processing:2,ungraded:3,completed:4,not_uploaded:5,exempt:6,unknown:0};
let recognitionList = null, listPollTimer;
function recognitionFor(row) {
  if (row.exempt || row.identity?.exempt) return {state:'exempt',uncertain_count:0,needs_attention:false};
  if (row.recognition) return row.recognition;
  // Older responses remain usable during reload; missing state is never treated as success.
  const state = row.grading_state === 'running' ? 'processing' : row.grading_state === 'failed' || row.has_ocr_error ? 'failed' : row.uncertain_count ? 'uncertain' : row.is_final || row.status === 'confirmed' ? 'completed' : row.state === 'not_submitted' || row.pages?.length === 0 ? 'not_uploaded' : row.state || row.grading_state ? 'ungraded' : 'unknown';
  return {state,uncertain_count:state==='processing'?0:(row.uncertain_count || 0),needs_attention:['failed','uncertain','ungraded'].includes(state),failed:state==='failed'};
}
function listReturnPath() {
  return Nav.safePath(new URLSearchParams(location.search).get('from'));
}
function recognitionHref(row,scope,questionOnly=false) {
  const r=recognitionFor(row),sid=row.submission_id || row.id;
  const from=listReturnPath() || (/^\/(tasks|assignments)\//.test(location.pathname)?location.pathname+location.search:`/tasks/${row.task_id||state.submission?.task_id}`);
  const params=new URLSearchParams({from});
  if(scope==='task'||new URLSearchParams(location.search).get('tab')==='attention'){params.set('attention','1');params.set('queue',scope);}
  if (r.first_question_id && (!r.failed||questionOnly)) params.set('question',r.first_question_id);
  if (r.uncertain_count && (!r.failed||questionOnly)) params.set('filter','uncertain');
  return `/submissions/${sid}?${params}`;
}
function recognitionHTML(row,scope='task') {
  const r=recognitionFor(row);
  const labels={failed:['批改失败，可重试','red'],uncertain:[`${row.is_final || row.status==='confirmed'?'已计分 · ':''}识别不确定 ${r.uncertain_count} 题`,'amber'],processing:['处理中','blue'],ungraded:['待批改','amber'],completed:['已计分 · 未发现识别异常','green'],not_uploaded:['未交',''],exempt:['免交',''],unknown:['状态暂时无法获取','amber']};
  const [label,color]=labels[r.state] || labels.unknown;
  const stateBadge=badge(label,color);
  const extra=r.failed && r.uncertain_count ? `<a href="${escapeHTML(recognitionHref(row,scope,true))}" data-nav class="badge amber">识别不确定 ${r.uncertain_count} 题</a>` : '';
  return `<div class="recognition-status">${r.state==='uncertain'?`<a href="${escapeHTML(recognitionHref(row,scope))}" data-nav>${stateBadge}</a>`:stateBadge}${extra}</div>`;
}
function recognitionAction(row,scope) {
  const r=recognitionFor(row),label=r.failed?'查看失败原因':r.uncertain_count?'核对不确定题':r.state==='completed'?'查看成绩':r.state==='processing'?'查看进度':'继续批改';
  return `<a class="btn small ${r.needs_attention?'primary':''}" href="${escapeHTML(recognitionHref(row,scope))}" data-nav>${label} →</a>`;
}
function recognitionControls(scope,rows) {
  const active=rows.filter(r=>recognitionFor(r).state!=='exempt'),attention=active.filter(r=>recognitionFor(r).needs_attention).length;
  const uncertain=active.filter(r=>recognitionFor(r).uncertain_count).length,total=active.reduce((n,r)=>n+recognitionFor(r).uncertain_count,0);
  const completed=active.filter(r=>(r.is_final || r.status==='confirmed')&&recognitionFor(r).state!=='not_uploaded').length,failed=active.filter(r=>recognitionFor(r).failed).length;
  return `<div class="recognition-overview" role="status" aria-live="polite"><strong>已计分 ${completed} 份</strong><span class="${uncertain?'status-warning':''}">${uncertain} 份存在识别不确定，共 ${total} 题</span><span class="${failed?'status-danger':''}">批改失败 ${failed} 份</span><button class="btn small ${attention?'primary':''}" data-action="filter-attention">只看待核对（${attention} 份）</button></div><div class="recognition-toolbar"><label class="field recognition-search">搜索学生<input id="${scope==='assignment'?'class-assignment-search':'task-recognition-search'}" data-recognition-search="${scope}" type="search" maxlength="120" placeholder="${scope==='task'?'姓名、学号或班级':'姓名或学号'}，空格组合查找"></label><label class="field">识别状态<select id="${scope}-recognition-filter">${Object.entries(recognitionViews).map(([k,v])=>`<option value="${k}">${v}</option>`).join('')}</select></label><label class="field">排序<select id="${scope}-recognition-order"><option value="priority">异常优先</option><option value="number">按学号查看</option></select></label><button class="btn small" data-action="clear-recognition-search" hidden>清空搜索</button></div><p id="${scope}-recognition-count" class="muted tiny" role="status" aria-live="polite" aria-atomic="true"></p>`;
}
function applyRecognitionList() {
  if (!recognitionList) return;
  const {scope,rows}=recognitionList,params=new URLSearchParams(location.search);
  const filter=$(`#${scope}-recognition-filter`),order=$(`#${scope}-recognition-order`);
  if (!filter || !order) return;
  const prefix=scope==='assignment'?'g.':'';
  const view=params.get(prefix+'view') || 'all';filter.value=Object.hasOwn(recognitionViews,view)?view:'all';order.value=params.get(prefix+'order')==='number'?'number':'priority';
  const query=params.get(prefix+'q') || '',input=$(`[data-recognition-search="${scope}"]`);
  if(input && input.value!==query)input.value=query;
  const words=query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  const clear=$('[data-action="clear-recognition-search"]');if(clear)clear.hidden=!query;
  const ordered=[...rows].sort((a,b)=> (order.value==='priority'?(recognitionRank[recognitionFor(a).state]??0)-(recognitionRank[recognitionFor(b).state]??0):0) || String(a.student_code).localeCompare(String(b.student_code),'zh-CN',{numeric:true}) || a.id-b.id);
  let visible=0;
  for (const row of ordered) {
    const el=document.querySelector(`[data-recognition-id="${row.id}"]`);if (!el) continue;
    const r=recognitionFor(row),selected=filter.value;
    $$('a[href^="/submissions/"]',el).forEach(link=>{
      const target=new URL(link.href);target.searchParams.set('from',location.pathname+location.search);
      link.href=target.pathname+'?'+target.searchParams;
    });
    const matches=selected==='all' || selected==='attention'&&r.needs_attention || selected==='uncertain'&&r.uncertain_count>0 || selected===r.state;
    const text=[row.student_code,row.name,row.identity?.name,row.class_name||row.identity?.class_name,scope==='task'&&!row.identity?'临时答卷':''].filter(Boolean).join(' ').toLocaleLowerCase();
    el.hidden=!matches || !words.every(word=>text.includes(word));
    el.classList.toggle('has-recognition-issue',r.needs_attention);el.parentElement.append(el);if (!el.hidden) visible++;
  }
  const empty=$(`#${scope}-recognition-empty`);if(empty)empty.hidden=visible>0;
  const count=$(`#${scope}-recognition-count`);if(count)count.textContent=`显示 ${visible} / ${rows.length} ${scope==='assignment'?'名学生':'份答卷'}`;
  $$('[data-action="filter-recognition"]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.view===filter.value)));
}
function mountRecognitionList(scope,rows) {
  recognitionList={scope,rows};
  $(`#${scope}-recognition-empty`)?.insertAdjacentHTML('beforeend',' <button class="btn small" data-action="reset-recognition-list">显示全部</button>');
  $$('[data-recognition-id]').forEach(row=>{const labels=$$('thead th',row.closest('table')).map(th=>th.textContent);$$('td',row).forEach((td,i)=>td.dataset.label=labels[i]||'');});
  applyRecognitionList();
}
function updateRecognitionView(scope,key,value) {
  const params=new URLSearchParams(location.search),name=(scope==='assignment'?'g.':'')+key;if(value)params.set(name,value);else params.delete(name);Nav.patchURL(params);applyRecognitionList();
}
function hasListInteraction() {
  return state.busy || hasDirty() || !!$('#class-dialog') || $$('input[type="file"]').some(input=>input.files?.length>0) || document.activeElement?.matches('input,select,textarea');
}
function pollTaskList(tid,version) {
  clearTimeout(listPollTimer);
  listPollTimer=setTimeout(async()=>{
    if(version!==state.routeVersion)return;
    if(hasListInteraction()) {pollTaskList(tid,version);return;}
    try {
      const data=await api(`/api/tasks/${tid}`);if(version!==state.routeVersion)return;
      if(hasListInteraction()) {
        const panel=$('#task-status-message');
        if(panel && JSON.stringify(data)!==JSON.stringify(recognitionList?.task))panel.textContent='批改进度有更新，结束当前操作后自动显示。';
        pollTaskList(tid,version);return;
      }
      if(JSON.stringify(data)!==JSON.stringify(recognitionList?.task))renderTask(data,true);
      else {const panel=$('#task-status-message');if(panel)panel.textContent='';pollTaskList(tid,version);}
    }catch(error){if(version!==state.routeVersion)return;const panel=$('#task-status-message');if(panel)panel.textContent='状态暂时无法获取，正在重新读取。';pollTaskList(tid,version);}
  },2500);
}

function toast(message, error = false) {
  const el = $('#toast');
  el.textContent = message;
  el.className = error ? 'error' : '';
  el.style.display = 'block';
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.style.display = 'none'; }, error ? 8000 : 4000);
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.method && options.method !== 'GET') headers.set('X-CSRF-Token', $('meta[name="csrf-token"]').content);
  if (options.body && !(options.body instanceof FormData)) {
    headers.set('Content-Type', 'application/json');
    options.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, {...options, headers});
  let data;
  try { data = await response.json(); } catch { throw new Error('服务器未返回有效数据，请检查服务后重试。'); }
  if (!response.ok) {const error=new Error(data.error || `请求失败（${response.status}）`);error.status=response.status;throw error;}
  return data;
}

function hasDirty() { return state.newTaskDirty || state.drafts.size > 0; }
async function navigate(path, replace = false) {
  return Nav.go(path,replace);
}
window.addEventListener('beforeunload', event => { if (hasDirty() || state.busy || Nav.pendingFiles()) { event.preventDefault(); event.returnValue = ''; } });
let lastPath = location.pathname;

function heading(title, description, action = '') {
  return `<div class="page-heading"><div><h1>${escapeHTML(title)}</h1><p class="subtitle">${escapeHTML(description)}</p></div>${action}</div>`;
}
function empty(title, text, action = '') {
  return `<div class="empty"><div class="empty-symbol" aria-hidden="true">▤</div><h3>${escapeHTML(title)}</h3><p>${escapeHTML(text)}</p>${action}</div>`;
}
function dateText(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? escapeHTML(value) : date.toLocaleDateString('zh-CN', {year:'numeric',month:'2-digit',day:'2-digit'});
}
function numberText(value) { return Number(value || 0).toLocaleString('zh-CN', {maximumFractionDigits:2}); }

async function route() {
  $('#main').classList.remove('submission-page');
  clearTimeout(gradingPollTimer);clearTimeout(listPollTimer);recognitionList=null;
  state.pageUpload = null;
  state.reviewSave = null;
  attentionQueue = null;attentionContext=null;attentionLoadError='';photoRotations.clear();
  const version = ++state.routeVersion;
  const path = location.pathname;
  const navMore=$('.nav-more');if(navMore)navMore.open=path==='/settings' && !matchMedia('(max-width: 680px)').matches;
  lastPath = path+location.search;
  $('#main').innerHTML = loadingHTML('正在加载…');
  try {
    $$('.nav-item[data-nav]').forEach(link => {
      const fromList=path==='/'&&Workbench.legacyList();
      const active=link.pathname==='/settings'?path==='/settings':
        link.pathname==='/classes'?/^\/(classes|students)(?:\/|$)/.test(path):
        link.pathname==='/assignments'?fromList||/^\/(assignments|tasks|submissions)(?:\/|$)/.test(path):
        path==='/'&&!fromList;
      link.classList.toggle('active',active);if(active)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current');
    });
    if(path==='/assignments') { await Workbench.render(); return; }
    const workspaceMatch=path.match(/^\/assignments\/(\d+)(\/analysis)?\/?$/);
    if(workspaceMatch){if(workspaceMatch[2])await Analysis.render(Number(workspaceMatch[1]));else await Workspace.route(Number(workspaceMatch[1]));return;}
    if (await routeClasses(path)) return;
    if (path === '/settings') { await renderSettings(); return; }
    if (path === '/tasks/new') { renderNewTask(); await Workbench.mountCreation(); return; }
    const taskMatch = path.match(/^\/tasks\/(\d+)\/?$/);
    const submissionMatch = path.match(/^\/submissions\/(\d+)\/?$/);
    if (submissionMatch) {
      let data = await api(`/api/submissions/${submissionMatch[1]}`);
      if (version !== state.routeVersion) return;
      state.filter = 'all';
      state.submission = data; state.pageId = data.pages[0]?.id ?? null; state.activeQuestion = data.is_final || data.status === 'confirmed' ? null : nextPriorityQuestion(data.reviews)?.question_id ?? data.reviews[0]?.question_id ?? null;
      state.selected.clear(); state.zoom = 100; state.draw = false;
      const params = new URLSearchParams(location.search), questionId = Number(params.get('question'));
      if (params.has('attention')) {
        const scope=params.get('queue')==='task' || !data.identity?.assignment_id ? 'task':'assignment';
        const id=scope==='task'?data.task_id:data.identity.assignment_id;
        attentionContext={scope,id,endpoint:`/api/${scope==='task'?'tasks':'assignments'}/${id}/attention`,returnPath:Nav.returnInfo().path};
        await refreshAttentionQueue();
        if(version!==state.routeVersion)return;
      }
      if(params.get('filter')==='uncertain') {
        state.filter='uncertain';state.activeQuestion=data.reviews.find(q=>q.uncertain)?.question_id??null;
      }
      if (data.reviews.some(q => q.question_id === questionId)) state.activeQuestion = questionId;
      const region = data.reviews.find(q => q.question_id === state.activeQuestion)?.regions?.[0];
      if (region) state.pageId = region.page_id;
      renderSubmission();
      if (isGrading(data)) pollGrading(data.id,version);
    } else if (taskMatch) {
      const data = await api(`/api/tasks/${taskMatch[1]}`);
      if (version !== state.routeVersion) return;
      const legacy=new URLSearchParams(location.search);
      if(['attention','uncertain'].includes(legacy.get('view'))&&data.assignments?.length===1&&!data.submissions.some(s=>!s.identity)){
        const p=new URLSearchParams({tab:'attention'});if(legacy.get('view')==='uncertain')p.set('a.kind','question');if(legacy.get('q'))p.set('a.q',legacy.get('q'));await navigate(`/assignments/${data.assignments[0].id}?${p}`,true);return;
      }
      renderTask(data);
      await mountTaskClasses(data);
      if (version === state.routeVersion) {
        const params=new URLSearchParams(location.search);
        if(params.has('view') || params.has('q'))(data.pending_identity_photos && params.get('view')==='attention' && !params.get('q') ? $('#task-pending-photos') : $('#task-submissions'))?.scrollIntoView({block:'start',behavior:'instant'});
      }
    } else if(path==='/tasks') {
      const data = await api('/api/tasks');
      if (version !== state.routeVersion) return;
      renderDashboard(data.tasks);
    } else await Workbench.render();
  } catch (error) {
    if (version !== state.routeVersion) return;
    $('#main').innerHTML = heading('暂时无法打开', '请确认本地服务正常运行。') + `<div class="notice error">${escapeHTML(error.message)}</div><button class="btn" data-action="reload">重新加载</button>`;
  } finally {
    if(version===state.routeVersion)enterPage();
  }
}

async function renderSettings() {
  $('#breadcrumb').textContent = '工作台 / 识别设置';
  const version = state.routeVersion;
  const data = await api('/api/settings');
  if (version !== state.routeVersion || location.pathname !== '/settings') return;
  $('#main').innerHTML = heading('识别设置', '选择本机识别、PaddleOCR 云端或 OpenAI 兼容图片接口。', '<a class="btn" href="/" data-nav>返回工作台</a>') +
    `<form id="settings-form" class="card"><div class="card-header"><div><h2>识别方式</h2><p>设置同时作用于照片中的学生身份与英文作答识别。云端速度取决于服务与网络。</p></div></div><div class="card-content form-grid"><label class="field">识别方式<select name="provider"><option value="local" ${data.provider === 'local' ? 'selected' : ''}>本机 PaddleOCR</option><option value="paddle_cloud" ${data.provider === 'paddle_cloud' ? 'selected' : ''}>PaddleOCR 云端（Jobs API）</option><option value="openai_compatible" ${data.provider === 'openai_compatible' ? 'selected' : ''}>OpenAI 兼容图片接口</option></select></label><p id="settings-local-note" class="notice">本机处理照片，不向外部服务上传；首次加载模型和 CPU 识别可能较慢。</p><div id="settings-openai-fields" class="form-grid"><label class="field">API 地址<input name="base_url" value="${escapeHTML(data.base_url || '')}" placeholder="https://api.example.com/v1"></label><label class="field">模型名<input name="model" value="${escapeHTML(data.model || '')}" placeholder="支持图片的模型名"></label><label class="field">OpenAI 兼容接口 API Key <span id="settings-key-status" class="muted tiny">${data.api_key_configured ? '已配置；留空保持不变' : '尚未配置'}</span><input name="api_key" type="password" autocomplete="new-password" placeholder="留空保留已配置密钥"></label><label class="inline-check"><input name="clear_key" type="checkbox">清除 OpenAI 接口 Key（请同时切到其他方式）</label></div><div id="settings-paddle-fields" class="form-grid"><label class="field">PaddleOCR Jobs 接口地址<input name="paddle_url" value="${escapeHTML(data.paddle_url || '')}" placeholder="https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"></label><label class="field">PaddleOCR 模型<input name="paddle_model" value="${escapeHTML(data.paddle_model || 'PaddleOCR-VL-1.6')}"></label><label class="field">PaddleOCR Token <span id="settings-paddle-status" class="muted tiny">${data.paddle_token_configured ? '已配置；留空保持不变' : '尚未配置'}</span><input name="paddle_token" type="password" autocomplete="new-password" placeholder="填入 AI Studio 提供的 Token"></label><label class="inline-check"><input name="clear_paddle_token" type="checkbox">清除 PaddleOCR Token（请同时切到其他方式）</label><div class="notice">PaddleOCR 将提交图片任务、等待云端识别并下载文字结果，最长等待约 3 分钟，请保持页面打开。版面区域需要老师核对。</div></div><p class="tiny muted">凭据分开保存在本机数据库，页面不会回显。留空保留原凭据；测试连接会保存设置并发送一张系统样例图，服务商可能计费。</p><div class="button-row"><button class="btn primary" type="submit">保存设置</button><button class="btn" type="button" data-settings-action="test">保存并测试图片连接</button></div><div id="settings-message" role="status" aria-live="polite"></div><div class="notice warn" id="settings-cloud-note">云端模式会把照片发送到所填写的服务。自动批改会直接生成最终成绩，教师可对照原图修改并保存改判。</div></div></form>`;
  updateSettingsProvider();
  const review = await api('/api/settings/ai-review');
  if (version !== state.routeVersion || location.pathname !== '/settings') return;
  $('#main').insertAdjacentHTML('beforeend', `<form id="ai-review-settings" class="card"><div class="card-content form-grid"><h2>AI 自动复核</h2><p>OCR 后自动复核能可靠定位的不确定短答，老师只处理剩余异常。使用支持图片的 OpenAI 兼容接口，独立于主 OCR 配置。</p><label class="inline-check"><input type="checkbox" name="enabled" ${review.enabled?'checked':''}>启用后台自动复核</label><label class="field">API 地址<input name="base_url" type="url" value="${escapeHTML(review.base_url)}" placeholder="https://api.example.com/v1"></label><label class="field">图片模型名<input name="model" value="${escapeHTML(review.model)}"></label><label class="field">API Key（${review.api_key_configured?'已配置，留空保留':'尚未配置'}）<input name="api_key" type="password" autocomplete="new-password"></label><label class="inline-check"><input name="clear_key" type="checkbox">清除已保存的 Key</label><label class="field">每次任务最多复核题数<input name="limit" type="number" min="1" max="100" value="${review.limit}"></label><p class="notice">启用后会将单题裁图发送至此服务，可能产生费用。无法定位、看不清或识别仍冲突的题保留给老师；已确认答案不会自动覆盖。</p><div class="button-row"><button class="btn primary" type="submit">保存 AI 设置</button><button class="btn" type="submit" name="test" value="1">保存并测试图片接口（可能计费）</button></div><p data-ai-message role="status" aria-live="polite"></p></div></form>`);
}

document.addEventListener('submit', async event => {
  if(event.target.id !== 'ai-review-settings') return;
  event.preventDefault();
  const form=event.target, fields=form.elements, button=event.submitter;
  await busyAction(button, async () => {
    await api('/api/settings/ai-review', {method:'PATCH',body:{enabled:fields.enabled.checked,
      limit:Number(fields.limit.value),base_url:fields.base_url.value,model:fields.model.value,
      api_key:fields.api_key.value,clear_key:fields.clear_key.checked}});
    fields.api_key.value='';fields.clear_key.checked=false;
    let message='AI 设置已保存。';
    if(button?.name==='test') message=(await api('/api/settings/ai-review/test',{method:'POST',body:{}})).message;
    $('[data-ai-message]',form).textContent=message;
  });
});
function updateSettingsProvider() {
  const provider = $('#settings-form')?.elements.provider.value;
  if (!provider) return;
  $('#settings-openai-fields').hidden = provider !== 'openai_compatible';
  $('#settings-paddle-fields').hidden = provider !== 'paddle_cloud';
  $('#settings-local-note').hidden = provider !== 'local';
  $('#settings-cloud-note').hidden = provider === 'local';
}
document.addEventListener('change',event => {
  if (event.target.matches('#settings-form [name=provider]')) updateSettingsProvider();
});
async function saveSettings(form) {
  const body = {provider:form.elements.provider.value,
    base_url:form.elements.base_url.value.trim(),model:form.elements.model.value.trim(),
    paddle_url:form.elements.paddle_url.value.trim(),paddle_model:form.elements.paddle_model.value.trim()};
  for (const [key,clear] of [['api_key','clear_key'],['paddle_token','clear_paddle_token']]) {
    if (form.elements[clear].checked) body[key] = '';
    else if (form.elements[key].value.trim()) body[key] = form.elements[key].value.trim();
  }
  const saved = await api('/api/settings',{method:'PATCH',body});
  form.elements.api_key.value = ''; form.elements.clear_key.checked = false;
  form.elements.paddle_token.value = ''; form.elements.clear_paddle_token.checked = false;
  $('#settings-key-status').textContent = saved.api_key_configured ? '已配置；留空保持不变' : '尚未配置';
  $('#settings-paddle-status').textContent = saved.paddle_token_configured ? '已配置；留空保持不变' : '尚未配置';
  state.newTaskDirty = false;
  return saved;
}
document.addEventListener('submit', async event => {
  if (event.target.id !== 'settings-form') return;
  event.preventDefault();
  const form = event.target;
  await busyAction($('button[type=submit]',form),async () => {
    await saveSettings(form);
    $('#settings-message').textContent = '设置已保存，之后的身份与作答识别均使用所选方式。';
    toast('识别设置已保存。');
  });
});
document.addEventListener('click', async event => {
  const button = event.target.closest('[data-settings-action="test"]');
  if (!button) return;
  await busyAction(button,async () => {
    const saved = await saveSettings($('#settings-form'));
    $('#settings-message').textContent = saved.provider === 'paddle_cloud' ? '正在提交 PaddleOCR 样例任务并等待识别结果，最长约 3 分钟，请保持页面打开…' : '正在测试图片接口，请稍候…';
    try {
      const result = await api('/api/settings/test',{method:'POST',body:{}});
      $('#settings-message').textContent = result.message;
    } catch (error) { $('#settings-message').textContent = error.message; throw error; }
  });
});
for (const eventName of ['input','change']) document.addEventListener(eventName,event => {
  if (event.target.closest('#settings-form')) state.newTaskDirty = true;
});

function pollDashboard(previousTasks,version) {
  clearTimeout(listPollTimer);
  listPollTimer=setTimeout(async()=>{
    if(version!==state.routeVersion)return;
    if(document.hidden || hasListInteraction()) {pollDashboard(previousTasks,version);return;}
    try {
      const data=await api('/api/tasks');if(version!==state.routeVersion)return;
      const changed=JSON.stringify(data.tasks)!==JSON.stringify(previousTasks),panel=$('#dashboard-status');
      if(hasListInteraction() || $('#main a:focus-visible,#main button:focus-visible')) {
        if(changed && panel){panel.hidden=false;panel.textContent='进度有更新，结束当前操作后自动显示。';}
        pollDashboard(previousTasks,version);return;
      }
      if(changed)renderDashboard(data.tasks);
      else {if(panel){panel.hidden=true;panel.textContent='';}pollDashboard(previousTasks,version);}
    }catch(error){
      if(version!==state.routeVersion)return;
      const panel=$('#dashboard-status');if(panel){panel.hidden=false;panel.textContent='状态暂时无法获取，保留上次统计，正在重新读取。';}
      $$('[data-dashboard-recognition]').forEach(cell=>{
        if(!$('[data-dashboard-unavailable]',cell))cell.insertAdjacentHTML('afterbegin','<span class="badge amber" data-dashboard-unavailable>状态暂时无法获取</span><small class="muted">上次读取：</small>');
        $$('.badge.green',cell).forEach(badge=>{badge.classList.remove('green');badge.classList.add('amber');});
      });
      pollDashboard(null,version);
    }
  },2500);
}
function renderDashboard(tasks) {
  $('#breadcrumb').textContent='工作台 / 标准答案与布置';
  const sum=key=>tasks.reduce((n,t)=>n+Number(t[key]||0),0);
  const action='<a class="btn primary" href="/tasks/new" data-nav>＋ 新建默写任务</a>';
  $('#main').innerHTML=heading('标准答案与布置','这里管理标准答案与临时答卷；日常批改请从工作台选择班级作业。',action)+
    `<p id="dashboard-status" class="notice" role="status" aria-live="polite" hidden></p><div class="stats recognition-stats">${[['默写任务',tasks.length],['已上传答卷',sum('uploaded_count')],['待核对答卷',sum('attention_count')],['待确认归属照片',sum('pending_identity_photos')],['识别不确定题',sum('uncertain_questions')],['批改失败答卷',sum('failed_count')]].map(([label,n])=>statCard(label,n)).join('')}</div>`+
    `<section class="card"><div class="card-header"><div><h2>我的默写任务</h2><p>已计分仍可能存在识别不确定，老师可按需核对。</p></div></div>${tasks.length?`<div class="table-wrap"><table><thead><tr><th>任务名称</th><th>题目 / 总分</th><th>学生答卷</th><th>批改状态与识别情况</th><th>创建日期</th><th>操作</th></tr></thead><tbody>${tasks.map(t=>`<tr><td><a href="/tasks/${t.id}" data-nav><strong>${escapeHTML(t.name)}</strong></a></td><td>${t.question_count} 题 / ${numberText(t.total_points)} 分</td><td>${t.uploaded_count??t.submission_count} 份</td><td data-dashboard-recognition>${t.attention_count==null?'<span class="badge amber">状态暂时无法获取</span>':`<div class="recognition-status"><span class="badge ${(t.attention_count||t.pending_identity_photos)?'amber':t.processing_count?'blue':t.completed_count?'green':''}">${t.processing_count?`处理中 ${t.processing_count} 份 · `:''}已计分 ${t.completed_count} 份</span>${t.background_jobs?'<span class="badge blue">后台处理中</span>':''}${t.uncertain_questions?`<a class="badge amber" href="/tasks/${t.id}?view=uncertain" data-nav>${t.uncertain_submissions} 份存在识别不确定，共 ${t.uncertain_questions} 题</a>`:''}${t.pending_identity_photos?`<a class="badge amber" href="/tasks/${t.id}?view=attention" data-nav>待确认归属 ${t.pending_identity_photos} 张照片</a>`:''}${t.failed_count?`<span class="badge red">批改失败 ${t.failed_count} 份</span>`:''}${!t.attention_count&&!t.pending_identity_photos&&!t.processing_count&&!t.background_jobs&&t.completed_count?'<span class="muted tiny">未发现识别异常，可按需抽查</span>':''}${!t.uploaded_count?'<span class="muted tiny">等待答卷</span>':''}</div>`}</td><td>${dateText(t.created_at)}</td><td><a class="btn small ${(t.attention_count||t.pending_identity_photos)?'primary':''}" href="/tasks/${t.id}${(t.attention_count||t.pending_identity_photos)?'?view=attention':''}" data-nav>${(t.attention_count||t.pending_identity_photos)?'查看待核对项':t.completed_count?'查看成绩':'继续批改'} →</a> <button class="btn small danger" data-action="delete-task" data-id="${t.id}" aria-label="删除任务：${escapeHTML(t.name)}">删除</button></td></tr>`).join('')}</tbody></table></div>`:empty('还没有默写任务','添加标准答案，上传答卷，开始第一次批改。',action)}</section>`;
  if(tasks.some(t=>t.processing_count||t.background_jobs))pollDashboard(tasks,state.routeVersion);
}

function answerRow(index, answer = '', question = {}) {
  const kind = question.kind || 'word';
  return `<tr><td><input class="number-input" aria-label="题号" type="number" min="1" max="999" value="${question.number || index}" required style="width:65px"></td><td><select aria-label="第 ${index} 题类型">${Object.entries(kindNames).map(([value,label])=>`<option value="${value}" ${value===kind?'selected':''}>${label}</option>`).join('')}</select></td><td><input class="answer-input" aria-label="第 ${index} 题标准答案" value="${escapeHTML(answer)}" placeholder="例如 apple" required maxlength="4000"><details><summary>上下文 / 提示词（选填）</summary><label class="field">题目上下文<textarea class="context-input" maxlength="10000" rows="3">${escapeHTML(question.context || '')}</textarea></label><label class="field">提示词<input class="hint-input" maxlength="200" value="${escapeHTML(question.hint || '')}"></label></details></td><td><input class="variant-input" aria-label="第 ${index} 题可接受写法" placeholder="多个写法用 | 分隔" maxlength="5000"></td><td><input class="points-input" aria-label="第 ${index} 题分值" type="number" min="0.01" max="1000" step="0.01" value="${question.points ?? 1}" required></td><td><button type="button" class="icon-button" data-action="remove-row" aria-label="删除第 ${index} 题">×</button></td></tr>`;
}
function contextHTML(q) {
  return `${q.context ? `<div class="question-context"><span class="muted tiny">题目上下文</span><p>${escapeHTML(q.context)}</p></div>` : ''}${q.hint ? `<p class="tiny">提示词：<strong>${escapeHTML(q.hint)}</strong></p>` : ''}`;
}
function selectedWord(id) {
  const file = $(id).files[0];
  if (!file || !/\.docx$/i.test(file.name) || file.size > 5*1024*1024) throw new Error('请选择不超过 5MB 的 Word（.docx）文件。');
  return file;
}
async function previewWord() {
  const file = selectedWord('#word-file');
  const hasAnswers = $$('#answer-rows .answer-input').some(input=>input.value.trim());
  if (hasAnswers && !confirm('Word 预览将替换当前答案表及原文。尚未保存的内容会丢失，确定继续？')) return;
  const body = new FormData(); body.append('file',file);
  const draft = await api('/api/tasks/import-word',{method:'POST',body});
  $('#answer-rows').innerHTML = draft.questions.map(q=>answerRow(q.number,q.answer,q)).join('');
  $('#new-task-form').elements.task_name.value = draft.name;
  $('#task-passage').value = draft.passage;
  $('#task-instructions').value = draft.instructions;
  $('#word-source-name').value = draft.source_name;
  $('#word-warnings').innerHTML = `<div class="notice warn">${draft.warnings.map(escapeHTML).join('<br>')}</div>`;
  $('#word-confirmation').hidden = false;
  $('#word-confirmed').checked = false; $('#word-confirmed').required = true;
  state.newTaskDirty = true;
  updateEditorTotal();
  toast(`已提取 ${draft.questions.length} 道候选题，请核对后创建。`);
}
async function previewAnswerWord() {
  const file = selectedWord('#answer-word-file');
  const body = new FormData();
  body.append('file', file);
  body.append('mode', 'answers');
  const draft = await api('/api/tasks/import-word', {method:'POST', body});
  const preview = $('#answer-word-preview');
  preview.dataset.name = draft.name;
  preview.dataset.source = draft.source_name;
  preview.innerHTML = `<p>请勾选要填入草稿的题目。答案可在这里修改，分值在草稿中填写。</p>
    ${draft.warnings.length ? `<div class="notice warn" role="status">${draft.warnings.map(escapeHTML).join('<br>')}</div>` : ''}
    ${draft.groups.map((group, index) => `<fieldset class="class-choice-list" data-answer-word-group style="max-height:none;min-inline-size:0">
      <legend>${escapeHTML(group.label)} · ${group.items.length} 题</legend>
      ${group.kind === 'summary' ? `<p>概要写作目前可预览。</p>${group.items.flatMap(item => item.versions.map(version =>
        `<label class="field">${escapeHTML(version.label)}<textarea rows="4" readonly>${escapeHTML(version.answer)}</textarea></label>`)).join('')}`
        : `<label class="inline-check"><input type="checkbox" data-answer-word-all>选择本组全部题目</label>
        ${group.kind === 'unclassified' ? `<label class="field">本组题型<select data-answer-word-kind><option value="">请选择</option><option value="phrase">半句</option><option value="sentence">整句</option></select></label>` : ''}
        ${group.items.map((item, position) => {
          const id = `answer-word-${index}-${position}`;
          return `<div data-answer-word-item data-kind="${escapeHTML(group.kind)}">
            <label class="inline-check"><input type="checkbox" data-answer-word-choice>选择原第 ${item.number} 题</label>
            <textarea id="${id}" aria-label="${escapeHTML(group.label)}原第 ${item.number} 题答案" rows="2" maxlength="4000">${escapeHTML(item.answer)}</textarea>
          </div>`;
        }).join('')}`}
    </fieldset>`).join('')}
    ${draft.groups.some(group => group.kind !== 'summary') ? '<button class="btn primary" type="button" data-action="apply-answer-word">填入作业草稿</button>' : ''}`;
}
function applyAnswerWord() {
  const preview = $('#answer-word-preview');
  const selected = $$('[data-answer-word-choice]:checked', preview);
  if (!selected.length) throw new Error('请先勾选要填入草稿的题目。');
  const questions = selected.map((box, index) => {
    const item = box.closest('[data-answer-word-item]');
    const kind = item.dataset.kind === 'unclassified' ? $('[data-answer-word-kind]', item.closest('fieldset')).value : item.dataset.kind;
    const answer = $('textarea', item).value.trim().replace(/\s+/g, ' ');
    if (!kind || !answer || answer.length > 4000) throw new Error('请为每道已选题补全题型和答案（最多 4000 字）。');
    return answerRow(index+1, answer, {kind, points:''});
  });
  const existing = $$('#answer-rows input:not([type=number]),#answer-rows textarea').some(input => input.value.trim());
  if (existing && !confirm('填入 Word 答案会替换当前答案表。确定继续？')) return;
  $('#answer-rows').innerHTML = questions.join('');
  const title = $('#new-task-form').elements.task_name;
  if (!title.value.trim()) title.value = preview.dataset.name;
  $('#word-source-name').value = preview.dataset.source;
  $('#word-confirmation').hidden = false;
  $('#word-confirmed').checked = false;
  $('#word-confirmed').required = true;
  state.newTaskDirty = true;
  updateEditorTotal();
  $('.points-input', $('#answer-rows'))?.focus();
  toast(`已填入 ${questions.length} 道题，请填写分值并核对。`);
}
function renderNewTask() {
  $('#breadcrumb').textContent = '工作台 / 新建默写任务';
  $('#main').innerHTML = heading('新建默写任务', '先准备标准答案，再开始收集学生答卷。', '<a class="btn" href="/" data-nav>返回任务列表</a>') +
    '<form id="new-task-form"><section class="card"><div class="card-header"><div><h2>从 Word 导入语法填空</h2><p>识别括号题号、下划线和文末答案区；只生成候选内容，不自动保存。</p></div></div><div class="card-content form-grid"><label class="field">选择 Word 试题（.docx，不超过 5MB）<input id="word-file" type="file" accept=".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document"></label><button class="btn" type="button" data-action="preview-word">解析 Word 并预览题目</button><div id="word-warnings" aria-live="polite"></div><input id="word-source-name" type="hidden"><details><summary>试题原文与说明（可修改）</summary><label class="field">试题说明<textarea id="task-instructions" maxlength="4000" rows="2"></textarea></label><label class="field">完整题干（不包含答案区）<textarea id="task-passage" maxlength="200000" rows="10"></textarea></label></details><label class="inline-check" id="word-confirmation" hidden><input id="word-confirmed" type="checkbox">我已对照原 Word 核对题号、上下文、提示词、答案与分值</label></div></section><section class="card"><div class="card-header"><div><h2>导入答案 Word</h2><p>上传纯答案，按半句、整句和概要写作分组预览。</p></div></div><div class="card-content form-grid"><label class="field">选择答案 Word（.docx，不超过 5MB）<input id="answer-word-file" type="file" accept=".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document"></label><button class="btn" type="button" data-action="preview-answer-word">识别并预览答案</button><div id="answer-word-preview" class="form-grid" aria-live="polite"></div></div></section><section class="card"><div class="card-header"><div><h2>任务信息</h2><p>使用清晰的名称，方便以后查找</p></div></div><div class="card-content"><label class="field">任务名称<input name="task_name" placeholder="例如：Unit 3 单词与短语默写" maxlength="120" required></label></div></section><section class="card"><div class="card-header"><div><h2>快速录入标准答案</h2><p>先把答案一行一题粘贴进来，题号、每题 1 分和单词类型会自动生成；之后可在表格中调整。</p></div><button class="btn small" type="button" data-action="add-row">＋ 添加一题</button></div><div class="card-content"><div class="paste-row"><label class="field">批量添加<textarea id="paste-answers" rows="3" placeholder="一行一题，例如：&#10;apple&#10;go to school&#10;I like reading."></textarea></label><button class="btn" type="button" data-action="paste-rows">添加到答案表</button></div></div><div class="table-wrap"><table class="answer-editor"><thead><tr><th>题号</th><th>类型</th><th>标准答案</th><th>可接受写法（选填）</th><th>分值</th><th></th></tr></thead><tbody id="answer-rows">'+answerRow(1)+'</tbody></table></div><div class="editor-footer"><span class="muted tiny" id="editor-total">共 1 题 · 总分 1 分</span><button class="btn primary" type="submit">创建默写任务 →</button></div></section><p class="notice"><strong>创建前预览：</strong>题数和总分会随表格实时更新。评分规则：标准答案或任一可接受写法完全匹配即得本题满分，否则为 0 分。区分大小写和标点，连续空白会合并。复核时可手动改分并填写理由。</p></form>';
}
function updateEditorTotal() {
  const rows = $$('#answer-rows tr');
  $('#editor-total').textContent = `共 ${rows.length} 题 · 总分 ${numberText(rows.reduce((sum, row) => sum + Number($('.points-input', row).value || 0), 0))} 分`;
}
async function createTask(form) {
  const rows = $$('#answer-rows tr');
  if (!rows.length) throw new Error('请至少添加一道题。');
  const questions = rows.map((row, index) => ({number:Number($('.number-input',row).value),context:$('.context-input',row).value,hint:$('.hint-input',row).value,kind:$('select', row).value,answer:$('.answer-input', row).value.trim(),variants:$('.variant-input', row).value.split('|').map(x => x.trim()).filter(Boolean),points:Number($('.points-input', row).value)}));
  if (questions.some(q => !q.answer)) throw new Error('每道题都需要填写标准答案。');
  if ($('#word-source-name').value && !$('#word-confirmed').checked) throw new Error('请先核对 Word 候选内容并勾选确认。');
  const workflow=new URLSearchParams(location.search).get('workflow')==='1',class_ids=$$('input[name=workflow_classes]:checked',form).map(el=>Number(el.value));
  if(workflow&&!class_ids.length)throw new Error('请至少选择一个布置班级。');
  const data = await api('/api/tasks', {method:'POST',body:{name:form.elements.task_name.value.trim(),questions,passage:$('#task-passage').value,instructions:$('#task-instructions').value,source_name:$('#word-source-name').value}});
  $('#word-file').value = '';
  $('#answer-word-file').value = '';
  state.newTaskDirty = false;
  state.busy = false;
  if(workflow){
    try {const assigned=await api(`/api/tasks/${data.id}/assignments`,{method:'POST',body:{class_ids}});await navigate(assigned.assignments.length===1?`/assignments/${assigned.assignments[0].id}?tab=upload`:'/assignments?all=1');toast('作业已创建并布置。');}
    catch(error){await navigate(`/tasks/${data.id}`);toast(`标准答案已保存，班级布置未确认。请在此核对布置结果，无需重新创建。${error.message}`,true);}
  }else{await navigate(`/tasks/${data.id}`);toast('标准答案已创建，可布置给班级。');}
}

function renderTask(task,refresh=false) {
  $('#breadcrumb').textContent = `工作台 / ${task.name}`;
  const totalPoints = task.total_points ?? task.questions.reduce((sum, q) => sum + q.points, 0);
  const html = heading(task.name, '按学生建立答卷，照片、识别结果与复核记录保存在同一处。', `<div class="button-row"><a class="btn primary" href="#task-class-panel">布置给班级</a><details class="action-menu"><summary class="btn">导出 / 打印</summary><div class="action-menu-content"><a class="btn" href="/tasks/${task.id}/answer-card" target="_blank" rel="noopener">打印独立答题卡</a><a class="btn" href="/tasks/${task.id}/answer-card/download?format=pdf">下载 PDF</a><a class="btn" href="/tasks/${task.id}/answer-card/download?format=docx">下载 Word</a><a class="btn" href="/tasks/${task.id}/sheet" target="_blank" rel="noopener">打印答题纸</a><a class="btn" href="/tasks/${task.id}/grades.csv">导出成绩 CSV</a><div class="menu-danger"><button class="btn danger" data-action="delete-task" data-id="${task.id}">删除任务</button></div></div></details></div>`) +
    `<div class="summary-strip" id="task-summary" style="margin-bottom:24px"><span><strong>${task.questions.length}</strong>道题目</span><span><strong>${numberText(totalPoints)}</strong>总分</span><span><strong>${task.submissions.length}</strong>份答卷</span><a href="/tasks" data-nav>← 标准答案与布置</a></div><div id="task-class-panel"></div>${task.pending_identity_photos?`<section id="task-pending-photos" class="card"><div class="card-content notice warn"><strong>还有 ${task.pending_identity_photos} 张照片待确认学生归属</strong><p>这些照片尚未生成学生答卷，请进入对应班级任务处理。</p>${task.assignments.map(a=>`<a class="btn small primary" href="/assignments/${a.id}" data-nav>${escapeHTML(a.class_name)} · 处理照片 →</a>`).join(" ")}</div></section>`:""}${task.passage ? `<details class="card"><summary class="card-content">查看试题原文与说明</summary><div class="card-content"><p>${escapeHTML(task.instructions)}</p><div class="question-context">${escapeHTML(task.passage)}</div></div></details>` : ''}<details class="card"><summary class="card-content">临时答卷 / 旧版入口（不关联班级）</summary><div class="card-header"><div><h2>添加临时答卷</h2><p>班级学生请使用上方班级任务名单，避免重复建档</p></div></div><div class="card-content"><form id="new-student-form" data-task-id="${task.id}" class="student-form"><label class="field">学生编号<input name="student_code" placeholder="例如：01 或 202601" maxlength="80" required></label><button class="btn primary" type="submit">＋ 建立答卷并上传照片</button></form></div></details><section class="card" id="task-submissions"><div class="card-header"><div><h2>学生答卷</h2><p>已计分和识别异常分别显示，正常错题不要求人工确认。</p></div><span class="badge">${task.submissions.length} 份</span></div><div class="card-content">${recognitionControls('task',task.submissions)}<p id="task-status-message" role="status"></p></div>${task.submissions.length?`<div class="table-wrap recognition-table"><table><thead><tr><th>学生 / 班级</th><th>成绩</th><th>批改状态与识别情况</th><th>教师核对</th><th>操作</th></tr></thead><tbody>${task.submissions.map(sub=>`<tr data-recognition-id="${sub.id}"><td><strong>${escapeHTML(sub.student_code)} ${escapeHTML(sub.identity?.name||'')}</strong><div class="muted tiny">${escapeHTML(sub.identity?.class_name||'临时答卷')}</div></td><td>${sub.recognition?.state==='not_uploaded'?'—':sub.is_final?`${numberText(sub.score)} / ${numberText(sub.total_points)}`:'暂无最终成绩'}<div class="muted tiny">${escapeHTML(sub.grading_source||'未批改')}</div></td><td>${recognitionHTML(sub,'task')}</td><td>已核对 ${sub.teacher_reviewed_count??sub.confirmed_count} 题</td><td>${recognitionAction(sub,'task')}</td></tr>`).join('')}</tbody></table></div>`:empty('等待第一份学生答卷','请先上传学生作业照片。')}<div id="task-recognition-empty" class="card-content muted" hidden>没有符合筛选条件的答卷。</div></section><details class="card"><summary class="card-content" style="cursor:pointer">查看本次标准答案（${task.questions.length} 题）</summary><div class="table-wrap"><table><thead><tr><th>题号</th><th>类型</th><th>标准答案</th><th>可接受写法</th><th>分值</th></tr></thead><tbody>${task.questions.map(q => `<tr><td>${q.number}</td><td>${kindNames[q.kind]}</td><td>${escapeHTML(q.answer)}${contextHTML(q)}</td><td>${escapeHTML((q.variants || []).join(' / ')) || '—'}</td><td>${numberText(q.points)}</td></tr>`).join('')}</tbody></table></div></details>`;
  if(refresh) {
    // Preserve upload inputs, open evidence and teacher forms while updating results.
    const template=document.createElement('template');template.innerHTML=html;
    for(const id of ['task-summary','task-submissions'])$(`#${id}`).replaceWith(template.content.querySelector(`#${id}`));
    const previous=$('#task-pending-photos'),pending=template.content.querySelector('#task-pending-photos');
    if(previous) {if(pending)previous.replaceWith(pending);else previous.remove();}
    else if(pending)$('#task-class-panel').after(pending);
  } else $('#main').innerHTML=html;
  mountRecognitionList('task',task.submissions);recognitionList.task=task;
  if(task.background_jobs || task.submissions.some(s=>recognitionFor(s).state==='processing'))pollTaskList(task.id,state.routeVersion);
}

function isGrading(sub) {
  return sub?.grading_state === 'running' || ['queued','running'].includes(sub?.processing_job?.state) || !!sub?.pages?.some(page=>page.status==='running');
}
function updateReviewLock() {
  const locked=state.busy || isGrading(state.submission) || !!state.submission?.identity?.class_archived;
  $$('.review-edit-fields').forEach(fields=>{fields.disabled=locked;});
  $$('.grading-status button,.review-actions button,.quick-verdict button,[data-action="associate"],[data-action="toggle-draw"],[data-action="ack-page"],[data-action="save-all-reviews"]').forEach(button=>{button.disabled=locked;});
  const undo=$('[data-action="undo-review"]');
  if(undo)undo.disabled=!!state.submission?.identity?.class_archived || state.busy || lastReviewUndo?.submission_id===state.submission?.id && isGrading(state.submission);
}
async function autoGrade(sid,rerun = false,options = {}) {
  if (state.drafts.size) throw new Error('有未保存的题目修改，请先保存后再自动批改。');
  const version = state.routeVersion;
  let sub = await api(`/api/submissions/${sid}`);
  if (version !== state.routeVersion) return sub;
  if (!isGrading(sub)) {
    const interrupted = sub.processing_job?.state === 'interrupted';
    try {
      const {job} = await api(interrupted ? `/api/grading-jobs/${sub.processing_job.id}/resume` : `/api/submissions/${sid}/grading-jobs`,
        {method:'POST',body:interrupted ? {} : rerun ? {rerun:true} : options});
      if (version !== state.routeVersion) return sub;
      sub = {...sub,processing_job:job};
      if (isGrading(sub)) sub = {...sub,is_final:false,status:'pending',recognition:{state:'processing',uncertain_count:0,needs_attention:false}};
      if (state.submission?.id === sid) {
        state.submission = sub;
        state.activeQuestion = null;
        renderSubmission();
        pollGrading(sid,version);
      }
    } catch (error) {
      if (version === state.routeVersion && state.submission?.id === sid) pollGrading(sid,version);
      throw error;
    }
    sub = await api(`/api/submissions/${sid}`);
  }
  if (version === state.routeVersion && state.submission?.id === sid) {
    state.submission = sub;
    state.activeQuestion = null;
    renderSubmission();
    if (isGrading(sub)) pollGrading(sid,state.routeVersion);
    else clearTimeout(gradingPollTimer);
  }
  return sub;
}
function pollGrading(sid,version) {
  clearTimeout(gradingPollTimer);
  gradingPollTimer = setTimeout(async () => {
    if (version !== state.routeVersion || state.submission?.id !== sid) return;
    try {
      const jobId = state.submission.processing_job?.id;
      if (jobId) {
        const {job} = await api(`/api/grading-jobs/${jobId}`);
        if (version !== state.routeVersion || state.submission?.id !== sid) return;
        if (['queued','running'].includes(job.state)) {
          state.submission.processing_job = job;
          const bar = $('.grading-status .running-bar');
          if (bar) bar.innerHTML = `<span class="spinner"></span>${escapeHTML(job.message || '后台正在批改')}；可以离开页面。`;
          pollGrading(sid,version);return;
        }
      }
      const sub = await api(`/api/submissions/${sid}`);
      if (version !== state.routeVersion || state.submission?.id !== sid) return;
      state.submission = sub;
      // Preserve local edits and keyboard focus while only updating background state.
      if (!state.drafts.size && !document.activeElement?.matches('[data-field]')) renderSubmission();
      else if(!isGrading(sub)) {
        const bar=$('.grading-status .running-bar');
        if(bar){bar.className=`notice ${sub.grading_state==='failed'?'error':''}`;bar.textContent=`${sub.grading_state==='failed'?`后台批改失败：${sub.grading_error || '请稍后重试'}`:sub.processing_job?.state==='interrupted'?'后台处理已中断':'后台处理已结束'}。当前输入已保留，保存修改后刷新成绩显示。`;}
      }
      updateReviewLock();
      if (isGrading(sub)) pollGrading(sid,version);
    } catch (error) {
      if (version !== state.routeVersion || state.submission?.id !== sid) return;
      toast(`读取批改进度失败，正在重新读取：${error.message}`,true);
      pollGrading(sid,version);
    }
  },2000);
}
function renderUnassignedOCR() {
  const target = $('#unassigned-ocr'), page = currentPage();
  if (!target) return;
  const used = new Set(state.submission.reviews.flatMap(q => reviewDraft(q).regions)
    .filter(r => r.page_id === page?.id).flatMap(r => r.block_ids || []));
  const blocks = (page?.blocks || []).filter(b => !used.has(b.id));
  target.innerHTML = blocks.length ? `<details class="unassigned-ocr"><summary>其他识别文字（含姓名、标题或待处理正文）· ${blocks.length} 个文字块</summary><p class="muted tiny">这些文字尚未关联题目，不代表学生答错。需要时展开复制到教师转录框，或在原图中选择区域。</p><pre>${escapeHTML(blocks.map(b => b.text).join('\n'))}</pre></details>` : '';
}

function reviewDraft(question, includeChanges = true) {
  return (includeChanges && state.drafts.get(question.question_id)) || {transcript:question.transcript || '',regions:question.regions || [],confirmed_blank:!!question.confirmed_blank,score_override:question.score_override ?? null,reason:question.reason || ''};
}
function confidenceFor(q) {
  const values = (q.regions || []).flatMap(region => (state.submission.pages.find(page => page.id === region.page_id)?.blocks || [])
    .filter(block => (region.block_ids || []).includes(block.id)).filter(block => block.confidence != null).map(block => Number(block.confidence)).filter(Number.isFinite));
  return values.length ? Math.min(...values) : null;
}
function questionPriority(q) {
  if (q.confirmed && !state.drafts.has(q.question_id)) return 4;
  const draft = reviewDraft(q);
  const confidence = confidenceFor(q);
  if (draft.transcript.trim() && !q.matched) return 0;
  if (confidence !== null && confidence < 0.85) return 1;
  if (!draft.transcript.trim() && !draft.confirmed_blank) return 2;
  if (!q.confirmed) return 3;
  return 4;
}
function nextPriorityQuestion(reviews, currentId = null) {
  return reviewQuestions(reviews).filter(q => q.question_id !== currentId && !q.confirmed)[0] || null;
}
function reviewQuestions(reviews = state.submission.reviews) {
  return reviews.filter(q => state.filter === 'all' || (state.filter === 'wrong' && (q.graded || q.auto_result || q.confirmed) && Number(q.score) < Number(q.points)) || (state.filter === 'uncertain' && q.uncertain) || (state.filter === 'different' && q.transcript.trim() && !q.matched) || (state.filter === 'pending' && (!q.confirmed || state.drafts.has(q.question_id))) || (state.filter === 'confirmed' && q.confirmed && !state.drafts.has(q.question_id)))
    .sort((a,b) => state.reviewOrder === 'number' ? a.number-b.number : questionPriority(a)-questionPriority(b) || a.number-b.number);
}
function selectReviewQuestion(id) {
  if (state.activeQuestion !== id) {
    visualFrame = null;
    if (state.draw === 'visual') state.draw = false;
  }
  state.activeQuestion = id;
  state.selected.clear();
  const question = state.submission.reviews.find(q=>q.question_id===id);
  const region = question && reviewDraft(question).regions.find(r=>state.submission.pages.some(p=>p.id===r.page_id));
  if (region) state.pageId = region.page_id;
}
function recognitionEvidenceHTML(q) {
  const decision=q.auto_result;
  if(decision?.ai_review) return `<div class="notice" role="note"><strong>AI 自动复核完成 · ${escapeHTML(decision.ai_review.model)}</strong><p>独立图片转录与局部 OCR 一致，系统按原文严格计分。原始识别证据已保留，教师仍可改判。</p><button class="btn small" data-action="undo-ai-review" data-id="${q.question_id}" data-event-id="${decision.ai_review.event_id}">撤销本次 AI 判定</button></div>`;
  if(!decision?.recognition_conflict || !decision.recognition_evidence?.length) return '';
  const labels={existing_ocr:'整页识别',supplementary_ocr:'整页补识别',region_supplementary_ocr:'局部补识别'};
  return `<div class="recognition-evidence notice warn" role="note" aria-label="识别结果对照"><strong>${q.uncertain?'识别结果不一致，请对照原图判定':'识别结果曾不一致，教师判定已保存'}</strong><dl>${decision.recognition_evidence.map(e=>`<div><dt>${labels[e.source]||'识别记录'}</dt><dd>${escapeHTML(e.transcript)}</dd></div>`).join('')}</dl><p>当前机器采用：${escapeHTML(decision.transcript)}；各次原文均保留。</p></div>`;
}
let visualFrame = null, visualPending = null, visualFeedback = null;
function visualReviewHTML(q) {
  if (!q?.uncertain || q.teacher_edited || state.submission.identity?.class_archived ||
      !['word','phrase','grammar'].includes(q.kind)) return '';
  const current = entry => entry?.version === state.routeVersion && entry.question === q.question_id;
  const frame = current(visualFrame) && visualFrame.page_id === state.pageId;
  const pending = current(visualPending);
  const feedback = current(visualFeedback) ? visualFeedback.message : '';
  const suggestion = state.submission.visual_suggestions?.[q.question_id];
  const locked = state.busy || isGrading(state.submission);
  const button = (action, label, disabled = false, variant = '') =>
    `<button class="btn small ${variant}" data-action="${action}" data-id="${q.question_id}" ${disabled ? 'disabled' : ''}>${label}</button>`;
  const label = {unreadable:'笔迹无法辨认', blank:'疑似空白，请核对原图', ambiguous:'笔迹或位置不能确定'};
  const result = suggestion ? `<img alt="送给视觉模型的本题局部图" src="${escapeHTML(suggestion.preview_url)}"><p>复读转录 <strong>${suggestion.status === 'readable' ? escapeHTML(suggestion.transcript) : label[suggestion.status] || '无法核实'}</strong></p>
    ${suggestion.suggested_score == null ? '' : `<p>建议 ${numberText(suggestion.suggested_score)} / ${numberText(suggestion.max_score)} 分</p>`}
    ${suggestion.can_adopt ? button('visual-adopt', attentionContext ? '采纳并下一项' : '采纳复读建议', locked, 'primary') : '<p class="tiny muted">请对照原图使用下方人工判定。</p>'}` : '';
  return `<section class="visual-review notice" aria-label="视觉复读建议"><strong>视觉复读</strong>
    <p class="tiny muted">${state.draw === 'visual' ? '请在原图拖动，只圈本题作答。' : '框选本题作答后，可发送局部图复读。请避开姓名、学号和相邻题。'}</p>
    <div class="button-row">${button('visual-select', state.draw === 'visual' ? '取消框选' : frame ? '重新框选' : '框选作答', locked || pending)}
    ${frame ? button('visual-run', pending ? '正在复读…' : '发送局部图复读', locked || pending) : ''}</div>
    <p role="status" class="tiny ${feedback ? 'status-danger' : 'muted'}">${escapeHTML(feedback || (pending ? '正在复读，可继续人工核对。' : ''))}</p>${result}</section>`;
}

function renderVisualReview() {
  const panel = $('#visual-review');
  const q = state.submission?.reviews.find(item=>item.question_id===state.activeQuestion);
  if (panel) panel.innerHTML = visualReviewHTML(q);
}

async function requestVisualReread(id) {
  const frame = visualFrame, sub = state.submission;
  if (!frame || frame.question !== id || frame.page_id !== state.pageId ||
      frame.version !== state.routeVersion || visualPending) return;
  visualPending = frame;
  visualFeedback = null;
  renderVisualReview();
  try {
    const suggestion = await api(`/api/submissions/${sub.id}/reviews/${id}/visual-reread`, {
      method:'POST',body:{page_id:frame.page_id,box:frame.box,rotation:frame.rotation}});
    if (state.submission === sub && frame.version === state.routeVersion) {
      sub.visual_suggestions = {...sub.visual_suggestions,[id]:suggestion};
      if (visualFrame === frame) visualFrame = null;
    }
  } catch (error) {
    if (frame.version === state.routeVersion) visualFeedback = {...frame,message:error.message};
  } finally {
    visualPending = null;
    if (frame.version === state.routeVersion) {
      renderVisualReview();
      renderPhoto();
    }
  }
}

function reviewHTML(q) {
  const d = reviewDraft(q), dirty = state.drafts.has(q.question_id), active = state.activeQuestion === q.question_id;
  const confidence = confidenceFor(q);
  const waiting = !q.graded && !q.auto_result && !d.transcript.trim() && !d.confirmed_blank;
  const fullStatus = dirty ? '有未保存修改' : q.graded || q.auto_result ? `${q.teacher_edited || q.confirmed ? '教师复核' : '机器自动批改'} · ${q.uncertain ? '不确定' : Number(q.score) < Number(q.points) ? '答错' : '正确'}` : q.confirmed ? '已确认' : waiting ? '待识别 / 待提取' : questionPriority(q) === 0 ? '有差异 · 请核对' : confidence !== null && confidence < 0.85 ? '低置信度' : '待核对';
  const status = dirty?'未保存':q.uncertain?'不确定':q.confirmed_blank?'未作答':waiting?'待识别':(q.graded||q.auto_result||q.confirmed)?(Number(q.score)<Number(q.points)?'答错':''):'待核对';
  const statusClass = dirty || q.uncertain || status==='待核对' ? 'amber' : status==='答错' || status==='未作答' ? 'red' : '';
  const summary = q.teacher_decision ? `${({correct:'教师判定正确',incorrect:'教师判定错误',blank:'教师确认未作答'})[q.teacher_decision]} · ${numberText(q.score)} / ${numberText(q.points)} 分${q.transcript ? ` · 原转录：${escapeHTML(q.transcript)}` : ' · 未补录作答文字'}` : q.graded || q.auto_result ? `作答：${escapeHTML(q.transcript || (q.confirmed_blank ? '（教师确认未作答）' : q.auto_result?.diagnostic === 'ocr_no_answer' ? '（识别结果未返回作答文字）' : '（识别文字未能可靠关联本题）'))} · 标准答案：${escapeHTML(q.answer)}${q.uncertain ? ' · 识别不确定，可查看原图' : ''}` : q.confirmed ? `已确认 · ${numberText(q.score)} / ${numberText(q.points)}` : `${q.machine_text ? `识别：${escapeHTML(q.machine_text.slice(0, 48))}` : '尚未取得本题作答，暂不判断对错'}${confidence !== null ? ` · 置信度 ${Math.round(confidence * 100)}%` : ''}`;
  const decisionLabel = {correct:'教师判定正确',incorrect:'教师判定错误',blank:'教师确认未作答'}[q.teacher_decision];
  const body = active ? `<div class="review-body">${contextHTML(q)}<div class="expected quick-expected"><span class="muted">标准答案</span> <strong>${escapeHTML(q.answer)}</strong>${q.variants?.length ? `<div class="variants">可接受：${q.variants.map(escapeHTML).join(' / ')}</div>` : ''}</div><p class="machine">机器识别：${escapeHTML(q.auto_result?.transcript ?? q.machine_text) || '未取得作答文字，请查看左侧原图'}</p>${recognitionEvidenceHTML(q)}<div id="visual-review">${visualReviewHTML(q)}</div>${q.uncertain ? '<p class="quick-review-hint">请对照原图直接判定，无需先输入答案。</p>' : ''}${decisionLabel ? `<p class="notice teacher-verdict">${decisionLabel} · ${numberText(q.score)} / ${numberText(q.points)} 分。${q.confirmed_blank ? '' : '原始识别保留，未补录的文字不代表未作答。'}</p>` : ''}<div class="quick-verdict" aria-label="教师直接判定"><p class="tiny muted">${attentionContext?'核对原图后点击，即保存并进入下一项':'核对原图后点击即保存，留在当前题目'}</p><div class="quick-verdict-buttons"><button class="btn verdict-correct" data-action="quick-correct" data-id="${q.question_id}" ${state.busy || isGrading(state.submission) ? 'disabled' : ''}>${attentionContext?'判对并下一项':'判为正确'} · ${numberText(q.points)} 分</button><button class="btn verdict-incorrect" data-action="quick-incorrect" data-id="${q.question_id}" ${state.busy || isGrading(state.submission) ? 'disabled' : ''}>${attentionContext?'判错并下一项':'判为错误'} · 0 分</button><button class="btn" data-action="quick-blank" data-id="${q.question_id}" ${state.busy || isGrading(state.submission) ? 'disabled' : ''}>未作答 · 0 分</button></div></div><details class="review-details" ${dirty || (!q.auto_result && !q.graded && !q.teacher_decision) ? 'open' : ''}><summary>修改识别文字 / 部分给分 / 更多设置</summary><fieldset class="review-edit-fields" aria-label="修改本题作答与分数">${q.auto_result?.reason ? `<p class="tiny muted">识别说明：${escapeHTML(q.auto_result.reason)}</p>` : ''}<div class="region-label"><span>已关联 ${d.regions.length} 个段落参考区域</span><button class="text-button" data-action="clear-regions" data-id="${q.question_id}">清除关联</button></div><label class="field teacher-transcript"><span>教师确认转录 <small>评分依据</small></span><textarea data-field="transcript" aria-label="第 ${q.number} 题教师确认转录" rows="3" ${d.confirmed_blank ? 'disabled' : ''}>${escapeHTML(d.transcript)}</textarea></label>${!q.machine_text ? '<div class="notice warn">机器识别为空，不等于学生未作答。请对照原图确认。</div>' : ''}${dirty ? '<div class="diff">已修改，请保存后更新差异与得分。</div>' : `<div class="diff" aria-label="文字差异">${q.teacher_decision ? '教师已按原图判定，本题成绩不依赖机器转录。' : waiting ? '尚未取得本题作答，暂不比较答案。' : diffHTML(q.diff)}</div>`}<label class="inline-check"><input type="checkbox" data-field="confirmed_blank" ${d.confirmed_blank ? 'checked' : ''}>经原图核对，确认本题未作答</label><div class="manual-row"><label class="field">手动分数（选填）<input data-field="score_override" type="number" min="0" max="${q.points}" step="0.01" placeholder="自动评分" value="${d.score_override ?? ''}"></label><label class="field">改分理由（改分时必填）<input data-field="reason" maxlength="1000" placeholder="例如：原卷书写正确，识别有误" value="${escapeHTML(d.reason)}"></label></div><div class="review-actions"><button class="btn small" data-action="save-review" data-id="${q.question_id}" ${state.busy ? 'disabled' : ''}>${q.auto_result || q.graded ? '保存修改（立即更新成绩）' : '保存草稿'}</button><button class="btn small primary" data-action="confirm-review" data-id="${q.question_id}" ${state.busy ? 'disabled' : ''}>确认本题并保存 ✓</button>${attentionQueue ? `<button class="btn small primary" data-action="save-next-attention" data-id="${q.question_id}">保存并下一条异常</button>` : ''}<button class="text-button" data-action="defer-review" data-id="${q.question_id}">跳过本题</button></div></fieldset></details></div>` : `<div class="review-summary">${summary}<button class="btn small" data-action="focus-question" data-id="${q.question_id}">查看并处理 →</button></div>`;
  return `<article class="review-card ${active ? 'active' : ''} ${q.confirmed ? 'is-confirmed' : ''}" data-question-id="${q.question_id}"><div class="review-head" data-action="focus-question" data-id="${q.question_id}" tabindex="0" role="button" aria-label="查看第 ${q.number} 题 · ${escapeHTML(fullStatus)}"><h3><span class="question-number">${String(q.number).padStart(2,'0')}</span> ${kindNames[q.kind] || '题目'} ${badge(status,statusClass,`data-review-state ${status?'':'hidden'} title="${escapeHTML(fullStatus)}"`)}</h3><span class="score">${waiting ? '—' : numberText(q.score)}<small> / ${numberText(q.points)}</small></span></div>${body}</article>`;
}
function diffHTML(diff = []) {
  return diff.map(part => {
    if (part.op === 'equal') return escapeHTML(part.actual ?? part.expected);
    return `${part.expected ? `<del>${escapeHTML(part.expected)}</del>` : ''}${part.actual ? `<ins>${escapeHTML(part.actual)}</ins>` : ''}`;
  }).join('');
}
function attentionNavigation() {
  if (!attentionContext) return '';
  const title=attentionContext.scope==='task'?'任务异常':'全班异常';
  return `<div class="attention-navigation notice ${attentionLoadError?'warn':''}" role="status"><strong>${attentionLoadError || `${title} · ${attentionQueue?.items.length??0} 条待处理`}</strong><span>普通错题已计分，不要求逐题确认。</span>${attentionLoadError?'<button class="btn small primary" data-action="retry-attention">重新读取异常列表</button>':attentionQueue?'<button class="btn small" data-action="next-attention">下一条异常 →</button>':''}</div>`;
}
async function refreshAttentionQueue() {
  const context=attentionContext,version=state.routeVersion;
  if(!context || context.loading)return false;
  context.loading=true;
  try {
    const queue=await api(context.endpoint);
    if(context!==attentionContext || version!==state.routeVersion)return false;
    attentionQueue=queue;attentionLoadError='';return true;
  }catch(error){
    if(context===attentionContext && version===state.routeVersion)attentionLoadError='异常列表暂时无法读取，已保存的成绩与修改不受影响。';
    return false;
  }finally{context.loading=false;}
}
async function nextAttention(refresh = false, questionId = state.activeQuestion, previousItems = attentionQueue?.items || []) {
  if (!attentionContext || attentionContext.loading || (!attentionQueue&&!refresh) || !await Nav.mayLeave()) return;
  const context=attentionContext,version=state.routeVersion,sid=state.submission.id,qid=questionId,old=previousItems;
  const index=old.findIndex(i=>i.submission_id===sid&&i.question_id===qid);
  if((refresh || attentionLoadError) && !await refreshAttentionQueue()) {
    if(context===attentionContext && version===state.routeVersion)renderSubmission();return;
  }
  const ordered=[...old.slice(index+1),...old.slice(0,index+1)];
  const next=ordered.map(i=>attentionQueue.items.find(n=>n.id===i.id)).find(i=>i&&!(i.submission_id===sid&&i.question_id===qid)) || attentionQueue.items.find(i=>!(i.submission_id===sid&&i.question_id===qid));
  if(!next){const remaining=attentionQueue.items.length;await Nav.back({approved:true});toast(remaining?'没有其他异常，当前项目仍待处理。':'本轮异常已处理完毕。');return;}
  if(next.kind==='identity'){await Nav.go(`/assignments/${next.assignment_id||attentionContext.id}?tab=attention&photo=${next.photo_id}`,true,{approved:true});return;}
  const params=new URLSearchParams({attention:'1',queue:attentionContext.scope,from:attentionContext.returnPath});
  if(next.question_id){params.set('question',next.question_id);params.set('filter','uncertain');}
  await Nav.go(`/submissions/${next.submission_id}?${params}`,true,{approved:true});
}
function renderSubmission() {
  const viewport = photoViewport();
  const sub = state.submission;
  const questions = reviewQuestions();
  if (state.activeQuestion !== null && !questions.some(q=>q.question_id===state.activeQuestion)) selectReviewQuestion(questions[0]?.question_id ?? null);
  const identity = sub.identity;
  const overview = (sub.is_final || sub.status === 'confirmed') && state.activeQuestion === null;
  const canExtract = sub.pages.length && sub.pages.every(p => p.status === 'done' || p.blocks?.length) && !isGrading(sub);
  const missingAnswer = sub.reviews.some(q => !q.teacher_edited && q.auto_result && !q.auto_result.transcript && (q.auto_result.source === 'no_valid_answer' || q.auto_result.diagnostic));
  const repairControls = canExtract ? `<button class="btn" data-action="repair-recognition" ${state.busy ? 'disabled' : ''}>修复识别问题</button><details class="more-operations"><summary>更多识别操作</summary><button class="btn" data-action="reextract-answers" ${state.busy ? 'disabled' : ''}>重新分题（不调用识别）</button>${missingAnswer ? `<button class="btn" data-action="retry-missing" ${state.busy ? 'disabled' : ''}>补识别未取得作答</button><p class="muted">仅补识别可定位的缺失作答区域，每页成功补识别一次；使用云端时可能消耗额度。仍未取得作答可对照原图改判。</p>` : ''}</details>` : '';
  $('#breadcrumb').textContent = `工作台 / ${sub.task_name} / ${sub.student_code}`;
  $('#main').innerHTML = heading(`${identity ? `${identity.name}（${identity.student_code}）` : sub.student_code}`, `${identity ? `${identity.class_name} · ${identity.term} · ` : ''}${sub.task_name}`, `<div class="button-row"><button class="btn" data-action="return-context">${escapeHTML(Nav.returnInfo().label)}</button><details class="action-menu"><summary class="btn">更多操作</summary><div class="action-menu-content"><a class="btn" href="/api/submissions/${sub.id}/history" target="_blank" rel="noopener">修改记录</a><a class="btn" href="/submissions/${sub.id}/report" target="_blank" rel="noopener">打印批改报告</a></div></details></div>`) +
    `${attentionNavigation()}<div class="submission-recognition" role="status">${recognitionHTML(sub,attentionContext?.scope||'task')}${recognitionFor(sub).state==='completed'?'<span class="muted tiny">可按需抽查原图</span>':''}</div><div class="summary-strip review-summary-strip" aria-live="polite"><span><strong>${numberText(sub.score)} / ${numberText(sub.total_points)}</strong> ${sub.is_final || sub.status === 'confirmed' ? '最终成绩' : '尚未完成批改'}</span><span><strong>${sub.correct_count ?? sub.reviews.filter(q => (q.graded || q.confirmed) && Number(q.score) >= Number(q.points)).length}</strong> 正确 · <strong>${sub.wrong_count ?? sub.reviews.filter(q => (q.graded || q.confirmed) && Number(q.score) < Number(q.points)).length}</strong> 错误 · <strong>${recognitionFor(sub).uncertain_count}</strong> 不确定</span><span class="muted tiny">${escapeHTML(sub.grading_source || (sub.status === 'confirmed' ? '教师复核' : '等待自动批改'))}</span><span>机器批改 ${sub.machine_graded_count || 0} 题 · 教师复核 ${sub.teacher_reviewed_count ?? sub.confirmed_count} 题</span></div><div class="grading-status" role="status">${isGrading(sub) ? `<p class="running-bar"><span class="spinner"></span>${sub.processing_job?.state==='queued'?'已排队，等待后台批改':'后台正在批改'}；完成后可修改，可以离开页面。刷新只读取进度，不重复识别。</p>` : sub.grading_state === 'failed' ? `<p class="notice error">自动批改失败：${escapeHTML(sub.grading_error || '请重试')}。已保存的原图和修改保留。</p>` : ''}${sub.pages.length && !isGrading(sub) ? `<button class="btn ${sub.is_final || sub.status === 'confirmed' ? '' : 'primary'}" data-action="auto-grade" ${state.busy ? 'disabled' : ''}>${sub.grading_state === 'failed' ? '重试自动批改' : sub.is_final || sub.status === 'confirmed' ? '重新自动批改' : '开始自动批改'}</button>` : ''}${sub.processing_job?.state==='interrupted'?'<p class="notice warn">上次处理已中断，点击“开始／重新自动批改”可继续；不会在刷新时重新提交。</p>':''}${repairControls}${sub.pages.filter(p => p.status === 'done' && p.error).map(p => `<p class="notice warn">${escapeHTML(p.error)}</p>`).join('')}</div>${sub.is_final || sub.status === 'confirmed' ? `<button class="btn result-view-toggle" data-action="toggle-evidence">${overview ? '查看原图 / 添加照片' : '返回成绩概览'}</button>` : ''}<div class="review-layout ${overview ? 'result-overview' : ''}"><section class="card review-image-card" ${overview ? 'hidden' : ''}><div class="card-header"><div><h2>学生答卷原图</h2><p>识别框可能覆盖整段；点击文字块，或手动框选作答区域</p></div><span class="badge blue">原图核对</span></div><div id="photo-panel"></div></section><section aria-label="逐题复核"><div class="review-controls" aria-label="题目结果筛选"><div class="review-control-actions"><select id="review-order" aria-label="题目排序"><option value="priority">优先处理需判断题目</option><option value="number">按题号查看</option></select><select id="review-filter" aria-label="筛选题目"><option value="all">全部题目</option><option value="wrong">错题</option><option value="uncertain">不确定</option><option value="different">有差异（人工批改）</option><option value="pending">未人工核对（可选）</option><option value="confirmed">教师已核对</option></select></div></div><div id="unassigned-ocr"></div><div id="review-list" class="review-list"></div></section></div>`;
  $('#main').classList.add('submission-page');
  const layout=$('.review-layout'),tools=document.createElement('div');tools.className='review-workspace-bar';
  layout.before(tools);
  ['.submission-recognition','.review-summary-strip','.review-controls','.result-view-toggle'].forEach(selector=>{const node=$(selector);if(node)tools.append(node);});
  const maintenanceRow=document.createElement('div');maintenanceRow.className='review-maintenance-row';layout.before(maintenanceRow);maintenanceRow.append($('.grading-status'));
  $('.grading-status').insertAdjacentHTML('beforebegin','<p id="page-upload-status" class="notice" role="status" aria-live="polite" hidden></p>');
  $('.grading-status').insertAdjacentHTML('beforebegin','<div id="review-drafts" class="notice button-row" role="status" aria-live="polite" hidden><span id="review-drafts-message"></span><button class="btn small primary" data-action="save-all-reviews">保存本答卷全部修改</button></div>');
  renderPageUploadStatus();
  $('#review-filter').value = state.filter;
  const maintenance=$('.grading-status');
  if(sub.identity?.class_archived){maintenance.innerHTML='<p class="notice">班级已归档，仅可查看历史成绩与原图；恢复班级后才能修改。</p>';}
  else if(!isGrading(sub)&&sub.grading_state!=='failed'&&sub.processing_job?.state!=='interrupted'&&(sub.is_final||sub.status==='confirmed')){
    const details=document.createElement('details');details.className='more-operations';details.innerHTML='<summary>更多操作 · 识别维护</summary><p class="tiny muted">重新识别可能调用云端并消耗额度；教师修改保留。</p>';[...maintenance.children].filter(node=>!node.matches('.notice')).forEach(node=>details.append(node));maintenance.append(details);
    [...maintenance.querySelectorAll(':scope > .notice')].forEach(node=>maintenanceRow.append(node));
    maintenance.querySelectorAll('details').forEach(group=>{const section=document.createElement('div');section.className='review-maintenance-group';[...group.children].filter(node=>node.tagName!=='SUMMARY').forEach(node=>section.append(node));group.replaceWith(section);});
    $('.page-heading .action-menu-content').append(maintenance);
  }
  renderPhoto(viewport); renderReviews();updateReviewLock();
}
function renderReviews() {
  const questions = reviewQuestions();
  if (state.activeQuestion !== null && !questions.some(q => q.question_id === state.activeQuestion)) { selectReviewQuestion(questions[0]?.question_id ?? null); renderPhoto(); }
  $('#review-list').innerHTML = questions.length ? questions.map(reviewHTML).join('') : empty('此筛选下没有题目', '可切换筛选查看其他题目。');
  const order = $('#review-order'); if (order) order.value = state.reviewOrder;
  renderDraftActions();
  updateReviewLock();
}
function currentPage() { return state.submission.pages.find(page => page.id === state.pageId); }
function photoRotation(page) { return photoRotations.get(page.id) ?? page.display_rotation ?? 0; }
function displaySize(page) { return photoRotation(page) % 180 ? [page.height,page.width] : [page.width,page.height]; }
function displayBox(box, page) {
  const [x1,y1,x2,y2] = box, w = page.width, h = page.height;
  return ({90:[y1,w-x2,y2,w-x1],180:[w-x2,h-y2,w-x1,h-y1],270:[h-y2,x1,h-y1,x2]})[photoRotation(page)] || box;
}
function originalBox(box, page) {
  const [x1,y1,x2,y2] = box, w = page.width, h = page.height;
  return ({90:[w-y2,x1,w-y1,x2],180:[w-x2,h-y2,w-x1,h-y1],270:[y1,h-x2,y2,h-x1]})[photoRotation(page)] || box;
}
function boxStyle(box, page) {
  box = displayBox(box,page);
  const [w,h] = displaySize(page), width = Math.max(w,1), height = Math.max(h,1);
  return `left:${box[0]/width*100}%;top:${box[1]/height*100}%;width:${(box[2]-box[0])/width*100}%;height:${(box[3]-box[1])/height*100}%`;
}
function photoViewport() {
  const scroll = $('#photo-scroll');
  return scroll?.getClientRects().length ? {key:scroll.dataset.photoKey,question:scroll.dataset.question,top:scroll.scrollTop,left:scroll.scrollLeft} : null;
}
function locateQuestionRegion() {
  const scroll=$('#photo-scroll'), region=$('.region-box',scroll || document);
  if (!region || !scroll?.getClientRects().length) return;
  const box=region.getBoundingClientRect(), view=scroll.getBoundingClientRect();
  scroll.scrollBy({top:box.top-view.top-(scroll.clientHeight-box.height)/2,left:box.left-view.left-(scroll.clientWidth-box.width)/2,behavior:'instant'});
}
function renderPhoto(viewport = photoViewport()) {
  const sub = state.submission, page = currentPage();
  const panel = $('#photo-panel');
  if (!page) {
    panel.innerHTML = empty('上传学生默写答卷', '支持清晰的 JPG、PNG 照片，可连续上传多页。') + uploadHTML();
    return;
  }
  const q = sub.reviews.find(item => item.question_id === state.activeQuestion);
  const regions = q ? reviewDraft(q).regions.filter(region => region.page_id === page.id) : [];
  panel.innerHTML = `<div class="image-toolbar"><select id="page-select" aria-label="选择答卷页">${sub.pages.map((p,index) => `<option value="${p.id}" ${p.id === page.id ? 'selected' : ''}>第 ${index+1} 页 · ${p.status === 'uploaded' && p.blocks?.length ? '已有文字 · 待提取' : ({uploaded:'待识别',running:'识别中',done:'已识别',error:'识别失败'}[p.status] || p.status)}</option>`).join('')}</select><button class="btn small" data-action="rotate-photo" aria-label="逆时针旋转答卷">旋转 90°</button>${regions.length ? '<button class="btn small" data-action="locate-region">定位本题</button>' : ''}<label class="zoom-label"><span>缩放</span><input id="image-zoom" type="range" min="50" max="250" step="10" value="${state.zoom}" aria-label="原图缩放比例"></label></div>${state.busy ? `<div class="running-bar"><span class="spinner"></span>${escapeHTML(state.ocrProgress || '正在处理，请保持页面打开。首次识别加载模型可能需要数分钟。')}</div>` : ''}${page.blocks?.some(b => b.approximate) ? '<p class="notice warn">云端文字框位置为参考，未提供 OCR 置信度。请对照原图调整区域，勿据此认定笔迹正确。</p>' : ''}${page.error ? `<div class="notice error" style="margin:12px">${escapeHTML(page.error)}</div>` : ''}<div class="photo-scroll" id="photo-scroll"><div class="photo-stage ${state.draw ? 'drawing' : ''}" id="photo-stage" style="width:${state.zoom}%"><img width="${displaySize(page)[0]}" height="${displaySize(page)[1]}" src="${escapeHTML(photoRotation(page) ? `/api/pages/${page.id}/preview?rotation=${photoRotation(page)}` : page.url)}" alt="学生 ${escapeHTML(sub.student_code)} 的第 ${sub.pages.indexOf(page)+1} 页原始答卷" draggable="false">${(page.blocks || []).map(block => `<button type="button" class="block-box ${state.selected.has(block.id) ? 'selected' : ''}" data-block-id="${block.id}" title="${escapeHTML(block.text)}${block.confidence == null ? '' : ` · 置信度 ${Math.round(block.confidence*100)}%`}" aria-label="选择识别文字：${escapeHTML(block.text)}" aria-pressed="${state.selected.has(block.id)}" style="${boxStyle(block.box,page)}" ${state.draw ? 'tabindex="-1"' : ''}></button>`).join('')}${regions.map(region => `<span class="region-box" style="${boxStyle(region.box,page)}"></span>`).join('')}</div></div><div class="image-toolbar"><button class="btn small ${state.draw ? 'primary' : ''}" data-action="toggle-draw" aria-pressed="${state.draw === true}">${state.draw ? '✓ 框选模式' : '框选区域'}</button><button class="btn small primary" data-action="associate" ${state.busy ? 'disabled' : ''}>关联所选文字 → 第 ${q?.number ?? '—'} 题</button><button class="text-button" data-action="clear-selection">取消选择</button></div><p class="image-help">${state.draw ? '在图片上按住并拖动，松开后加入当前题目的区域。可跨页多次框选。' : '蓝框为识别文字，绿框为当前题目关联区域。点选多个文字块后点击关联。'}</p><div class="selected-text" id="selected-text"></div>${page.needs_review ? '<div class="notice warn">本页尚未处理；可运行 OCR，或人工逐题核对后点击下方按钮。</div><button class="btn small" data-action="ack-page">已人工核对本页全部作答</button>' : ''}${uploadQueueHTML()}${uploadHTML()}`;
  const scroll=$('#photo-scroll'),key=`${sub.id}:${page.id}:${photoRotation(page)}`;
  if (visualFrame?.version === state.routeVersion && visualFrame.question === q?.question_id && visualFrame.page_id === page.id) {
    $('#photo-stage').insertAdjacentHTML('beforeend',`<span class="region-box visual-frame" style="${boxStyle(visualFrame.box,page)}"></span>`);
  }
  if (state.draw === 'visual') $('.image-help',panel).textContent = '拖动框选本题手写作答，然后核对范围并发送复读。';
  scroll.dataset.photoKey=key;scroll.dataset.question=String(state.activeQuestion);
  if (viewport?.key===key && viewport.question===String(state.activeQuestion)) scroll.scrollTo({top:viewport.top,left:viewport.left,behavior:'instant'});
  else locateQuestionRegion();
  updateSelectedText(); renderUnassignedOCR();updateReviewLock();
}
function uploadQueueHTML() {
  const pages = state.submission.pages || [];
  return pages.length ? `<div class="upload-queue" aria-label="照片识别队列">${pages.map((p,i) => `<div class="queue-item"><span class="queue-index">${i+1}</span><span class="queue-name">第 ${i+1} 页 · ${p.answer_layout === 'separate' ? '独立答题卡' : '原文作答'}</span><span class="badge ${p.status === 'done' ? 'green' : p.status === 'error' ? 'red' : p.status === 'running' ? 'blue' : 'amber'}">${({uploaded:'待识别',running:'识别中',done:'已识别',error:'识别失败'}[p.status] || p.status)}</span>${p.status === 'error' ? `<button class="text-button" data-action="retry-page" data-id="${p.id}">重试</button>` : ''}</div>`).join('')}</div>` : '';
}
function uploadHTML() {
  if(state.submission?.identity?.class_archived)return '';
  return `<fieldset style="border:0;padding:0;margin:0;min-width:0" ${state.busy || isGrading(state.submission) ? 'disabled' : ''}>${answerLayoutField('page-answer-layout')}${uploadZone({id:'page-upload',multiple:true,note:isGrading(state.submission)?'本轮批改结束后可追加':'按选择顺序上传'})}</fieldset>`;
}
function renderPageUploadStatus() {
  const target=$('#page-upload-status'),result=state.pageUpload;
  if (!target) return;
  target.hidden=!result || result.sid!==state.submission?.id;
  let message=result?.summary || result?.message || '';
  if (result?.summary && result.hasSavedPages) {
    message+=isGrading(state.submission) ? ' 已交给后台，完成后自动更新成绩。' : state.submission.is_final ? ' 本轮批改已完成。' : state.submission.grading_state==='failed' ? ' 本轮批改失败，原图已保存，可重试自动批改。' : state.submission.processing_job?.state==='interrupted' ? ' 本轮处理已中断，原图已保存，可继续后台批改。' : result.error ? ` 照片已保存，后台提交或状态读取暂未确认：${result.error}。可点击“开始自动批改”继续，无需重新上传已保存照片。` : ' 照片已保存，正在准备后台批改。';
  } else if (result?.error) {
    message+=` 当前照片状态暂未核实：${result.error}。请刷新后核对照片队列，不要整批重传。`;
  }
  target.textContent=target.hidden ? '' : message;
  target.className=`notice ${result?.warning || result?.error && !isGrading(state.submission) && !state.submission.is_final ? 'warn' : ''}`;
  target.style.overflowWrap='anywhere';
}
function updateSelectedText() {
  const el = $('#selected-text');
  if (el) el.textContent = (currentPage()?.blocks || []).filter(block => state.selected.has(block.id)).map(block => block.text).join('\n') || '尚未选择文字块';
}
function captureDraft(card, changes = {}) {
  const id = Number(card.dataset.questionId);
  const q = state.submission.reviews.find(item => item.question_id === id);
  const existing = reviewDraft(q);
  if (!$('[data-field="transcript"]',card)) return existing;
  const rawScore = $('[data-field="score_override"]',card).value;
  const blank=$('[data-field="confirmed_blank"]',card).checked;
  const draft = {...existing,transcript:blank?'':$('[data-field="transcript"]',card).value,confirmed_blank:blank,score_override:rawScore === '' ? null : Number(rawScore),reason:$('[data-field="reason"]',card).value,...changes};
  const saved=reviewDraft(q,false),changed=Object.keys(saved).some(key=>JSON.stringify(saved[key])!==JSON.stringify(draft[key]));
  if(changed) {
    state.drafts.set(id,draft);
    const badge = $('[data-review-state]',card);
    badge.textContent = '未保存'; badge.className = 'badge amber';badge.hidden=false;badge.title='有未保存修改';
    $('.review-head',card).setAttribute('aria-label',`查看第 ${q.number} 题 · 有未保存修改`);
    $('.diff',card).textContent = '已修改，请保存后更新差异与得分。';
  }else{
    state.drafts.delete(id);
    // Reuse the saved rendering for labels without replacing the focused input.
    const savedView=document.createElement('template');savedView.innerHTML=reviewHTML(q);
    $('[data-review-state]',card).replaceWith($('[data-review-state]',savedView.content));
    $('.review-head',card).setAttribute('aria-label',$('.review-head',savedView.content).getAttribute('aria-label'));
    $('.diff',card).innerHTML=$('.diff',savedView.content)?.innerHTML || '';
  }
  state.reviewSave=null;renderDraftActions();
  return draft;
}
function renderDraftActions() {
  const panel=$('#review-drafts');if(!panel)return;
  const count=state.drafts.size,result=state.reviewSave;
  panel.hidden=!count&&!result;
  panel.classList.toggle('warn',!!result?.error);
  const message=result?.running?`正在保存，已确认 ${result.saved} / ${result.total} 题。`:result?.error?`已确认保存 ${result.saved} 题，${count} 题仍保留草稿。${result.error}`:result?`已保存 ${result.saved} 题，${state.submission.is_final?'成绩与修改记录已更新':'草稿已保存，可继续核对'}。`:`有 ${count} 题修改尚未保存，离开前请保存。`;
  const status=$('#review-drafts-message');if(status.textContent!==message)status.textContent=message;
  const button=$('[data-action="save-all-reviews"]',panel),label=`保存本答卷全部修改（${count} 题）`;
  button.hidden=!count;if(button.textContent!==label)button.textContent=label;
  button.disabled=state.busy||isGrading(state.submission);
  setButtonBusy(button,!!result?.running);
}
async function saveAllReviews() {
  if(isGrading(state.submission))throw new Error('答卷正在处理，完成后可修改。');
  const entries=state.submission.reviews.filter(q=>state.drafts.has(q.question_id)).sort((a,b)=>a.number-b.number).map(q=>[q.question_id,reviewDraft(q)]);
  if(!entries.length)return;
  const result=state.reviewSave={total:entries.length,saved:0,running:true,error:''};
  let currentId=null;
  updateReviewLock();renderDraftActions();
  try {
    for(const [id,draft] of entries){currentId=id;await saveReview(id,false,draft,false);result.saved++;renderDraftActions();}
  }catch(error){
    const question=state.submission.reviews.find(q=>q.question_id===currentId);
    result.error=`第 ${question?.number ?? currentId} 题：${error.message}`;
    if(!reviewQuestions().some(q=>q.question_id===currentId))state.filter='pending';
    selectReviewQuestion(currentId);
  }finally{
    result.running=false;
    if(attentionContext&&result.saved)await refreshAttentionQueue();
    renderSubmission();
  }
  toast(result.error?`已确认保存 ${result.saved} 题，其余输入保留，请核对提示。`:`已保存 ${result.saved} 题${state.submission.is_final?'，成绩已更新':'草稿'}。`,!!result.error);
}
async function saveReview(id, confirmed, customDraft, refresh = true) {
  if (isGrading(state.submission)) throw new Error('答卷正在处理，完成后可修改。');
  const q = state.submission.reviews.find(item => item.question_id === id);
  const card = $(`[data-question-id="${id}"]`);
  const draft = customDraft || (card ? captureDraft(card) : reviewDraft(q));
  if (draft.score_override !== null && (!Number.isFinite(draft.score_override) || draft.score_override < 0 || draft.score_override > q.points)) throw new Error(`手动分数须在 0 至 ${q.points} 之间。`);
  if (draft.score_override !== null && !draft.reason.trim()) throw new Error('填写手动分数时，必须说明改分理由。');
  if ((confirmed || q.auto_result) && !draft.transcript.trim() && !draft.confirmed_blank) throw new Error('转录为空。请对照原图填写作答，或勾选“确认本题未作答”。');
  state.drafts.set(id,draft);
  const fields = card ? $$('input,textarea,select',card) : [], disabled = fields.map(el => el.disabled);
  fields.forEach(el => { el.disabled = true; });
  let updated;
  try { updated = await api(`/api/submissions/${state.submission.id}/reviews/${id}`,{method:'POST',body:{...draft,confirmed}}); }
  finally { fields.forEach((el,index) => { if (el.isConnected) el.disabled = disabled[index]; }); }
  state.drafts.delete(id); state.submission = updated;
  if(refresh) {
    if(attentionContext)await refreshAttentionQueue();
    renderReviews();renderPhoto();
  }
}
async function quickDecision(id, decision, suggestionId) {
  if (isGrading(state.submission)) throw new Error('答卷正在处理，完成后可修改。');
  if (hasDirty()) throw new Error('有未保存修改，请先保存，或放弃修改后再直接判定。');
  $$('.quick-verdict button').forEach(button => {button.disabled=true;});
  const fields=$$('.review-card.active input,.review-card.active textarea,.review-card.active select');
  const disabled=fields.map(field=>field.disabled);
  fields.forEach(field=>{field.disabled=true;});
  const origin=location.pathname+location.search;
  try {
    const body = suggestionId ? {visual_suggestion_id:suggestionId} : {quick_decision:decision};
    state.submission = await api(`/api/submissions/${state.submission.id}/reviews/${id}`,{method:'POST',body});
    if(state.submission.undo)setReviewUndo({...state.submission.undo,submission_id:state.submission.id,name:state.submission.identity?.name||state.submission.student_code,origin});
  }
  finally {fields.forEach((field,i)=>{if(field.isConnected)field.disabled=disabled[i];});}
  state.selected.clear();
  if (attentionContext) {
    state.busy=false;
    await nextAttention(true);
  } else {
    if(!reviewQuestions().some(q=>q.question_id===id))state.filter='all';
    selectReviewQuestion(id);
    renderSubmission();
  }
  toast('判定已保存，成绩已更新。');
}

async function undoReview() {
  const undo=lastReviewUndo;
  if(!undo)return;
  if(undo.submission_id===state.submission?.id && isGrading(state.submission))throw new Error('答卷正在处理，完成后可撤销判定。');
  if(hasDirty())throw new Error('有未保存修改，请先保存或放弃修改，再撤销上一条判定。');
  await api(`/api/submissions/${undo.submission_id}/reviews/${undo.question_id}/undo`,{method:'POST',body:{event_id:undo.event_id}});
  setReviewUndo(null);
  const target=new URL(undo.origin,location.origin);target.searchParams.set('question',undo.question_id);
  target.searchParams.delete('filter');
  state.busy=false;
  await navigate(target.pathname+target.search,true);
  toast('已撤销上一条判定，原成绩与识别文字已恢复。');
}
function focusQuestion(id) {
  if (state.busy) return;
  if (!reviewQuestions().some(q => q.question_id === id)) return;
  selectReviewQuestion(id);
  renderSubmission();
  locateQuestionRegion();
}
function stripQuestionPrefix(text, number) {
  return text.replace(new RegExp(`^\\s*(?:[（(]\\s*${number}\\s*[)）]|${number}\\s*[.．、:：)）])\\s*`),'');
}
async function associate(blocks, box = null) {
  if (isGrading(state.submission)) throw new Error('答卷正在处理，完成后可修改。');
  const q = state.submission.reviews.find(item => item.question_id === state.activeQuestion);
  if (!q) throw new Error('请先选择右侧的题目。');
  if (!blocks.length && !box) throw new Error('请先选择文字块，或框选作答区域。');
  const card = $(`[data-question-id="${q.question_id}"]`);
  const d = card ? captureDraft(card) : {...reviewDraft(q)};
  const regionBox = box || [Math.min(...blocks.map(b => b.box[0])),Math.min(...blocks.map(b => b.box[1])),Math.max(...blocks.map(b => b.box[2])),Math.max(...blocks.map(b => b.box[3]))];
  const region = {page_id:state.pageId,box:regionBox.map(Math.round),block_ids:blocks.map(b => b.id)};
  const text = stripQuestionPrefix(blocks.map(b => b.text).join('\n'),q.number);
  const next = {...d,regions:[...d.regions,region],transcript:[d.transcript,text].filter(Boolean).join('\n'),confirmed_blank:false};
  state.drafts.set(q.question_id,next);
  await saveReview(q.question_id,false,next);
  state.selected.clear(); renderPhoto(); toast('已关联并保存，仍需人工复核。');
}
async function busyAction(button, action) {
  if (state.busy) return;
  state.busy = true;
  if (button) button.disabled = true;
  setButtonBusy(button,true);clearActionError(button);
  try { await action(); }
  catch (error) { toast(error.message,true);showActionError(button,error); }
  finally { state.busy = false; state.ocrProgress = null;setButtonBusy(button,false); if (button?.isConnected) button.disabled = false; if (state.submission && $('#photo-panel')) renderPhoto();updateReviewLock(); }
}

document.addEventListener('click', async event => {
  const link = event.target.closest('a[data-nav]');
  if (link && (!link.target||link.target==='_self') && !event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey && event.button === 0) { event.preventDefault(); await navigate(link.pathname+link.search+link.hash); return; }
  const blockButton = event.target.closest('[data-block-id]');
  if (blockButton && !state.draw) {
    const id = Number(blockButton.dataset.blockId);
    state.selected.has(id) ? state.selected.delete(id) : state.selected.add(id);
    blockButton.classList.toggle('selected',state.selected.has(id)); blockButton.setAttribute('aria-pressed',String(state.selected.has(id))); updateSelectedText(); return;
  }
  const button = event.target.closest('[data-action]');
  if (!button) return;
  const action = button.dataset.action, id = Number(button.dataset.id);
  if (['filter-recognition','filter-attention','clear-recognition-search','reset-recognition-list'].includes(action)) return;
  if(action==='return-context'){await Nav.back();return;}
  if (state.busy) return;
  if (action === 'undo-review') { await busyAction(button,undoReview);return; }
  if (action === 'undo-ai-review') {
    await busyAction(button, async () => {
      if(hasDirty()) throw new Error('请先保存或放弃未保存的修改。');
      const data=await api(`/api/submissions/${state.submission.id}/reviews/${id}/undo`,
        {method:'POST',body:{event_id:Number(button.dataset.eventId)}});
      state.submission=data;state.drafts.clear();renderSubmission();
    });
    return;
  }
  if (action === 'dismiss-undo') { setReviewUndo(null);return; }
  if (action === 'delete-task') {
    await busyAction(button, async () => {
      const info = await api(`/api/tasks/${id}/deletion-preview`);
      if (!info.can_delete) throw new Error(info.blocked_reason);
      const c = info.counts;
      if (!window.confirm(`删除任务“${info.name}”？\n\n将删除 ${c.questions} 道题、${c.assignments} 个班级任务、${c.submissions} 份答卷（已上传 ${c.uploaded_submissions} 份）、${c.photos} 张照片，以及相关成绩和批改记录。\n班级、学生名单和姓名纠错记忆保留。删除前会自动保存本机备份。${hasDirty() ? '\n本页未保存的修改也会丢弃。' : ''}\n\n确定删除？`)) return;
      const result = await api(`/api/tasks/${id}`, {method:'DELETE', body:{confirmed:true, expected_counts:info.counts}});
      state.drafts.clear(); state.newTaskDirty = false; state.busy = false;
      await navigate('/');
      toast(result.message);
    });
    return;
  }
  if (action === 'reload') { route(); return; }
  if (action === 'preview-answer-word') { await busyAction(button,previewAnswerWord); return; }
  if (action === 'apply-answer-word') { await busyAction(button,applyAnswerWord); return; }
  if (action === 'preview-word') { await busyAction(button,previewWord); return; }
  if (action === 'add-row') { $('#answer-rows').insertAdjacentHTML('beforeend',answerRow(Math.max(0,...$$('#answer-rows .number-input').map(input=>Number(input.value)))+1)); state.newTaskDirty = true; updateEditorTotal(); return; }
  if (action === 'remove-row') { button.closest('tr').remove(); state.newTaskDirty = true; updateEditorTotal(); return; }
  if (action === 'paste-rows') {
    const answers = $('#paste-answers').value.split(/\r?\n/).map(x => x.trim()).filter(Boolean);
    if (!answers.length) { toast('先粘贴答案，一行一题。',true); return; }
    const rows = $$('#answer-rows tr');
    if (rows.length === 1 && !$('.answer-input',rows[0]).value.trim() && !$('.variant-input',rows[0]).value.trim()) rows[0].remove();
    for (const answer of answers) $('#answer-rows').insertAdjacentHTML('beforeend',answerRow($$('#answer-rows tr').length+1,answer));
    $('#paste-answers').value = ''; state.newTaskDirty = true; updateEditorTotal(); return;
  }
  if (action === 'toggle-evidence') { selectReviewQuestion(state.activeQuestion === null ? reviewQuestions()[0]?.question_id ?? null : null); renderSubmission(); return; }
  if (action === 'focus-question') { focusQuestion(id); return; }
  if (action === 'visual-select') {
    state.draw = state.draw === 'visual' ? false : 'visual';
    visualFrame = visualFeedback = null;
    renderPhoto();renderVisualReview();
    if (state.draw) $('#photo-scroll')?.scrollIntoView({block:'nearest'});
    return;
  }
  if (action === 'visual-run') { await requestVisualReread(id); return; }
  if (action === 'visual-adopt') {
    const suggestion = state.submission.visual_suggestions?.[id];
    if (suggestion?.can_adopt) await busyAction(button,()=>quickDecision(id,null,suggestion.id));
    return;
  }
  if (action === 'locate-region') { locateQuestionRegion(); return; }
  if (action === 'defer-review') { if(attentionContext){await nextAttention();return;}const next = nextPriorityQuestion(state.submission.reviews, id); if (next) focusQuestion(next.question_id); else toast('没有其他待处理题目。'); return; }
  if (action === 'rotate-photo') { const page = currentPage(); if (page) { photoRotations.set(page.id,(photoRotation(page)+90)%360); visualFrame=null; renderPhoto(); renderVisualReview(); } return; }
  if (action === 'next-attention') { await nextAttention(); return; }
  if (action === 'retry-attention') {
    const context=attentionContext,version=state.routeVersion;
    await busyAction(button,async()=>{
      button.textContent='正在读取…';
      await refreshAttentionQueue();
      if(context===attentionContext && version===state.routeVersion) {
        const panel=$('.attention-navigation');if(panel)panel.outerHTML=attentionNavigation();
      }
    });return;
  }
  if (action === 'toggle-draw') { state.draw = state.draw !== true; renderPhoto(); renderVisualReview(); return; }
  if (action === 'clear-selection') { state.selected.clear(); renderPhoto(); return; }
  if (action === 'clear-regions') {
    const card = button.closest('.review-card');
    captureDraft(card,{regions:[]});renderReviews();renderPhoto();toast(state.drafts.has(id)?'已清除本地关联，请保存修改。':'本题与已保存内容一致。');return;
  }
  await busyAction(button,async () => {
    if (action === 'save-all-reviews') { await saveAllReviews();return; }
    if (action.startsWith('quick-') && ['correct','incorrect','blank'].includes(action.slice(6))) {
      await quickDecision(id,action.slice(6)); return;
    }
    if (['repair-recognition','auto-grade','reextract-answers','retry-missing','retry-page','ack-page'].includes(action) && state.drafts.size) throw new Error('有未保存的题目修改，请先保存后再处理照片。');
    if (action === 'ack-page') {
      if (!window.confirm('确认已对照本页原图，核对全部作答？此操作不会自动确认题目或改变转录。')) return;
      state.submission = await api(`/api/pages/${state.pageId}/reviewed`,{method:'POST',body:{confirmed:true}});
      renderSubmission(); toast('本页已标记人工核对，题目仍须逐题确认。');
    }
    if (action === 'save-next-attention') { const previousItems=attentionQueue?.items || []; await saveReview(id,true); state.busy = false; await nextAttention(false,id,previousItems); return; }
    if (action === 'save-review' || action === 'confirm-review') { const confirming = action === 'confirm-review'; await saveReview(id, confirming); toast(state.submission.reviews.find(q => q.question_id === id).auto_result ? '修改已保存，最终成绩已立即更新。' : confirming ? '本题已确认。' : '草稿已保存。'); if(!reviewQuestions().some(q=>q.question_id===id))state.filter='all';selectReviewQuestion(id); renderSubmission(); }
    if (action === 'associate') await associate((currentPage().blocks || []).filter(block => state.selected.has(block.id)));
    if (['repair-recognition','auto-grade','reextract-answers','retry-missing','retry-page'].includes(action)) {
      state.ocrProgress = action === 'repair-recognition' ? '正在提交后台识别任务…' : action === 'reextract-answers' ? '正在使用已保存的识别文字重新分题，不调用识别服务。' : action === 'retry-missing' ? '正在提交局部补识别任务…' : '正在提交后台批改任务…'; renderPhoto();
      try { await autoGrade(state.submission.id,action === 'repair-recognition' || action === 'auto-grade' && (state.submission.is_final || state.submission.status === 'confirmed'),action === 'reextract-answers' ? {reextract:true} : action === 'retry-missing' ? {retry_missing:true} : {}); toast(isGrading(state.submission) ? '已交给后台批改，可以离开页面。' : '处理完成；教师修改已保留。'); }
      catch (error) {
        try { state.submission = await api(`/api/submissions/${state.submission.id}`); } catch {}
        renderSubmission(); throw error;
      }
    }

  });
});

document.addEventListener('submit', async event => {
  const form = event.target;
  if (!['new-task-form','new-student-form'].includes(form.id)) return;
  event.preventDefault();
  await busyAction($('button[type="submit"]',form), async () => {
    if (form.id === 'new-task-form') await createTask(form);
    else {
      const data = await api(`/api/tasks/${form.dataset.taskId}/submissions`,{method:'POST',body:{student_code:form.elements.student_code.value.trim()}});
      state.busy = false; await navigate(`/submissions/${data.id}`);
    }
  });
});
document.addEventListener('change', event => {
  if (event.target.matches('[data-answer-word-all]')) {
    $$('[data-answer-word-choice]', event.target.closest('fieldset')).forEach(box => {box.checked=event.target.checked;});
  } else if (event.target.matches('[data-answer-word-choice]')) {
    const group = event.target.closest('fieldset');
    const boxes = $$('[data-answer-word-choice]', group);
    const all = $('[data-answer-word-all]', group);
    all.checked = boxes.every(box => box.checked);
    all.indeterminate = !all.checked && boxes.some(box => box.checked);
  }
});
document.addEventListener('input', event => {
  if (event.target.matches('#answer-word-preview textarea:not([readonly])')) state.newTaskDirty = true;
  if (event.target.closest('#new-task-form') && !event.target.closest('#answer-word-preview')) { state.newTaskDirty = true; updateEditorTotal(); if(event.target.id !== 'word-confirmed') $('#word-confirmed').checked = false; }
  const card = event.target.closest('.review-card');
  if (card && event.target.dataset.field) captureDraft(card);
  if (event.target.id === 'image-zoom') { state.zoom = Number(event.target.value); $('#photo-stage').style.width = `${state.zoom}%`; }
});
document.addEventListener('change', async event => {
  const el = event.target;
  if (el.matches('.upload-input')) updateUploadFiles(el);
  if (el.id === 'review-filter') { if (state.busy) { el.value = state.filter; return; } state.filter = el.value; state.activeQuestion = null; renderSubmission(); }
  if (el.id === 'review-order') { if (state.busy) { el.value = state.reviewOrder; return; } state.reviewOrder = el.value; renderReviews(); }
  if (el.id === 'page-select') { state.pageId = Number(el.value); state.selected.clear(); visualFrame=null; renderPhoto(); renderVisualReview(); }
  if (el.dataset.field === 'confirmed_blank') {
    const card = el.closest('.review-card');
    captureDraft(card);
    renderReviews();
  }
  if (el.id === 'page-upload') {
    const files = [...el.files], layout = $('#page-answer-layout').value;
    await busyAction(el,async () => {
      if (state.drafts.size) throw new Error('请先保存题目修改，再上传照片并自动批改。');
      if (isGrading(state.submission)) throw new Error('答卷正在批改，请结束后再追加照片。');
      if (!files.length) return;
      const sid=state.submission.id,version=state.routeVersion,failures=[],originalPages=state.submission.pages.length;
      const result=state.pageUpload={sid,message:'正在上传照片…',warning:false};
      let uploaded=0,attempted=0;
      for (let i=0;i<files.length;i++) {
        if (version !== state.routeVersion) return;
        const file=files[i];result.message=`正在保存第 ${i+1} / ${files.length} 张：${file.name}`;renderPageUploadStatus();
        try {
          if (!/\.(jpe?g|png)$/i.test(file.name) || file.size > 10*1024*1024) throw new Error('仅支持 10 MB 以内的 JPG、PNG 图片');
          const body = new FormData(); body.append('image',file); body.append('answer_layout',layout);
          attempted++;
          const page = await api(`/api/submissions/${sid}/pages`,{method:'POST',body});
          if (version !== state.routeVersion) return;
          if (!state.submission.pages.some(p=>p.id===page.id)) state.submission.pages.push(page);
          Object.assign(state.submission,{is_final:false,status:'pending',grading_state:'idle',recognition:null});
          state.pageId=page.id;uploaded++;
        } catch (error) {
          if (version !== state.routeVersion) return;
          failures.push(`${file.name}：${error.message}`);
        }
      }
      el.value='';
      const summary=`已确认保存 ${uploaded} / ${files.length} 张${failures.length ? `；未成功或未确认 ${failures.length} 张：${failures.join('；')}。请求未取得确认时可能已保存，请先核对照片队列，不要整批重传。` : '。'}`;
      result.summary=summary;result.hasSavedPages=uploaded>0;result.warning=failures.length>0;
      if (uploaded) state.activeQuestion=null;
      renderSubmission();
      if (uploaded) {
        try {
          await autoGrade(sid);
          if (version !== state.routeVersion) return;
        } catch (error) {
          if (version !== state.routeVersion) return;
          result.error=error.message;
          pollGrading(sid,version);
        }
        renderSubmission();
      } else if (attempted) {
        try {
          const latest=await api(`/api/submissions/${sid}`);
          if (version !== state.routeVersion) return;
          state.submission=latest;
          result.hasSavedPages=latest.pages.length>originalPages;
          if (result.hasSavedPages) result.error='本次保存响应未确认，尚未自动提交批改，请先核对照片队列';
          if (isGrading(latest)) pollGrading(sid,version);
        } catch (error) {
          if (version !== state.routeVersion) return;
          result.error=error.message;
        }
        renderSubmission();
      }
      renderPageUploadStatus();toast(uploaded ? '照片保存结果已显示，后台进度见上方。' : '本次未确认保存新照片，请核对上传结果。',!!(result.warning || result.error));
    });
  }
});
document.addEventListener('keydown', event => {
  if (event.defaultPrevented || event.isComposing || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey || state.busy) return;
  if ((event.key === 'Enter' || event.key === ' ') && event.target.matches('.review-head')) { event.preventDefault(); focusQuestion(Number(event.target.dataset.id)); return; }
  if (!state.submission || event.target.closest('input,textarea,select,button,a,summary,[role="button"],[role="link"]') || event.target.isContentEditable) return;
  const questions = reviewQuestions(), current = questions.findIndex(q => q.question_id === state.activeQuestion);
  if (event.key === 'ArrowLeft') { event.preventDefault(); const q = questions[Math.max(0,current-1)]; if (q) focusQuestion(q.question_id); }
  if (event.key === 'ArrowRight') { event.preventDefault(); const q = questions[Math.min(questions.length-1,current+1)]; if (q) focusQuestion(q.question_id); }
  if (event.key.toLowerCase() === 'n') { event.preventDefault(); const q = nextPriorityQuestion(state.submission.reviews,state.activeQuestion); if (q) focusQuestion(q.question_id); else toast('没有其他待处理题目。'); }
  if (event.key === 'Enter' && state.activeQuestion && $('.review-details[open]')) { event.preventDefault(); $(`[data-action=confirm-review][data-id=\"${state.activeQuestion}\"]`)?.click(); }
});

let drag = null;
document.addEventListener('pointerdown', event => {
  const stage = event.target.closest('#photo-stage');
  if (!stage || !state.draw || state.busy || isGrading(state.submission) || event.button !== 0) return;
  event.preventDefault();
  const rect = stage.getBoundingClientRect();
  const point = [Math.max(0,Math.min(rect.width,event.clientX-rect.left)),Math.max(0,Math.min(rect.height,event.clientY-rect.top))];
  drag = {stage,rect,start:point,pointer:event.pointerId};
  stage.setPointerCapture(event.pointerId);
  stage.insertAdjacentHTML('beforeend','<span class="draw-box" id="draw-box"></span>');
});
document.addEventListener('pointermove', event => {
  if (!drag || drag.pointer !== event.pointerId) return;
  const {rect,start} = drag;
  const point = [Math.max(0,Math.min(rect.width,event.clientX-rect.left)),Math.max(0,Math.min(rect.height,event.clientY-rect.top))];
  drag.end = point;
  const box = $('#draw-box');
  if (box) box.style.cssText = `left:${Math.min(start[0],point[0])}px;top:${Math.min(start[1],point[1])}px;width:${Math.abs(point[0]-start[0])}px;height:${Math.abs(point[1]-start[1])}px`;
});
document.addEventListener('pointerup', async event => {
  if (!drag || drag.pointer !== event.pointerId) return;
  const {stage,rect,start,end} = drag; drag = null;
  $('#draw-box')?.remove();
  if (!stage.isConnected || !end || Math.abs(end[0]-start[0]) < 5 || Math.abs(end[1]-start[1]) < 5) return;
  const page = currentPage();
  const [width,height] = displaySize(page);
  const box = originalBox([Math.min(start[0],end[0])/rect.width*width,Math.min(start[1],end[1])/rect.height*height,Math.max(start[0],end[0])/rect.width*width,Math.max(start[1],end[1])/rect.height*height],page);
  if (state.draw === 'visual') {
    visualFrame = {version:state.routeVersion,question:state.activeQuestion,page_id:page.id,
      box:box.map(Math.round),rotation:photoRotation(page)};
    state.draw = false;
    renderPhoto();renderVisualReview();
    $('[data-action="visual-run"]')?.focus({preventScroll:true});
    return;
  }
  const blocks = (page.blocks || []).filter(b => b.box[0] < box[2] && b.box[2] > box[0] && b.box[1] < box[3] && b.box[3] > box[1]);
  await busyAction(null,() => associate(blocks,box));
});
document.addEventListener('pointercancel', () => { drag = null; $('#draw-box')?.remove(); });
$('#guide-button').addEventListener('click', () => $('#guide-dialog').showModal());
$('#close-guide').addEventListener('click', () => $('#guide-dialog').close());
$('#guide-dialog').addEventListener('click', event => { if (event.target === $('#guide-dialog')) { const r = event.target.getBoundingClientRect(); if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) event.target.close(); } });
Nav.init();route().then(()=>Nav.restore(Nav.current.view));



document.addEventListener('change',event=>{
 const match=event.target.id.match(/^(task|assignment)-recognition-(filter|order)$/);
 if(match)updateRecognitionView(match[1],match[2]==='filter'?'view':'order',event.target.value);
});
for(const type of ['input','compositionend'])document.addEventListener(type,event=>{
 const scope=event.target.dataset.recognitionSearch;
 if(scope && !event.isComposing)updateRecognitionView(scope,'q',event.target.value);
});
document.addEventListener('click',event=>{
 const button=event.target.closest('[data-action]');if(!button||!recognitionList)return;
 if(button.dataset.action==='filter-recognition'){
  updateRecognitionView(recognitionList.scope,'q','');
  updateRecognitionView(recognitionList.scope,'view',button.dataset.view);
  $(`#${recognitionList.scope==='assignment'?'assignment-students':'task-submissions'}`)?.scrollIntoView({block:'start',behavior:'instant'});
  $(`[data-recognition-search="${recognitionList.scope}"]`)?.focus({preventScroll:true});
 }
 if(button.dataset.action==='filter-attention')updateRecognitionView(recognitionList.scope,'view','attention');
 if(['clear-recognition-search','reset-recognition-list'].includes(button.dataset.action)){
  updateRecognitionView(recognitionList.scope,'q','');
  if(button.dataset.action==='reset-recognition-list')updateRecognitionView(recognitionList.scope,'view','all');
  $(`[data-recognition-search="${recognitionList.scope}"]`)?.focus();
 }
});
