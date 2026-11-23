'use strict';

// Class pages reuse the existing router, request validation and unsaved-change guard.
const classUI = {data:null, classes:[], importSource:null, importPreview:null, batchPhotos:[], batchSummary:null};
const classStates = {not_submitted:['未交',''],ocr_pending:['待识别','amber'],review_pending:['待核对','amber'],completed:['已计分','green'],exempt:['免交','']};
const classLink = (path, text, style = '') => `<a class="btn small ${style}" href="${path}" data-nav>${escapeHTML(text)}</a>`;
const classBadge = value => { const s = classStates[value] || [value || '待处理','']; return badge(s[0],s[1]); };
const classCounts = counts => `<div class="class-counts">${[['expected','应交学生','人'],['submitted','已交学生','人'],['not_submitted','未交学生','人','not_uploaded'],['completed','已计分答卷','份'],['attention_submissions','待核对答卷','份','attention'],['uncertain_questions','识别不确定','题','uncertain'],['failed_submissions','批改失败','份','failed'],['processing_submissions','处理中','份','processing'],['exempt','免交学生','人']].map(([key,label,unit,view]) => {
  const content = `<strong>${numberText(counts?.[key])} <small>${unit}</small></strong><span>${label}</span>`;
  return view ? `<button type="button" data-action="filter-recognition" data-view="${view}" aria-label="查看${label}对应名单" aria-pressed="false">${content}</button>` : `<div>${content}</div>`;
}).join('')}</div>`;
const classTaskStatus = (c,id) => `<div class="recognition-status"><span class="badge ${c.completed&&!c.attention_submissions&&!c.processing_submissions?'green':''}">已计分 ${numberText(c.completed)} 份</span>${c.uncertain_questions?`<a class="badge amber" href="/assignments/${id}?view=uncertain" data-nav>${numberText(c.uncertain_submissions)} 份存在识别不确定，共 ${numberText(c.uncertain_questions)} 题</a>`:''}${c.failed_submissions?`<span class="badge red">批改失败 ${numberText(c.failed_submissions)} 份</span>`:''}${c.processing_submissions?`<span class="badge blue">处理中 ${numberText(c.processing_submissions)} 份</span>`:''}${c.completed&&!c.attention_submissions&&!c.processing_submissions?'<span class="tiny muted">未发现识别异常，可按需抽查</span>':''}</div>`;
const classStatus = student => student.state || (student.status === 'confirmed' ? 'completed' : student.pages?.length ? 'review_pending' : 'not_submitted');
const classScore = student => student.score == null || ['not_submitted','exempt'].includes(classStatus(student)) ? '—' : `${classStatus(student) === 'completed' ? '最终' : '暂定'} ${numberText(student.score)} / ${numberText(student.total_points)}`;
const classMemberActive = member => !!member.active && member.student_active !== 0 && member.student_active !== false;
const classHistoryAction = student => student.submission_id || student.id ? classLink(`/submissions/${student.submission_id || student.id}`,'查看答卷') : classLink(`/assignments/${student.assignment_id}`,'查看班级任务');

function identityMatchHTML(photo) {
  const details = photo.match_details;
  const method = details?.method;
  const labels = {exact:['精确关联','blue'],band:[`学号栏匹配 · ${details?.identity_band?.code || ''}`,'blue'],memory:['教师纠错记忆 · 请核对','blue'],inferred:['推断关联 · 请核对','amber'],duplicate:['重复 / 已占用 · 待确认','amber'],pending:['待确认归属','amber'],manual:['教师确认','green'],corrected:['教师更正','green']};
  const label = labels[method];
  const band = details?.identity_band;
  const bandHTML = band ? `<details class="tiny"><summary>学号栏读数</summary>状态：${escapeHTML({valid:'有效',incomplete:'不完整',conflict:'冲突',template_mismatch:'模板不符',absent:'未读到'}[band.state] || band.state)}；读作“${escapeHTML(band.observed_suffix ?? '—')}”${band.code ? `，拼接为 ${escapeHTML(band.code)}` : ''}；模板 ${escapeHTML(band.format_code || '—')}${band.raw_text ? `<br>原文：${escapeHTML(band.raw_text)}` : ''}${details?.observed_name ? `<br>姓名线索：${escapeHTML(details.observed_name)}` : ''}</details>` : '';
  return `${label ? `<span class="badge ${label[1]}">${escapeHTML(label[0])}</span>` : ''}${details?.reason ? `<p class="notice ${method === 'inferred' || method === 'pending' || method === 'duplicate' ? 'warn' : ''}">${escapeHTML(details.reason)}</p>` : ''}${bandHTML}${method === 'memory' ? `<div class="tiny">记忆依据：${(details.source_photo_ids || []).map(id => `<a href="/api/batch-photos/${Number(id)}/image" target="_blank" rel="noreferrer">教师确认照片 #${Number(id)}</a>`).join('、')}</div>` : ''}`;
}
function inferredPhoto(photo) {
  return !!photo?.submission_id && photo.identity_source === 'machine' && ['inferred','memory','band'].includes(photo.match_details?.method);
}
function identityDifferenceHTML(d) {
  if (d.op === 'suffix') return `忽略姓名末尾数字（识别后缀：${escapeHTML(d.observed || '无')}；名单后缀：${escapeHTML(d.expected || '无')}）`;
  if (d.op === 'insert') return `第 ${Number(d.index)+1} 字处补入“${escapeHTML(d.expected)}”`;
  if (d.op === 'delete') return `去掉第 ${Number(d.index)+1} 字“${escapeHTML(d.observed)}”`;
  return `第 ${Number(d.index)+1} 字“${escapeHTML(d.observed)}” → “${escapeHTML(d.expected)}”`;
}
function identityCandidatesHTML(candidates) {
  const list = values => `<ul>${values.map(c => `<li><button type="button" class="btn small" data-identity-select="${Number(c.id)}">${escapeHTML(c.student_code)} ${escapeHTML(c.name)}</button>${c.observed_name ? ` · 机器读作“${escapeHTML(c.observed_name)}”` : ''}：${c.differences?.length ? c.differences.map(identityDifferenceHTML).join('；') : c.observed_name && c.observed_name !== c.name ? '教师确认的姓名对应' : '姓名一致'}${c.already_matched ? '（已有照片）' : ''}</li>`).join('')}</ul>`;
  const primary = candidates.filter(c => !c.supplementary), extra = candidates.filter(c => c.supplementary);
  return candidates.length ? `<div class="notice"><strong>候选学生与姓名差异</strong>${list(primary)}${extra.length ? `<details><summary>更多相近候选（${extra.length}，仅供人工选择）</summary>${list(extra)}</details>` : ''}</div>` : '';
}
function memoryNameCandidates(photo) {
  const text = String(photo.identified_text || '');
  const cn = [...new Set([...text.matchAll(/[\u4e00-\u9fff·]{2,4}/g)].map(m => m[0]))].sort((a, b) => b.length - a.length);
  const codes = [...new Set([...text.matchAll(/(?<![0-9A-Za-z])[0-9０-９]{1,6}(?![0-9A-Za-z])/g)].map(m => m[0]))];
  return [...cn, ...codes];
}
function identityLearningHTML(photo) {
  let name = photo.match_details?.teacher_observed_name || photo.match_details?.observed_name || (photo.identity_names?.length === 1 ? photo.identity_names[0] : '');
  let auto = false;
  if (!name) { const candidates = memoryNameCandidates(photo); if (candidates.length) { name = candidates[0]; auto = true; } }
  const band = photo.match_details?.method === 'band' || photo.match_details?.identity_band?.state === 'valid';
  return `<div class="identity-learning form-grid"><label class="inline-check"><input type="checkbox" name="remember_identity" ${band ? '' : 'checked'}>记住此姓名纠错，下次同班自动匹配</label>${band ? '<p class="tiny muted">这张照片按学号栏匹配；学号读错不是姓名错误，默认不保存姓名记忆。</p>' : ''}<label class="field">要记住的识别姓名<input name="observed_name" value="${escapeHTML(name || '')}" maxlength="40" pattern="[\u4e00-\u9fff·A-Za-z0-9０-９]{1,40}" placeholder="可填照片里被误读的姓名或学号；留空则不保存">${auto ? '<p class="tiny">已按识别内容自动填入候选，请核对。</p>' : ''}</label><p class="tiny" data-memory-preview>${escapeHTML(name || '识别姓名')} → 请先选择实际学生</p><p class="muted tiny">只保存本班姓名对应，不训练 OCR；可在班级“姓名纠错记忆”中停用。</p></div>`;
}
function identityPayload(form) {
  return {assignment_student_id:Number(form.elements.assignment_student_id.value),confirmed:form.elements.confirmed.checked,
    remember_identity:form.elements.remember_identity.checked,observed_name:form.elements.observed_name.value.trim(),
    append_page:!!form.elements.append_page?.checked,auto_grade:true};
}
function updateIdentityLearning(form) {
  if (!form?.elements.remember_identity) return;
  const append=form.elements.append_page,existing=form.elements.assignment_student_id.selectedOptions[0]?.dataset.hasPages==='1';
  if (append) { append.disabled=!existing;append.required=existing;if(!existing)append.checked=false;form.querySelector('[data-append-confirm]').hidden=!existing; }
  const remembered = form.elements.remember_identity.checked;
  form.elements.observed_name.disabled = !remembered;
  const selected = form.elements.assignment_student_id.selectedOptions[0];
  const observed = form.elements.observed_name.value.trim();
  $('[data-memory-preview]',form).textContent = !remembered ? '本次仅确认归属，不保存姓名记忆。'
    : `${observed || '识别姓名'} → ${selected?.value ? selected.textContent.replace(/（.*$/, '') : '请先选择实际学生'}${observed ? '' : '；留空则不保存记忆'}`;
}

async function openIdentityMemories(cid) {
  const [data,history] = await Promise.all([api(`/api/classes/${cid}/identity-memories`),api(`/api/classes/${cid}/identity-memories/candidates`)]);
  classUI.memoryClass = cid;
  classUI.memories = data.memories;
  const sourceLinks = ids => ids.map(id => `<a href="/api/batch-photos/${Number(id)}/image" target="_blank" rel="noreferrer">照片 #${Number(id)}</a>`).join('、');
  classDialog('姓名纠错记忆', `<div class="form-grid"><p class="notice">同班跨作业使用。这里只记住老师确认的错名，不会训练 OCR 或改变历史成绩。冲突时暂停匹配，请核对后明确保留一个对应。</p><div class="memory-list">${data.memories.map(m => `<article class="memory-item"><strong>${escapeHTML(m.observed_name)} → ${escapeHTML(m.name)}</strong><span class="badge ${m.conflict ? 'red' : m.enabled ? 'green' : ''}">${m.conflict ? '冲突 · 已暂停匹配' : m.enabled ? '已启用' : '已停用'}</span><p class="tiny">依据：${sourceLinks(m.source_photo_ids)}</p><div class="button-row"><button class="btn small" data-class-action="memory-toggle" data-id="${m.id}" ${data.archived ? 'disabled' : ''}>${m.enabled ? '停用记忆' : '启用记忆'}</button>${m.conflict ? `<button class="btn small" data-class-action="memory-resolve" data-id="${m.id}" ${data.archived ? 'disabled' : ''}>仅保留此对应</button>` : ''}</div></article>`).join('') || '<p class="muted">暂无纠错记忆。</p>'}</div><h3>历史纠错预览</h3><p class="tiny muted">勾选并核对后启用；打开预览不会保存。</p><form id="class-memory-import" class="form-grid" data-class-dirty>${history.candidates.map(c => `<label class="memory-candidate"><input type="checkbox" name="memory_photo" value="${c.photo_id}"><span>${escapeHTML(c.observed_name)} → ${escapeHTML(c.name)}（${escapeHTML(c.student_code)}） · ${sourceLinks([c.photo_id])}</span></label>`).join('') || '<p class="muted tiny">没有可直接导入的历史纠错。缺少姓名证据的照片不自动学习。</p>'}<button class="btn primary" type="submit" ${data.archived || !history.candidates.length ? 'disabled' : ''}>启用勾选的历史纠错</button></form></div>`);
}
function identityConfirmationHTML(photo, students, reassign = false) {
  const candidates = photo.match_details?.candidates || photo.candidates || [];
  const available = students.filter(s => !s.exempt && (!reassign || s.id !== photo.suggested_student_id));
  const candidateIds = new Set(candidates.map(c => c.id));
  const selected = !reassign && ['exact','duplicate'].includes(photo.match_details?.method) ? photo.suggested_student_id : null;
  const options = list => list.map(s => `<option value="${s.id}" ${s.id === selected ? 'selected' : ''} data-has-pages="${s.submission_id ? '1':'0'}">${escapeHTML(s.student_code)} ${escapeHTML(s.name)}${s.submission_id ? reassign ? '（已有答卷，需核验）' : '（追加到已有答卷）' : ''}</option>`).join('');
  const recommended = candidates.map(c => available.find(s => s.id === c.id)).filter(Boolean);
  return `<a href="${escapeHTML(photo.url)}" target="_blank" rel="noreferrer"><img class="batch-preview" src="${escapeHTML(photo.url)}" alt="照片 #${photo.id} 原图，点击放大"></a><p class="batch-text">机器转录：${escapeHTML(photo.identified_text || '未识别到文字，仍可对照原图手动归属。')}</p>${identityMatchHTML(photo)}${reassign ? '<p class="notice warn">更正会将这张机器关联的照片移给所选学生，并重新批改；原学生恢复待交。已有教师修改、多页答卷或目标冲突时不能更正。</p>' : '<p class="tiny">请对照原图核对学号和姓名；候选只供参考。</p>'}${identityCandidatesHTML(candidates)}<label class="field">${reassign ? '更正到本次应交学生' : '本次应交学生'}<select name="assignment_student_id" required><option value="">请选择并核对编号、姓名</option>${recommended.length ? `<optgroup label="候选学生（请核对，不自动选择）">${options(recommended)}</optgroup><optgroup label="其余应交名单">${options(available.filter(s => !candidateIds.has(s.id)))}</optgroup>` : options(available)}</select></label>${!reassign ? '<label class="inline-check" data-append-confirm hidden><input type="checkbox" name="append_page" disabled>已核对：这不是重复照片，确为该学生的另一页；我确认追加到已有答卷</label>' : ''}${identityLearningHTML(photo)}<label class="inline-check"><input name="confirmed" type="checkbox" required>${reassign ? '我已核对原图，确认将照片移给所选学生并重新批改' : '我已对照原图核对学号和姓名，确认归属此学生'}</label>`;
}

function pendingIdentityPhotos() {
  if (classUI.photoReadError || classUI.workbenchReadError) return [];
  return (classUI.attention?.items || []).filter(item=>item.kind==='identity')
    .map(item=>classUI.batchPhotos.find(p=>p.id===item.photo_id))
    .filter(p=>p && !p.submission_id && !p.excluded && !p.processing_identity && !['confirmed','identifying'].includes(p.status)
      && !classUI.data.students.find(s=>s.id===p.suggested_student_id)?.exempt);
}
async function openBatchIdentity(id, reassign = false) {
  const version=state.routeVersion;
  let photo=classUI.batchPhotos.find(p=>p.id===id);
  if (!photo) throw new Error('照片状态已变化，请重新读取名单。');
  if (photo.evidence_loaded===false) {
    photo={...photo,...await api(`/api/batch-photos/${photo.id}`)};
    if (version!==state.routeVersion) return;
    classUI.batchPhotos=classUI.batchPhotos.map(p=>p.id===photo.id?photo:p);
  }
  if (reassign && !inferredPhoto(photo)) throw new Error('只有学号栏匹配、姓名推断或纠错记忆关联的照片可以使用此更正入口。');
  if (!reassign && (photo.submission_id || photo.excluded || photo.processing_identity || photo.status==='identifying')) throw new Error('照片已关联、已排除或正在处理，请重新读取名单。');
  const next=!reassign && pendingIdentityPhotos().some(p=>p.id!==id);
  classDialog(`照片 #${photo.id} · ${reassign ? '更正机器归属' : '核对归属'}`, `<form id="${reassign ? 'class-batch-reassign' : 'class-batch-confirm'}" data-photo-id="${photo.id}" data-continue="${next ? '1':'0'}" class="form-grid" data-class-dirty>${identityConfirmationHTML(photo,classUI.data.students,reassign)}<button class="btn primary" type="submit">${reassign ? '确认更正并重新批改' : next ? '确认并下一张' : '确认归属并加入答卷'}</button></form>`);
}

async function classAction(button, callback) {
  if (state.busy) { toast('正在处理，请稍候。'); return; }
  state.busy = true;
  const controls = [...document.querySelectorAll('[data-class-action], form[id^="class-"] button[type="submit"]')];
  const previouslyDisabled = controls.map(control => control.disabled);
  controls.forEach(control => { control.disabled = true; });
  setButtonBusy(button,true);clearActionError(button);
  try { await callback(); }
  catch (error) { toast(error.message, true);showActionError(button,error); }
  finally {
    state.busy = false;
    controls.forEach((control,index) => { if (control.isConnected) control.disabled = previouslyDisabled[index]; });
    setButtonBusy(button,false);
    if ($('#attention-results') && classUI.attention) Workspace.attention(classUI.attention);
    if ($('#batch-photo-queue')) { renderBatchPhotos(); renderGradingJob(classUI.job); }
  }
}

async function classGo(path) {
  state.busy = false;
  await navigate(path);
}

async function classRefresh(background=false) {
  if(!background)state.newTaskDirty = false;
  if(/^\/assignments\/\d+\/?$/.test(location.pathname))return Workspace.refresh(background);
  return await routeClasses(location.pathname,background);
}

function classBeforeReplace(form = null) {
  const others = $$('[data-class-dirty][data-modified]').filter(item => item !== form && item.isConnected);
  return !others.length || confirm('页面上还有其他未保存的修改，本次操作将刷新页面并丢弃这些修改。确定继续？');
}

function classCloseDialog() {
  const dialog = $('#class-dialog');
  if (!dialog) return;
  if ($('[data-modified]',dialog) && !confirm('关闭会丢失尚未保存的修改，确定关闭？')) return;
  dialog.remove();
  state.newTaskDirty = !!$('[data-class-dirty][data-modified]');
  if(classUI.dialogFocus?.isConnected)classUI.dialogFocus.focus({preventScroll:true});
}

let classJobTimer;
async function routeClasses(path,background=false) {
  clearTimeout(classJobTimer);
  const match = path.match(/^\/(classes|students|assignments)(?:\/(\d+))?\/?$/);
  if (!match || (match[1] !== 'classes' && !match[2])) return false;
  const version = state.routeVersion;
  const data = await api(`/api/${match[1]}${match[2] ? `/${match[2]}` : ''}`);
  if (version !== state.routeVersion || location.pathname !== path) return true;
  if(background && hasListInteraction())return false;
  classUI.data = data;
  classUI.importSource = null; classUI.importPreview = null;
  if (match[1] === 'classes' && !match[2]) renderClassList(data.classes);
  else if (match[1] === 'classes') renderClassDetail(data);
  else if (match[1] === 'students') renderClassStudent(data);
  else {
    await Workspace.route(data.id);
    if(version!==state.routeVersion)return true;
    const params=new URLSearchParams(location.search),photo=Number(params.get('photo'));
    if(photo)document.querySelector(`[data-class-action=batch-confirm][data-id="${photo}"]`)?.click();
    else if(params.has('q'))$('#assignment-students')?.scrollIntoView({block:'start',behavior:'instant'});
  }
  return true;
}

function renderClassList(classes) {
  classUI.classes = classes;
  $('#breadcrumb').textContent = '工作台 / 班级管理';
  $('#main').innerHTML = heading('班级管理','名单只录入一次，之后按班布置和批改；归档后仍可查阅历史。','') +
    `<section class="card"><div class="card-header"><h2>建立班级</h2></div><form id="class-create" class="card-content class-inline-form" data-class-dirty><label class="field">班级名称<input name="name" required maxlength="100" placeholder="例如：五年级 2 班"></label><label class="field">学年 / 学期<input name="term" maxlength="100" placeholder="例如：2026—2027 第一学期"></label><button class="btn primary" type="submit">建立班级</button></form></section>` +
    `<section class="card"><div class="card-header"><div><h2>我的班级</h2><p>编号在各班独立使用，同名学生请结合编号核对。</p></div><span class="badge">共 ${classes.length} 个</span></div><div class="card-content class-toolbar"><label class="field class-search">搜索班级<input id="class-list-search" type="search" placeholder="班级名称或学期"></label><label class="inline-check"><input id="class-show-archived" type="checkbox">显示已归档班级</label></div><div id="class-list-results" class="class-grid">${classes.map(c => `<article class="class-tile" data-class-list-row data-search="${escapeHTML(`${c.name} ${c.term || ''}`.toLocaleLowerCase())}" data-archived="${c.archived ? '1' : '0'}" ${c.archived ? 'hidden' : ''}><div class="button-row"><h3><a href="/classes/${c.id}" data-nav>${escapeHTML(c.name)}</a></h3>${c.archived ? '<span class="badge">已归档</span>' : '<span class="badge blue">使用中</span>'}</div><p class="muted">${escapeHTML(c.term || '未设置学期')}</p><div class="summary-strip"><span><strong>${numberText(c.student_count)}</strong>在班学生</span><span><strong>${numberText(c.pending_count)}</strong>待处理作业</span></div><div class="button-row">${classLink(`/classes/${c.id}`,'查看班级 →')}</div></article>`).join('')}</div><div id="class-list-empty" ${classes.some(c => !c.archived) ? 'hidden' : ''}>${empty(classes.length ? '没有使用中的班级' : '先建立第一个班级',classes.length ? '可以显示归档班级，或建立一个新班级。' : '填写上方班级名称，随后粘贴或导入学生名单。')}</div></section>`;
}

function showClassTab(button) {
  $$('[data-class-action="tab"]').forEach(tab=>{
    const active=tab===button;
    tab.classList.toggle('primary',active);
    tab.setAttribute('aria-selected',String(active));
    $(`#class-${tab.dataset.panel}-panel`).hidden=!active;
  });
}
function renderClassDetail(data) {
  const members = data.members || [], assignments = data.assignments || [];
  $('#breadcrumb').textContent = `班级管理 / ${data.name}`;
  $('#main').innerHTML = heading(data.name,`${data.term || '未设置学期'} · ${members.filter(classMemberActive).length} 位有效在班学生`,`<div class="button-row">${classLink('/classes','返回班级列表')}<button class="btn small" data-class-action="memory-open" data-id="${data.id}">姓名纠错记忆</button></div>`) +
    `${data.archived ? '<div class="notice warn class-spacing">班级已归档，可查看历史。恢复班级后可以继续管理名单和布置任务。</div>' : ''}` +
    `<details class="card"><summary class="class-details-summary">班级设置</summary><form id="class-settings" class="card-content class-inline-form" data-class-dirty><label class="field">班级名称<input name="name" value="${escapeHTML(data.name)}" required maxlength="100"></label><label class="field">学年 / 学期<input name="term" value="${escapeHTML(data.term || '')}" maxlength="100"></label><button class="btn" type="submit">保存设置</button><button type="button" class="btn ${data.archived ? '' : 'danger'}" data-class-action="archive">${data.archived ? '恢复班级' : '归档班级'}</button></form></details>` +
    `<div class="class-tabs" role="tablist" aria-label="班级视图"><button class="btn primary" role="tab" aria-selected="true" aria-controls="class-roster-panel" id="class-roster-tab" data-class-action="tab" data-panel="roster">学生名册（${members.filter(classMemberActive).length}）</button><button class="btn" role="tab" aria-selected="false" aria-controls="class-assignments-panel" id="class-assignments-tab" data-class-action="tab" data-panel="assignments">班级任务（${assignments.length}）</button></div>` +
    `<div id="class-roster-panel" role="tabpanel" aria-labelledby="class-roster-tab">${!data.archived ? classImportHTML() + classAddHTML() : ''}<section class="card"><div class="card-header"><h2>学生名册</h2><span class="badge">固定学生档案 · 保留历史</span></div><div class="card-content class-toolbar"><label class="field class-search">搜索学生<input id="class-member-search" type="search" placeholder="姓名或班内编号"></label><label class="inline-check"><input id="class-show-former" type="checkbox">显示已退出成员</label></div><div class="table-wrap"><table><thead><tr><th>班内编号</th><th>姓名</th><th>状态</th><th>操作</th></tr></thead><tbody>${members.map(m => `<tr data-class-member-row data-search="${escapeHTML(`${m.student_code} ${m.name}`.toLocaleLowerCase())}" data-active="${m.active ? '1' : '0'}" ${m.active ? '' : 'hidden'}><td>${escapeHTML(m.student_code)}</td><td><a href="/students/${m.student_id}" data-nav><strong>${escapeHTML(m.name)}</strong></a></td><td><span class="badge ${classMemberActive(m) ? 'green' : ''}">${!m.active ? '已退出' : classMemberActive(m) ? '在班' : '档案已停用'}</span></td><td><div class="button-row">${classLink(`/students/${m.student_id}`,'历史作业')}${classMemberActive(m) && !data.archived ? `<button class="btn small" data-class-action="member-edit" data-id="${m.id}">改编号 / 转班</button>` : ''}${m.active && !data.archived ? `<button class="btn small danger" data-class-action="leave" data-id="${m.id}">退出班级</button>` : ''}</div></td></tr>`).join('')}</tbody></table></div><div id="class-member-empty" ${members.some(m => m.active) ? 'hidden' : ''}>${empty('没有符合条件的学生','导入名单或添加已有学生；已退出学生可通过上方选项查看。')}</div></section></div>` +
    `<div id="class-assignments-panel" role="tabpanel" aria-labelledby="class-assignments-tab" hidden><section class="card"><div class="card-header"><div><h2>已布置任务</h2><p>任务名单保留布置时的身份，转班和改名不会改写旧成绩。</p></div>${classLink('/assignments','查看全部作业')}</div>${assignments.length ? `<div class="table-wrap"><table><thead><tr><th>任务</th><th>应交 / 已交</th><th>批改状态与识别情况</th><th>布置日期</th><th>操作</th></tr></thead><tbody>${assignments.map(a => { const c = a.counts || a; return `<tr><td><strong>${escapeHTML(a.task_name || a.name)}</strong></td><td>${numberText(c.expected)} / ${numberText(c.submitted)}</td><td>${classTaskStatus(c,a.id)}</td><td>${dateText(a.created_at)}</td><td>${classLink(`/assignments/${a.id}${c.attention_submissions?'?view=attention':''}`,c.attention_submissions?'查看待核对答卷':'查看名单 / 成绩','primary')}</td></tr>`; }).join('')}</tbody></table></div>` : empty('还没有班级任务','打开一个默写任务，选择本班即可布置。',classLink('/tasks','前往标准答案与布置'))}</section></div>`;
  if(new URLSearchParams(location.search).get('panel')==='assignments')showClassTab($('#class-assignments-tab'));
}

function classImportHTML() {
  return `<details class="card" open><summary class="class-details-summary">批量录入名单</summary><form id="class-import" class="card-content form-grid" data-class-dirty><p class="muted tiny">直接选择 Excel（.xlsx）名单，或粘贴“班内编号、姓名”两列；也支持 CSV。编号按文本保留，001 不会变为 1；每批最多 1000 行，文件不超过 2 MB。已有同编号同姓名跳过，同编号不同姓名须先处理。</p><label class="field">粘贴名单<textarea name="text" id="class-import-text" rows="4" placeholder="编号&#9;姓名&#10;001&#9;张同学&#10;002&#9;李同学"></textarea></label><label class="field">选择 Excel（.xlsx）或 CSV 文件<input type="file" id="class-import-file" name="file" accept=".xlsx,.csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,text/csv"></label><label class="field">Excel 工作表<select id="class-import-sheet" name="sheet_name" disabled><option value="">预览后可选择工作表（默认首个可见工作表）</option></select></label><p class="muted tiny">粘贴与文件二选一。Excel 预览后可切换工作表，再重新预览。CSV 支持 UTF-8 / GB18030；旧版 .xls 请另存为 .xlsx。含明确“编号 / 学号、姓名”表头的多列名单会自动匹配，其他列不导入；有多个候选列时再手动指定。</p><details><summary>列顺序 / 多列名单设置</summary><div class="class-inline-form class-spacing"><label class="inline-check"><input name="mapping" id="class-import-mapping" type="checkbox">手动指定列</label><label class="field">编号在第几列<input name="code_column" id="class-import-code-column" type="number" value="1" min="1" max="50" step="1"></label><label class="field">姓名在第几列<input name="name_column" id="class-import-name-column" type="number" value="2" min="1" max="50" step="1"></label><label class="field">第一行处理<select name="has_header" id="class-import-header"><option value="auto">自动识别常见表头</option><option value="true">首行是标题，跳过</option><option value="false">首行就是学生，保留</option></select></label></div></details><div class="button-row"><button class="btn" type="submit">预览名单</button><span class="muted tiny">预览不会写入名册</span></div><div id="class-import-preview" aria-live="polite"></div></form></details>`;
}

function classAddHTML() {
  return `<details class="card"><summary class="class-details-summary">单个添加 / 添加已有学生</summary><div class="card-content form-grid"><form id="class-add-new" class="class-inline-form" data-class-dirty><label class="field">班内编号<input name="student_code" required maxlength="100" placeholder="例如：003"></label><label class="field">姓名<input name="name" required maxlength="100"></label><button class="btn" type="submit">新建并加入</button></form><div class="notice">转入学生如果已有档案，请搜索后选择。相同姓名或相同编号不会自动合并身份。</div><form id="class-find-student" class="class-inline-form"><label class="field class-search">搜索已有学生<input name="q" type="search" required placeholder="姓名、学号或班内编号"></label><button class="btn" type="submit">搜索档案</button></form><div id="class-student-results" aria-live="polite"></div></div></details>`;
}

function classPreview(data) {
  classUI.importPreview = data;
  const sheet = $('#class-import-sheet');
  if (data.sheet_names) {
    sheet.innerHTML = data.sheet_names.map(name => `<option value="${escapeHTML(name)}">${escapeHTML(name)}</option>`).join('');
    sheet.value = data.sheet_name;
    sheet.disabled = false;
    classUI.importSource.sheet_name = data.sheet_name;
  } else {
    sheet.innerHTML = '<option value="">仅 Excel 文件需要选择工作表</option>';
    sheet.disabled = true;
  }
  const errors = data.errors || [], rows = data.rows || [];
  let columnNotice = '';
  if (['auto','manual'].includes(data.column_detection)) {
    const column = index => `第 ${index+1} 列${data.header_detected ? `“${data.columns[index] || ''}”` : ''}`;
    const ignored = data.ignored_columns?.length ? `；其他列本次不导入：${data.ignored_columns.join('、')}` : '';
    columnNotice = `<div class="notice">${escapeHTML(`${data.column_detection === 'auto' ? '已按表头自动识别' : '按手动设置读取'}：班内编号 ← ${column(data.code_column)}；姓名 ← ${column(data.name_column)}${ignored}。请核对下方预览。`)}</div>`;
    if (data.column_detection === 'auto') {
      $('#class-import-code-column').value = data.code_column+1;
      $('#class-import-name-column').value = data.name_column+1;
    }
  }
  $('#class-import-preview').innerHTML = `${columnNotice}<div class="notice ${errors.length || rows.some(r => r.status === 'conflict') ? 'error' : ''}">新增 ${numberText(data.new_count)} 人 · 重复跳过 ${numberText(data.duplicate_count)} 人${errors.length ? `<ul>${errors.map(e => `<li>${escapeHTML(typeof e === 'object' ? `第 ${e.line || '?'} 行：${e.message || e.error || JSON.stringify(e)}` : e)}</li>`).join('')}</ul>` : ''}</div>${rows.length ? `<div class="table-wrap class-preview-table"><table><thead><tr><th>行号</th><th>班内编号</th><th>姓名</th><th>检查结果</th></tr></thead><tbody>${rows.map(r => `<tr><td>${escapeHTML(r.line)}</td><td>${escapeHTML(r.student_code)}</td><td>${escapeHTML(r.name)}</td><td><span class="badge ${r.status === 'new' ? 'green' : r.status === 'conflict' ? 'red' : ''}">${({new:'新增',duplicate:'重复，跳过',conflict:'冲突，不能覆盖'})[r.status] || escapeHTML(r.status)}</span></td></tr>`).join('')}</tbody></table></div>` : ''}<button type="button" class="btn primary" data-class-action="import-commit" ${errors.length || rows.some(r => r.status === 'conflict') || !data.new_count ? 'disabled' : ''}>确认导入 ${numberText(data.new_count)} 位学生</button><p class="tiny muted">确认后一次性保存；发现冲突时请修改名单，再重新预览。</p>`;
}

function renderClassStudent(data) {
  $('#breadcrumb').textContent = `班级管理 / 学生档案 / ${data.name}`;
  $('#main').innerHTML = heading(data.name,'同一份学生档案关联历次作业；历史成绩保留当时的姓名和班级。',classLink('/classes','返回班级管理')) +
    `<section class="card"><div class="card-header"><h2>学生档案</h2><span class="badge ${data.active ? 'green' : ''}">${data.active ? '使用中' : '已停用'}</span></div><form id="class-student-edit" class="card-content form-grid" data-class-dirty><div class="class-inline-form"><label class="field">姓名<input name="name" required maxlength="100" value="${escapeHTML(data.name)}"></label><label class="field">校级学号（可选）<input name="school_code" maxlength="100" value="${escapeHTML(data.school_code || '')}"></label><button class="btn" type="submit">保存档案</button></div><label class="inline-check"><input name="active" type="checkbox" ${data.active ? 'checked' : ''}>学生档案使用中（停用仍保留历史）</label></form><div class="card-content class-memberships">${(data.memberships || []).map(m => `<span class="class-membership"><a href="/classes/${m.class_id}" data-nav>${escapeHTML(m.class_name || m.name)} · ${escapeHTML(m.term || '')}</a>　${escapeHTML(m.student_code)} <span class="badge ${m.active ? 'green' : ''}">${m.active ? '在班' : '已退出'}</span></span>`).join('') || '<span class="muted">暂无班级记录</span>'}</div></section>` +
    `<section class="card"><div class="card-header"><div><h2>历次作业</h2><p>暂定成绩与最终成绩分开展示；不同满分的任务不直接汇总均分。</p></div></div>${data.submissions?.length ? `<div class="table-wrap"><table><thead><tr><th>任务</th><th>布置时班级 / 身份</th><th>状态</th><th>成绩</th><th>日期</th><th>操作</th></tr></thead><tbody>${data.submissions.map(s => `<tr><td><strong>${escapeHTML(s.task_name)}</strong></td><td>${escapeHTML(s.class_name || '未关联班级')}<br><span class="muted">${escapeHTML(s.student_code || '')} ${escapeHTML(s.name || s.student_name || '')}</span></td><td>${classBadge(classStatus(s))}</td><td>${escapeHTML(classScore(s))}</td><td>${dateText(s.created_at)}</td><td>${classHistoryAction(s)}</td></tr>`).join('')}</tbody></table></div>` : empty('还没有作业记录','按班布置任务后，历次作业和未交状态会显示在这里。')}</section>`;
}

function unsubmittedStudents(data) {
  return (data.students || []).filter(s=>recognitionFor(s).state==='not_uploaded')
    .sort((a,b)=>String(a.student_code).localeCompare(String(b.student_code),'zh-CN',{numeric:true}) || a.id-b.id);
}

async function copyUnsubmittedStudents() {
  const data = classUI.data, missing = unsubmittedStudents(data), version = state.routeVersion;
  if (!missing.length) { toast('当前没有未登记提交的学生。'); return; }
  const pending = classUI.batchSummary?.pending || 0;
  const note = classUI.photoReadError || !classUI.batchSummary ? '\n照片归属状态暂未核实，以下名单仅反映当前登记。' : pending ? `\n尚有 ${pending} 张照片待确认归属，以下名单以当前登记为准。` : '';
  const text = `${data.class_name} · ${data.task_name}\n未登记提交（${missing.length} 人）${note}\n${missing.map(s=>`${s.student_code} ${s.name}`).join('\n')}`;
  try {
    await navigator.clipboard.writeText(text);
    if (version === state.routeVersion) toast(`已复制 ${missing.length} 人未交名单。`);
  } catch {
    if (version !== state.routeVersion) return;
    if ($('#class-dialog')) { toast('浏览器未允许复制，请关闭当前窗口后重试。',true); return; }
    classDialog('手动复制未交名单',`<p class="notice">浏览器未允许自动复制。文字已选中，可按 Ctrl+C 或长按复制。</p><label class="field">本班全部未登记提交学生<textarea readonly rows="10">${escapeHTML(text)}</textarea></label>`);
    const field=$('#class-dialog textarea');field.focus();field.select();
  }
}

async function loadBatchPhotos(aid) {
  const version = state.routeVersion;
  try {
    const data = await api(`/api/assignments/${aid}/photos?summary=1`);
    if (version !== state.routeVersion || location.pathname !== `/assignments/${aid}`) return;
    classUI.photoReadError = '';
    classUI.batchPhotos = data.photos || [];
    classUI.batchSummary = data.summary || null;
    if(!hasListInteraction()&&!document.activeElement?.closest('#batch-photo-queue'))renderBatchPhotos();
  } catch (error) {
    if (version !== state.routeVersion || location.pathname !== `/assignments/${aid}`) return;
    classUI.photoReadError = error.message;
  }
}
function renderBatchPhotos() {
  const target = $('#batch-photo-queue');
  if (!target) return;
  const summary = classUI.batchSummary;
  if ($('#photo-archive-count')) $('#photo-archive-count').textContent=`（${classUI.batchPhotos.length} 张）`;
  if ($('#batch-match-summary')) $('#batch-match-summary').textContent = summary ? `已匹配 ${numberText(summary.matched)} 人 · 剩余未匹配学生 ${numberText(summary.remaining_students)} 人 · 自动处理中 ${numberText(summary.processing_photos || 0)} 张 · 待确认归属照片 ${numberText(Math.max(0, summary.pending - (summary.processing_photos || 0)))} 张 · 精确 ${numberText(summary.exact)} · 学号栏 ${numberText(summary.band || 0)} · 推断 ${numberText(summary.inferred)} · 记忆 ${numberText(summary.memory)} · 疑似重复 ${numberText(summary.duplicate)} · 已排除 ${numberText(summary.excluded)}` : '';
  const labels = {uploaded:'待识别',identifying:'处理中',identified:'待确认归属',error:'识别失败',confirmed:'已确认'};
  target.innerHTML = classUI.batchPhotos.length ? `<div class="batch-grid">${classUI.batchPhotos.map(photo => {
    if (photo.excluded) return `<article class="batch-photo"><strong>照片 #${photo.id} · 已排除</strong><p class="tiny muted">不参与匹配、批改和异常统计，原图保留。</p><a href="${escapeHTML(photo.url)}" target="_blank" rel="noreferrer">查看原图</a> <button class="btn small" data-class-action="batch-restore" data-id="${photo.id}">恢复照片</button></article>`;
    if (photo.processing_identity) return `<article class="batch-photo" data-batch-photo="${photo.id}"><strong>照片 #${photo.id}</strong> <span class="badge blue">${photo.status === 'identified' ? '等待系统统一匹配' : photo.status === 'identifying' ? '正在识别' : photo.status === 'error' ? '等待本批处理结束' : '排队处理中'}</span><p class="tiny muted">系统会自动匹配学生，暂不需要老师选择。</p><a href="${escapeHTML(photo.url)}" target="_blank" rel="noreferrer">查看原图</a></article>`;
    const student = (photo.submission_id && classUI.data.students.find(s => s.submission_id === photo.submission_id)) || classUI.data.students.find(s => s.id === photo.suggested_student_id);
    const scoreHTML = photo.submission_id ? `<div class="batch-photo-score"><strong>${photo.score == null ? '尚未出成绩' : `${photo.is_final ? '最终成绩' : '暂定成绩'} ${numberText(photo.score)} / ${numberText(photo.total_points)}`}</strong>${photo.grading_source ? `<span class="muted tiny">${escapeHTML(photo.grading_source)}</span>` : ''}${recognitionHTML({...photo,id:photo.submission_id},'assignment')}</div>` : '';
    const evidence = `<a href="${escapeHTML(photo.url)}" target="_blank" rel="noreferrer"><img src="${escapeHTML(photo.url)}" alt="照片 #${photo.id} 原图" loading="lazy"></a><p class="batch-text">${escapeHTML(photo.evidence_loaded===false ? '展开后读取已保存的识别文字。' : photo.identified_text || '尚无识别文字。')}</p>`;
    return `<article class="batch-photo" data-batch-photo="${photo.id}"><div><strong>照片 #${photo.id}</strong> <span class="muted tiny">${photo.answer_layout === 'separate' ? '独立答题卡' : '原文作答'}</span> <span class="badge ${photo.submission_id ? 'green' : 'amber'}">${labels[photo.status] || '待处理'}</span></div>${photo.submission_id && student ? `<a class="batch-student" href="/students/${student.student_id}" data-nav><span>关联学生</span><strong>${escapeHTML(student.name)}</strong><span>学号 ${escapeHTML(student.student_code)}</span></a>` : ''}${scoreHTML}${identityMatchHTML(photo)}${photo.submission_id ? `<details><summary>查看照片与识别原文</summary>${evidence}</details>` : evidence}${!photo.submission_id && student ? `<p class="tiny">建议核对：${escapeHTML(student.student_code)} ${escapeHTML(student.name)}</p>` : ''}${photo.error ? `<p class="notice error">${escapeHTML(photo.error)}</p>` : ''}<div class="button-row">${!photo.submission_id ? `<button class="btn small" data-class-action="batch-identify" data-id="${photo.id}" ${state.busy ? 'disabled' : ''}>${photo.status === "identified" ? "继续处理" : "重试识别"}</button><button class="btn small primary" data-class-action="batch-confirm" data-id="${photo.id}" ${state.busy ? 'disabled' : ''}>选择学生</button><button class="btn small" data-class-action="batch-exclude" data-id="${photo.id}">排除本张</button>` : classLink(`/submissions/${photo.submission_id}`,'查看成绩 / 改判 →')}${inferredPhoto(photo) ? `<button class="btn small" data-class-action="batch-reassign" data-id="${photo.id}" ${state.busy ? 'disabled' : ''}>更正机器归属</button>` : ''}</div></article>`;
  }).join('')}</div>` : '<span class="muted">尚未上传照片。</span>';
}

function renderAttention(data) {classUI.workbenchReadError='';classUI.attention=data;if(!hasListInteraction()&&!document.activeElement?.closest('#attention-results')){Workspace.attention(data);Workspace.updateExceptionDetails(data.summary);}}

document.addEventListener('toggle',async event=>{
  const detail=event.target;
  if(!detail.matches?.('.batch-photo details') || !detail.open)return;
  const id=Number(detail.closest('[data-batch-photo]')?.dataset.batchPhoto);
  const photo=classUI.batchPhotos.find(p=>p.id===id);
  if(!photo || photo.evidence_loaded!==false || detail.dataset.loading)return;
  detail.dataset.loading='1';
  const text=detail.querySelector('.batch-text');if(text)text.textContent='正在读取已保存文字…';
  try {
    const saved=await api(`/api/batch-photos/${id}`);
    Object.assign(photo,saved);
    if(text?.isConnected)text.textContent=photo.identified_text||'尚无识别文字。';
  } catch(error) {if(text?.isConnected)text.textContent=`读取失败，收起后可重试：${error.message}`;}
  finally {delete detail.dataset.loading;}
},true);

function renderGradingJob(job) {
  const panel = $('#batch-job-status'); if (!panel) return;
  classUI.job = job;
  const active = job && ['queued','running'].includes(job.state);
  const unfinished = job && ['queued','running','interrupted','failed'].includes(job.state);
  if(unfinished && $('#assignment-upload')) $('#assignment-upload').open=true;
  const upload = $('[data-workspace-upload]') || $('[data-class-action=batch-upload]'); if (upload) upload.disabled = !!unfinished||state.busy;
  const input = $('#batch-photo-input'); if (input) input.disabled = !!unfinished||state.busy;
  $$('[data-upload-retry],[data-upload-resend]').forEach(button=>button.disabled=!!unfinished||state.busy);
  $$('[data-upload-remove]').forEach(button=>button.disabled=state.busy);
  for (const action of ['batch-identify-all','batch-match','batch-grade-all']) {
    const button = $(`[data-class-action=${action}]`); if (button) button.disabled = !!active;
  }
  const layout = $('#batch-answer-layout'); if (layout) layout.disabled = !!unfinished||state.busy;
  const start = $('[data-class-action=batch-start]');
  if (start) { start.disabled = !!active; start.textContent = active ? '后台处理中' : job && ['interrupted','failed'].includes(job.state) ? '继续后台批改' : '处理已上传照片'; }
  panel.innerHTML = !job ? '<p class="tiny muted">照片上传结束后可离开页面；本机服务需保持运行。</p>' : `<div class="notice ${job.state === 'interrupted' || job.state === 'failed' ? 'warn' : ''}"><strong>${escapeHTML(({queued:'本批等待处理',running:'本批处理中',interrupted:'本批处理已中断',failed:'本批任务失败',done:'批改完成，查看成绩'})[job.state] || job.state)}</strong>${job.total ? ` · ${escapeHTML(({identifying:'识别学生',matching:'匹配名单',grading:'自动计分',ai_review:'AI 复核'})[job.stage] || '')} ${numberText(job.completed)} / ${numberText(job.total)}` : ''}<br>${job.state==='done'&&!job.total?'本次没有新增待批改答卷。全班完成情况见上方汇总。':escapeHTML(job.message)}${active ? '<br>可以离开本页面，本机服务开启时继续处理。' : ''}${job.state === 'interrupted' || job.state === 'failed' ? '<br>请先继续本次任务，结束后再上传新照片。' : ''}${job.errors?.length ? `<br>${job.errors.length} 项处理失败，原图已保留；可在全班异常中核对。` : ''}</div>`;
  if(job?.state==='done')panel.insertAdjacentHTML('beforeend',`<a class="btn" data-nav href="/assignments/${classUI.data.id}?tab=grades">查看成绩</a>`);
  if(classUI.refreshPending)panel.insertAdjacentHTML('beforeend','<p class="tiny muted" role="status">批改进度已更新，结束当前操作后自动刷新名单。</p>');
  if(classUI.photoReadError)panel.insertAdjacentHTML('beforeend',`<p class="notice warn">照片列表暂时无法获取，正在重新读取；不必重复上传。${escapeHTML(classUI.photoReadError)}</p>`);
  if(classUI.startError)panel.insertAdjacentHTML('beforeend',`<p class="notice warn">后台提交未确认，照片已保存；请先查看进度，或点击“处理已上传照片”继续，无需重新上传。${escapeHTML(classUI.startError)}</p>`);
  if(!active&&(classUI.startError||['interrupted','failed'].includes(job?.state)))panel.insertAdjacentHTML('beforeend',`<button class="btn primary" data-class-action="batch-start">${classUI.startError?'处理已上传照片':'继续后台批改'}</button>`);
}
async function loadAssignmentWorkbench(aid) {
  const version = state.routeVersion;
  try {
    const [attention, data] = await Promise.all([api(`/api/assignments/${aid}/attention`),api(`/api/assignments/${aid}/grading-jobs`)]);
    if (version !== state.routeVersion || location.pathname !== `/assignments/${aid}`) return;
    const wasActive = classUI.job && ['queued','running'].includes(classUI.job.state);
    if (data.job && (['queued','running'].includes(data.job.state) || data.job.id !== classUI.job?.id)) classUI.startError = '';
    if (classUI.photoReadError || !classUI.job || classUI.job.stage !== data.job?.stage || classUI.job.state !== data.job?.state) {
      await loadBatchPhotos(aid);
      if (version !== state.routeVersion) return;
    }
    if (wasActive && data.job && !['queued','running'].includes(data.job.state))classUI.refreshPending=true;
    if(!classUI.refreshPending && classUI.data?.students?.some(s=>recognitionFor(s).state==='processing') && !hasListInteraction()) {
      const latest=await api(`/api/assignments/${aid}`);
      if(version!==state.routeVersion)return;
      if(latest.students.some((s,i)=>JSON.stringify(s.recognition)!==JSON.stringify(classUI.data.students[i]?.recognition)))classUI.refreshPending=true;
    }
    if(classUI.refreshPending && !classUI.photoReadError && !hasListInteraction()) {
      const latest=await api(`/api/assignments/${aid}`);if(version!==state.routeVersion)return;classUI.data=latest;classUI.refreshPending=false;
    }
    classUI.job=data.job;renderAttention(attention);
    if(!document.activeElement?.closest('#batch-job-status')&&!$('#class-dialog'))renderGradingJob(data.job);
    clearTimeout(classJobTimer);
    if (classUI.photoReadError || classUI.refreshPending || data.job && ['queued','running'].includes(data.job.state) || classUI.data?.students?.some(s=>recognitionFor(s).state==='processing') || hasListInteraction() || document.activeElement?.closest('#attention-results,#batch-job-status')) classJobTimer = setTimeout(() => loadAssignmentWorkbench(aid),1500);
  } catch (error) {
    if (version !== state.routeVersion) return;
    classUI.workbenchReadError=error.message;
    Workspace.statusUnavailable();
    const panel = $('#attention-results');
    if (panel&&!document.activeElement?.closest('#attention-results')) panel.innerHTML = `<div class="notice warn" role="alert">状态暂时无法获取：${escapeHTML(error.message)} <button class="text-button" data-class-action="batch-refresh-workbench">重新读取</button></div>`;
    const message=$('#attention-count');if(message)message.textContent='状态暂时无法获取，保留上次列表，正在重新读取。';
    const count=$('[data-workspace-count]');if(count)count.textContent='—';
    const progress=$('#batch-job-status');if(progress&&!document.activeElement?.closest('#batch-job-status'))progress.textContent=`状态暂时无法获取：${error.message}，照片无需重复上传，正在重新读取。`;
    clearTimeout(classJobTimer);
    classJobTimer = setTimeout(() => loadAssignmentWorkbench(aid),1500);
  }
}
async function beginBackgroundGrading() {
  const job = classUI.job, aid = classUI.data.id, version = state.routeVersion;
  const path = job && ['interrupted','failed'].includes(job.state) ? `/api/grading-jobs/${job.id}/resume` : `/api/assignments/${aid}/grading-jobs`;
  try {
    const data = await api(path,{method:'POST',body:{}});
    if (version !== state.routeVersion) return;
    classUI.startError = '';
    renderGradingJob(data.job);
    toast(data.job.state === 'interrupted' ? '上次处理已中断，请点击继续后台批改。' : '已交给本机后台处理，可以离开此页面。');
  } catch (error) {
    if (version !== state.routeVersion) return;
    classUI.startError = error.message;
  }
  await loadBatchPhotos(aid);
  if (version !== state.routeVersion) return;
  renderGradingJob(classUI.job);
  clearTimeout(classJobTimer);
  classJobTimer = setTimeout(() => loadAssignmentWorkbench(aid),300);
}

function batchProgress(message) {
  const el = $('#batch-progress');
  if (el) el.textContent = message;
}
async function batchMatchAndGrade() {
  const result = await api(`/api/assignments/${classUI.data.id}/photos/match`,{method:'POST',body:{auto_grade:true}});
  classUI.batchPhotos = result.photos || [];classUI.batchSummary = result.summary || null;
  renderBatchPhotos();
  batchProgress('已按已有文字匹配，后续批改已交给后台，可以离开页面。');
}

async function mountTaskClasses(task) {
  const panel = $('#task-class-panel');
  if (!panel) return;
  panel.innerHTML = '<section class="card"><div class="card-content muted">正在加载班级布置…</div></section>';
  try {
    const [list, latest] = await Promise.all([api('/api/classes'),api(`/api/tasks/${task.id}`)]);
    if (!panel.isConnected) return;
    const assignments = latest.assignments || [], assigned = new Set(assignments.map(a => a.class_id));
    const available = list.classes.filter(c => !c.archived && !assigned.has(c.id));
    classUI.taskLink = {task_id:task.id,assignments,submissions:latest.submissions.filter(s => !s.identity)};
    panel.innerHTML = `<section class="card"><div class="card-header"><div><h2>按班级收作业</h2><p>选择班级后保存本次应交名单，再从名单选择学生上传。</p></div>${classLink('/classes','管理班级')}</div><div class="card-content form-grid">${assignments.length ? `<div class="class-assignment-links">${assignments.map(a => `<a class="class-assignment-link" href="/assignments/${a.id}" data-nav><strong>${escapeHTML(a.class_name || a.name)}</strong><span>${escapeHTML(a.term || '')}</span><span>查看名单 / 继续批改 →</span></a>`).join('')}</div>` : '<p class="muted">尚未布置给班级。已有未关联答卷继续保留，可手动关联。</p>'}${available.length ? `<form id="class-task-assign" data-task-id="${task.id}" data-class-dirty><fieldset class="class-choice-list"><legend>布置给以下班级（可多选）</legend>${available.map(c => `<label class="inline-check"><input type="checkbox" name="class_ids" value="${c.id}">${escapeHTML(c.name)} <span class="muted">${escapeHTML(c.term || '')} · ${numberText(c.student_count)} 人</span></label>`).join('')}</fieldset><button class="btn primary" type="submit">布置任务并固定应交名单</button></form>` : `<p class="muted tiny">${list.classes.length ? '可用班级均已布置；新班级或归档班级可前往班级管理处理。' : '请先建立班级并录入名单。'}</p>`}</div></section>`;
    if (assignments.length && classUI.taskLink.submissions.length) {
      panel.insertAdjacentHTML('beforeend',`<details class="card"><summary class="class-details-summary">关联已有未归属答卷（${classUI.taskLink.submissions.length} 份）</summary><form id="class-legacy-link" class="card-content form-grid" data-class-dirty><div class="notice warn">只关联同一默写任务。请核对原答卷与目标学生，编号相同不会自动关联；目标已有答卷时禁止覆盖。</div><label class="field">原答卷<select name="submission_id" id="class-link-submission" required><option value="">请选择待关联答卷</option>${classUI.taskLink.submissions.map(s => `<option value="${s.id}">答卷 #${s.id} · 原编号 ${escapeHTML(s.student_code)} · ${dateText(s.created_at)}</option>`).join('')}</select></label><label class="field">目标班级任务<select name="assignment_id" id="class-link-assignment" required><option value="">请选择班级</option>${assignments.map(a => `<option value="${a.id}">${escapeHTML(a.class_name)} · ${escapeHTML(a.term || '')}</option>`).join('')}</select></label><label class="field">目标学生<select name="assignment_student_id" id="class-link-student" required disabled><option value="">先选择目标班级</option></select></label><div id="class-link-preview" aria-live="polite"></div><button class="btn primary" type="submit">预览并确认关联</button></form></details>`);
    }
  } catch (error) {
    if (panel.isConnected) panel.innerHTML = `<div class="notice error">${escapeHTML(error.message)} <button class="text-button" data-class-action="task-retry" data-id="${task.id}">重新加载班级</button></div>`;
  }
}

function classDialog(title, body) {
  if(!$('#class-dialog'))classUI.dialogFocus=document.activeElement;
  $('#class-dialog')?.remove();
  $('#main').insertAdjacentHTML('beforeend',`<dialog id="class-dialog" aria-labelledby="class-dialog-title"><div class="dialog-header"><h2 id="class-dialog-title">${escapeHTML(title)}</h2><button class="icon-button" data-class-action="dialog-close" aria-label="关闭">×</button></div>${body}</dialog>`);
  const dialog = $('#class-dialog');
  dialog.addEventListener('cancel', event => {
    event.preventDefault();
    if (!state.busy) classCloseDialog();
  });
  dialog.showModal();
  updateIdentityLearning($('form',dialog));
}

function classFilter() {
  const q = ($('#class-list-search')?.value || '').trim().toLocaleLowerCase(), archived = $('#class-show-archived')?.checked;
  const membersQ = ($('#class-member-search')?.value || '').trim().toLocaleLowerCase(), former = $('#class-show-former')?.checked;
  $$('[data-class-list-row]').forEach(row => { row.hidden = !row.dataset.search.includes(q) || (!archived && row.dataset.archived === '1'); });
  $$('[data-class-member-row]').forEach(row => { row.hidden = !row.dataset.search.includes(membersQ) || (!former && row.dataset.active !== '1'); });
  for (const [emptyId,selector] of [['class-list-empty','[data-class-list-row]'],['class-member-empty','[data-class-member-row]']]) {
    const el = $(`#${emptyId}`); if (el) el.hidden = $$(selector).some(row => !row.hidden);
  }
}

async function classImportRequest(commit) {
  const source = classUI.importSource;
  if (!source) throw new Error('名单已更改，请先重新预览。');
  let body;
  if (source.file) {
    body = new FormData(); body.append('file',source.file); body.append('commit',String(commit));
    if (source.sheet_name) body.append('sheet_name',source.sheet_name);
    if (source.mapping) Object.entries(source.mapping).forEach(([key,value]) => body.append(key,String(value)));
    if (source.has_header !== undefined) body.append('has_header',String(source.has_header));
  } else body = {text:source.text,commit,...source.mapping,...(source.has_header === undefined ? {} : {has_header:source.has_header})};
  return api(`/api/classes/${classUI.data.id}/import`,{method:'POST',body});
}

function classStudentResults(data, forAssignment = false) {
  const target = $(forAssignment ? '#class-assignment-student-results' : '#class-student-results');
  if (!target) return;
  target.innerHTML = data.students?.length ? `<div class="class-student-results">${data.students.map(s => `<div class="class-student-result"><div><strong>${escapeHTML(s.name)}</strong><span class="muted tiny">　档案 #${s.id}${s.school_code ? ` · 学号 ${escapeHTML(s.school_code)}` : ''}</span><p class="muted tiny">${escapeHTML((s.memberships || []).filter(m => m.active).map(m => `${m.class_name || m.name} ${m.student_code}`).join('；') || '请打开档案核对身份')}</p></div><div class="button-row">${classLink(`/students/${s.id}`,'核对档案')}<button class="btn small" data-class-action="${forAssignment ? 'assignment-add-student' : 'add-existing'}" data-id="${s.id}">选择此学生</button></div></div>`).join('')}</div>` : '<div class="notice">没有找到档案。请检查姓名或编号；首次入班学生可新建档案。</div>';
}

document.addEventListener('click', event => {
  const button = event.target.closest('[data-identity-select]');
  if (!button || state.busy) return;
  const select = button.closest('form')?.elements.assignment_student_id;
  if (!select || ![...select.options].some(o => o.value === button.dataset.identitySelect)) return;
  select.value = button.dataset.identitySelect;
  select.dispatchEvent(new Event('change', {bubbles:true}));
  select.focus();
});

document.addEventListener('input', event => {
  updateIdentityLearning(event.target.closest('form'));
  const form = event.target.closest('[data-class-dirty]');
  if (form) { state.newTaskDirty = true; form.dataset.modified = '1'; }
  if (event.target.id.startsWith('class-import-') && event.target.id !== 'class-import-preview') {
    classUI.importSource = null; classUI.importPreview = null;
    if ($('#class-import-preview')) $('#class-import-preview').innerHTML = '';
  }
  if (['class-list-search','class-member-search'].includes(event.target.id)) classFilter();
});
document.addEventListener('change', async event => {
  updateIdentityLearning(event.target.closest('form'));
  const form = event.target.closest('[data-class-dirty]');
  if (form) { state.newTaskDirty = true; form.dataset.modified = '1'; }
  if (event.target.id === 'class-import-file') {
    const sheet = $('#class-import-sheet');
    sheet.innerHTML = '<option value="">预览后可选择工作表（默认首个可见工作表）</option>';
    sheet.disabled = true;
  }
  if (event.target.id.startsWith('class-import-') && event.target.id !== 'class-import-preview') {
    classUI.importSource = null; classUI.importPreview = null;
    if ($('#class-import-preview')) $('#class-import-preview').innerHTML = '';
  }
  if (['class-show-archived','class-show-former'].includes(event.target.id)) classFilter();
  if (event.target.id === 'class-link-assignment') {
    const select = $('#class-link-student'), preview = $('#class-link-preview');
    select.disabled = true; select.innerHTML = '<option value="">正在读取名单…</option>'; preview.innerHTML = '';
    const assignment_id = Number(event.target.value);
    if (!assignment_id) { select.innerHTML = '<option value="">先选择目标班级</option>'; return; }
    try {
      const assignment = await api(`/api/assignments/${assignment_id}`);
      if (!select.isConnected || Number($('#class-link-assignment')?.value) !== assignment_id) return;
      classUI.linkAssignment = assignment;
      select.innerHTML = '<option value="">请选择并核对学生</option>' + assignment.students.map(s => `<option value="${s.id}" ${s.submission_id ? 'disabled' : ''}>${escapeHTML(s.student_code)} ${escapeHTML(s.name)}${s.submission_id ? '（已有答卷，不能覆盖）' : s.exempt ? '（免交）' : ''}</option>`).join('');
      select.disabled = false;
    } catch (error) { if (select.isConnected) { select.innerHTML = '<option value="">读取失败，请重新选择班级</option>'; toast(error.message,true); } }
  }
  if (['class-link-student','class-link-submission'].includes(event.target.id)) {
    const source = classUI.taskLink?.submissions.find(s => s.id === Number($('#class-link-submission')?.value));
    const target = classUI.linkAssignment?.students.find(s => s.id === Number($('#class-link-student')?.value));
    if ($('#class-link-preview')) $('#class-link-preview').innerHTML = source && target ? `<div class="notice">原答卷 #${source.id}（原编号 ${escapeHTML(source.student_code)}） → ${escapeHTML(classUI.linkAssignment.class_name)} · ${escapeHTML(target.student_code)} ${escapeHTML(target.name)}。${classLink(`/submissions/${source.id}`,'打开原答卷核对')}</div>` : '';
  }
});

document.addEventListener('submit', async event => {
  const form = event.target;
  if (!form.id.startsWith('class-')) return;
  event.preventDefault();
  if (!['class-import','class-find-student','class-assignment-find'].includes(form.id) && !classBeforeReplace(form)) return;
  await classAction($('button[type="submit"]',form),async () => {
    const value = name => form.elements[name]?.value.trim();
    if (form.id === 'class-batch-confirm' || form.id === 'class-batch-reassign') {
      const reassign = form.id === 'class-batch-reassign', version=state.routeVersion;
      const linked = await api(`/api/batch-photos/${form.dataset.photoId}/${reassign ? 'reassign' : 'confirm'}`,{method:'POST',body:identityPayload(form)});
      if (version!==state.routeVersion) return;
      const saved=classUI.batchPhotos.find(p=>p.id===Number(form.dataset.photoId));
      if (saved) Object.assign(saved,linked.photo || {},{submission_id:linked.submission_id});
      $('#class-dialog')?.remove();state.newTaskDirty=!!$('[data-class-dirty][data-modified]');
      toast(reassign ? '已更正归属并交给后台批改，原学生恢复待交。' : '归属已保存并交给后台批改。');
      try {
        await classRefresh();
        if (version!==state.routeVersion) return;
        if (classUI.photoReadError || classUI.workbenchReadError) throw new Error(classUI.photoReadError || classUI.workbenchReadError);
        if (!reassign && form.dataset.continue==='1') {
          const next=pendingIdentityPhotos()[0];
          if (next) await openBatchIdentity(next.id);
        }
      } catch (error) {
        if (version!==state.routeVersion) return;
        const panel=$('#assignment-attention');
        if (panel) panel.innerHTML=`<div id="identity-save-status" class="card-content notice warn" role="status">归属已保存并交给后台批改；名单或下一张照片暂时无法读取：${escapeHTML(error.message)}。无需重复确认。<a class="btn small" data-nav href="${escapeHTML(location.pathname+location.search)}">重新读取名单</a></div>`;
        toast('归属已保存，后续读取暂时失败；请重新读取名单，无需重复确认。',true);
      }
    } else if (form.id === 'class-memory-import') {
      const chosen = [...form.querySelectorAll('[name=memory_photo]:checked')];
      if (!chosen.length) throw new Error('请先勾选并核对历史纠错。');
      for (const item of chosen) {
        await api(`/api/classes/${classUI.memoryClass}/identity-memories`,{method:'POST',body:{photo_id:Number(item.value)}});
        item.checked = false;
      }
      delete form.dataset.modified; state.newTaskDirty = false;
      await openIdentityMemories(classUI.memoryClass); toast('已启用选择的历史纠错。');
    } else if (form.id === 'class-create') {
      const data = await api('/api/classes',{method:'POST',body:{name:value('name'),term:value('term')}});
      state.newTaskDirty = false; await classGo(`/classes/${data.id}`);
    } else if (form.id === 'class-settings') {
      await api(`/api/classes/${classUI.data.id}`,{method:'PATCH',body:{name:value('name'),term:value('term')}});
      await classRefresh(); toast('班级设置已保存。');
    } else if (form.id === 'class-add-new' || form.id === 'class-add-existing') {
      const body = {name:value('name'),student_code:value('student_code')};
      if (form.dataset.studentId) { body.student_id = Number(form.dataset.studentId); delete body.name; }
      await api(`/api/classes/${classUI.data.id}/members`,{method:'POST',body});
      await classRefresh(); toast('学生已加入班级。');
    } else if (form.id === 'class-import') {
      const file = form.elements.file.files[0], text = value('text');
      if (!!file === !!text) throw new Error('请粘贴名单或选择一个 Excel / CSV 文件，二选一。');
      if (file && /\.xls$/i.test(file.name)) throw new Error('旧版 .xls 请在 Excel 中另存为 .xlsx 后上传。');
      if (file && (!/\.(xlsx|csv)$/i.test(file.name) || file.size > 2*1024*1024)) throw new Error('请选择不超过 2 MB 的 Excel（.xlsx）或 CSV 文件。');
      classUI.importSource = file ? {file,sheet_name:value('sheet_name')} : {text};
      if (form.elements.mapping.checked) classUI.importSource.mapping = {code_column:Number(value('code_column'))-1,name_column:Number(value('name_column'))-1};
      if (value('has_header') !== 'auto') classUI.importSource.has_header = value('has_header') === 'true';
      classPreview(await classImportRequest(false));
    } else if (form.id === 'class-find-student' || form.id === 'class-assignment-find') {
      const path = location.pathname, results = await api(`/api/students?q=${encodeURIComponent(value('q'))}`);
      if (path === location.pathname) classStudentResults(results,form.id === 'class-assignment-find');
    } else if (form.id === 'class-student-edit') {
      await api(`/api/students/${classUI.data.id}`,{method:'PATCH',body:{name:value('name'),school_code:value('school_code'),active:form.elements.active.checked}});
      await classRefresh(); toast('档案已保存，旧任务中的姓名和编号保持布置时记录。');
    } else if (form.id === 'class-task-assign') {
      const class_ids = $$('input[name="class_ids"]:checked',form).map(el => Number(el.value));
      if (!class_ids.length) throw new Error('请至少选择一个班级。');
      const data = await api(`/api/tasks/${form.dataset.taskId}/assignments`,{method:'POST',body:{class_ids}});
      state.newTaskDirty = false;
      if (data.assignments.length === 1) await classGo(`/assignments/${data.assignments[0].id}?tab=upload`);
      else { await mountTaskClasses({id:Number(form.dataset.taskId)}); toast('已分别布置给所选班级。'); }
    } else if (form.id === 'class-legacy-link') {
      const source = classUI.taskLink.submissions.find(s => s.id === Number(value('submission_id')));
      const target = classUI.linkAssignment?.students.find(s => s.id === Number(value('assignment_student_id')));
      if (!source || !target || Number(value('assignment_id')) !== classUI.linkAssignment.id) throw new Error('请重新选择并核对原答卷和目标学生。');
      if (!confirm(`确认将原答卷 #${source.id}（原编号 ${source.student_code}）关联到 ${classUI.linkAssignment.class_name} 的 ${target.student_code} ${target.name}？不会覆盖其他答卷。`)) return;
      await api(`/api/submissions/${source.id}/link`,{method:'POST',body:{assignment_student_id:target.id}});
      state.newTaskDirty = false; await classGo(`/submissions/${source.id}`); toast('答卷已关联，原图和复核记录保留。');
    } else if (form.id === 'class-member-change') {
      const destination = Number(value('class_id'));
      if (destination === classUI.data.id) await api(`/api/members/${form.dataset.memberId}`,{method:'PATCH',body:{student_code:value('student_code')}});
      else await api(`/api/members/${form.dataset.memberId}/transfer`,{method:'POST',body:{class_id:destination,student_code:value('student_code')}});
      await classRefresh(); toast(destination === classUI.data.id ? '班内编号已更新，旧任务记录未改动。' : '转班已完成，同一学生档案和历史作业保留。');
    } else if (form.id === 'class-exempt-form') {
      await api(`/api/assignment-students/${form.dataset.id}`,{method:'PATCH',body:{exempt:true,reason:value('reason')}});
      await classRefresh(); toast('已标记免交，已有答卷仍保留。');
    }
  });
});

document.addEventListener('click', async event => {
  const button = event.target.closest('[data-class-action]');
  if (!button) return;
  const action = button.dataset.classAction, id = Number(button.dataset.id);
  if (action.startsWith('batch-')) return;
  if (state.busy) { toast('正在处理，请稍候。'); return; }
  if (action === 'dialog-close') {
    classCloseDialog();
    return;
  }
  if (action === 'tab') {
    showClassTab(button);
    const params=new URLSearchParams(location.search);
    if(button.dataset.panel==='assignments')params.set('panel','assignments');else params.delete('panel');
    Nav.patchURL(params);
    return;
  }
  if (['archive','leave','import-commit','exempt','assignment-add-student'].includes(action) && !classBeforeReplace(action === 'import-commit' ? $('#class-import') : null)) return;
  await classAction(button,async () => {
    if (action === 'copy-unsubmitted') {
      await copyUnsubmittedStudents();
    } else if (action === 'memory-open') {
      if (!classBeforeReplace()) return;
      await openIdentityMemories(id);
    } else if (action === 'memory-toggle' || action === 'memory-resolve') {
      if (!classBeforeReplace()) return;
      const memory = classUI.memories.find(m => m.id === id);
      if (action === 'memory-resolve' && !confirm(`确认只保留“${memory.observed_name} → ${memory.name}”，停用该错名的其他对应？`)) return;
      await api(`/api/classes/${classUI.memoryClass}/identity-memories/${id}`,{method:'PATCH',body:{enabled:action === 'memory-resolve' || !memory.enabled,resolve_conflict:action === 'memory-resolve'}});
      await openIdentityMemories(classUI.memoryClass);
    } else if (action === 'archive') {
      if (!classUI.data.archived && !confirm('归档后从日常班级列表收起，学生档案和历史作业保留。确定归档？')) return;
      await api(`/api/classes/${classUI.data.id}`,{method:'PATCH',body:{archived:!classUI.data.archived}}); await classRefresh();
    } else if (action === 'leave') {
      const member = classUI.data.members.find(m => m.id === id);
      if (!confirm(`将 ${member.student_code} ${member.name} 移出本班？历史任务和成绩保留。`)) return;
      await api(`/api/members/${id}`,{method:'PATCH',body:{active:false}}); await classRefresh();
    } else if (action === 'import-commit') {
      if (!classUI.importPreview || classUI.importPreview.errors?.length || classUI.importPreview.rows.some(r => r.status === 'conflict')) throw new Error('请先修正名单并重新预览。');
      await classImportRequest(true); await classRefresh(); toast('名单已导入，重复记录已跳过。');
    } else if (action === 'member-edit') {
      const member = classUI.data.members.find(m => m.id === id), list = await api('/api/classes');
      classDialog(`调整 ${member.student_code} ${member.name}`,`<form id="class-member-change" data-member-id="${id}" class="form-grid" data-class-dirty><div class="notice">选择本班只修改编号；选择其他班级将结束原成员关系并转入新班。旧任务名单与历史成绩不改写。</div><label class="field">目标班级<select name="class_id">${list.classes.filter(c => !c.archived).map(c => `<option value="${c.id}" ${c.id === classUI.data.id ? 'selected' : ''}>${escapeHTML(c.name)} · ${escapeHTML(c.term || '')}</option>`).join('')}</select></label><label class="field">目标班内编号<input name="student_code" value="${escapeHTML(member.student_code)}" maxlength="100" required></label><button class="btn primary" type="submit">保存编号 / 确认转班</button></form>`);
    } else if (action === 'add-existing') {
      classDialog('将已有学生加入本班',`<form id="class-add-existing" data-student-id="${id}" class="form-grid" data-class-dirty><div class="notice">选择档案 #${id}。本操作新增班级成员关系，其他班级的关系和历史作业保留。</div><label class="field">本班编号<input name="student_code" required maxlength="100"></label><button class="btn primary" type="submit">确认加入本班</button></form>`);
    } else if (action === 'exempt') {
      const student = classUI.data.students.find(s => s.id === id);
      if (student.exempt) { await api(`/api/assignment-students/${id}`,{method:'PATCH',body:{exempt:false,reason:''}}); await classRefresh(); toast('已恢复为应交学生。'); }
      else classDialog(`${student.student_code} ${student.name} · 标记免交`,`<form id="class-exempt-form" data-id="${id}" class="form-grid" data-class-dirty><div class="notice">免交不计入本次应交人数；已经提交的答卷仍保留。</div><label class="field">免交原因<textarea name="reason" rows="3" required maxlength="500" placeholder="例如：当天请假"></textarea></label><button class="btn primary" type="submit">确认免交</button></form>`);
    } else if (action === 'assignment-add') {
      classDialog('补充本次应交学生',`<div class="notice class-spacing">转入的新生不会自动加入旧任务。请搜索并核对档案后，显式加入本次名单。</div><form id="class-assignment-find" class="class-inline-form"><label class="field">学生姓名 / 学号<input name="q" type="search" required></label><button class="btn" type="submit">搜索</button></form><div id="class-assignment-student-results" aria-live="polite"></div>`);
    } else if (action === 'assignment-add-student') {
      await api(`/api/assignments/${classUI.data.id}/students`,{method:'POST',body:{student_id:id}}); await classRefresh(); toast('已补充本次应交学生。');
    } else if (action === 'task-retry') await mountTaskClasses({id});
  });
});

document.addEventListener('click', async event => {
  const button = event.target.closest('[data-class-action^="batch-"]');
  if (!button || state.busy) return;
  const action = button.dataset.classAction;
  if (!classBeforeReplace()) return;
  await classAction(button, async () => {
    if (action === 'batch-ai-review') {
      const data = await api(`/api/assignments/${classUI.data.id}/ai-review-jobs`, {method:'POST',body:{}});
      renderGradingJob(data.job);
      await classRefresh();
    } else if (action === 'batch-start' || action === 'batch-identify-all' || action === 'batch-grade-all') {
      await beginBackgroundGrading();
    } else if (action === 'batch-refresh-workbench') {
      await loadAssignmentWorkbench(classUI.data.id);
    } else if (action === 'batch-identify') {
      const data = await api(`/api/batch-photos/${Number(button.dataset.id)}/process`,{method:'POST',body:{}});
      renderGradingJob(data.job);
      await classRefresh();
    } else if (action === 'batch-exclude' || action === 'batch-restore') {
      const excluded=action==='batch-exclude';
      if (excluded && !window.confirm('排除后本张照片不参与匹配和批改；原图保留，可以恢复。确定排除？')) return;
      await api(`/api/batch-photos/${Number(button.dataset.id)}/exclude`,{method:'POST',body:{excluded}});
      await classRefresh();toast(excluded ? '照片已排除，可随时恢复。':'照片已恢复，可继续匹配。');
    } else if (action === 'batch-match') {
      await batchMatchAndGrade();
      const message = $('#batch-progress').textContent;
      await classRefresh(); batchProgress(message);
    } else if (action === 'batch-confirm' || action === 'batch-reassign') {
      await openBatchIdentity(Number(button.dataset.id),action==='batch-reassign');
    }
  });
  if ($('#batch-photo-queue')) renderBatchPhotos();
});

