'use strict';

// Assignment pages share the same context; changing a tab never creates a submission.
const Workspace=(()=>{
  let upload={entries:[],aid:null},poll;
  const tab=()=>new URLSearchParams(location.search).get('tab')||'grades';
  function href(data,name){const p=new URLSearchParams(location.search);for(const k of ['photo','student','view'])p.delete(k);p.set('tab',name);return `/assignments/${data.id}?${p}`;}
  function analysisHref(id,view){const p=new URLSearchParams(location.search);for(const k of ['photo','student','tab','view'])p.delete(k);if(view==='students')p.set('view',view);return `/assignments/${id}/analysis${p.size?'?'+p:''}`;}
  function header(data,active=tab()){
    const archived=!!data.class_archived;
    $('#breadcrumb').textContent=`班级作业 / ${data.class_name} / ${data.task_name}`;
    const source=Nav.assignmentReturn().path.split('?')[0];
    const label=source==='/'?'返回工作台':source.startsWith('/classes/')?'返回班级':'返回作业管理';
    return heading(data.task_name,`${data.class_name} · ${data.term||'本次作业'}${archived?' · 已归档，仅可查看':''}`,`<button class="btn" type="button" data-assignment-return>${label}</button>`)+
      `<nav class="workspace-nav" data-workspace-nav aria-label="本次班级作业">${[['upload','上传照片'],['grades',archived?'历史成绩':'成绩'],['attention','待核对'],['analysis','讲评']].filter(([name])=>!archived||['grades','analysis'].includes(name)).map(([name,label])=>`<a data-nav href="${escapeHTML(name==='analysis'?analysisHref(data.id):href(data,name))}" ${name===active?'aria-current="page"':''}>${label}${name==='attention'?` <span data-workspace-count class="${data.counts?.attention_items>0?'has-items':''}">${data.counts?.attention_items??'—'}</span>`:''}</a>`).join('')}</nav>`;
  }
  function normalize(){
    const p=new URLSearchParams(location.search);let changed=false;
    for(const key of ['q','order','view'])if(p.has(key)){p.set('g.'+key,p.get(key));p.delete(key);changed=true;}
    if(p.get('g.view')==='uncertain'){p.set('tab','attention');p.set('a.kind','question');p.delete('g.view');changed=true;}
    else if(p.get('g.view')==='attention'){p.set('tab','attention');p.delete('g.view');changed=true;}
    if(!p.has('tab')){p.set('tab',p.has('photo')?'attention':'grades');changed=true;}
    if(!['upload','grades','attention'].includes(p.get('tab'))){p.set('tab','grades');changed=true;}
    if(changed)Nav.patchURL(p);
  }
  function scoreHTML(s,total){
    const points=s.total_points??total,valid=s.is_final&&Number.isFinite(s.score)&&Number.isFinite(points);
    const ratio=valid&&points>0?Math.min(100,Math.max(0,s.score/points*100)):null;
    return `<span class="grade-score" data-visual-number="score-${s.id}">${valid?`${numberText(s.score)} / ${numberText(points)}`:'—'}</span>${ratio==null?'':`<span class="grade-progress ${ratio<60?'low':''}" aria-hidden="true"><span style="transform:scaleX(${ratio/100})"></span></span>`}${!s.is_final&&s.submission_id?'<span class="tiny muted">暂无最终成绩</span>':''}<div class="tiny muted grade-source">${escapeHTML(s.grading_source||'')}</div>`;
  }
  function exceptionsHTML(data){
    const c=data.counts||{},n=key=>Number.isFinite(c[key])&&c[key]>=0?c[key]:null;
    const count=n('attention_items'),unknown=count===null,processing=n('processing_submissions');
    const parts=[];
    if(n('attention_submissions')>0)parts.push(`涉及 ${n('attention_submissions')} 份答卷`);
    if(n('failed_submissions')>0)parts.push(`批改失败 ${n('failed_submissions')} 份答卷`);
    if(n('uncertain_questions'))parts.push(`识别不确定 ${n('uncertain_questions')} 题`);
    if(n('identity_photos')>0)parts.push(`归属待确认 ${n('identity_photos')} 张照片`);
    if(n('ungraded_submissions')>0)parts.push(`待计分 ${n('ungraded_submissions')} 份答卷`);
    const empty=n('submitted')===0&&count===0;
    const title=unknown?'状态暂不可用':count>0?`待核对 ${count} 项`:processing>0?'后台正在处理':empty?'还没有照片':'未发现识别异常，可按需抽查';
    const detail=unknown?'待核对数量尚未取得，请重新读取状态。':count>0?parts.join(' · ')||'照片、答卷与题目的具体项目见待核对列表。':processing>0?'处理完成后自动更新成绩，无需重复上传。':empty?'上传本次照片后会自动批改。':'点击名单中的学生查看原图与本次作答。';
    return `<section class="exception-band ${unknown?'is-unknown':count>0?'':processing>0?'is-processing':'is-clear'}" id="assignment-exceptions" aria-label="本次需要处理的项目" role="status" aria-live="polite" aria-atomic="true"><div class="exception-main"><span class="exception-dot" aria-hidden="true"></span><strong>${title}</strong><span class="exception-detail">${escapeHTML(detail)}</span>${processing>0?`<span class="badge blue" data-processing>后台处理中 ${processing} 份答卷</span>`:''}</div>${count>0?`<a class="btn small" href="${escapeHTML(href(data,'attention'))}" data-nav>核对异常</a>`:''}</section>`;
  }
  function statusUnavailable(){
    const band=$('#assignment-exceptions');if(!band)return;
    band.className='exception-band is-unknown';band.innerHTML='<div class="exception-main"><strong>状态暂不可用</strong><span class="exception-detail">保留上次成绩，待核对数量暂时无法获取。</span></div><button class="btn small" data-action="reload">重新读取状态</button>';
    updateVisualNumber($('[data-workspace-count]'),'—');
  }
  function updateExceptionDetails(summary){
    const band=$('#assignment-exceptions');
    if(!band||!summary||band.classList.contains('is-unknown')||document.activeElement?.closest('#assignment-exceptions'))return;
    const data=classUI.data,counts={...data.counts,identity_photos:summary.identity_photos};
    const template=document.createElement('template');template.innerHTML=exceptionsHTML({...data,counts});
    replaceVisualContent(band,template.content.firstElementChild.innerHTML);
  }
  function grades(data){
    const students=data.students||[],archived=data.class_archived;
    return `<section class="card" id="assignment-summary"><div class="card-content summary-strip"><div class="grade-kpi"><span>已交 / 应交 · 人</span><strong data-visual-number="submitted">${data.counts?.submitted??'—'} / ${data.counts?.expected??'—'}</strong></div><div class="grade-kpi"><span>已计分 · 份</span><strong data-visual-number="completed">${data.counts?.completed??'—'}</strong></div><div class="grade-kpi"><span>最终均分 / 满分</span><strong data-visual-number="average">${data.final_average==null?'—':`${numberText(data.final_average)} / ${numberText(data.total_points)}`}</strong></div></div></section>${exceptionsHTML(data)}<section class="card" id="assignment-students"><div class="card-header"><div><h2>本次成绩</h2><p>点击姓名查看本次答卷；未交学生进入上传，不会建立空答卷。</p></div><div class="button-row">${archived?'':`<a class="btn primary" href="${escapeHTML(href(data,'upload'))}" data-nav>上传照片</a>`}<details class="action-menu"><summary class="btn">更多操作</summary><div class="action-menu-content"><a class="btn" href="/assignments/${data.id}/grades.xlsx">导出 Excel</a><a class="btn" href="/assignments/${data.id}/grades.csv">导出 CSV</a><a class="btn" href="/assignments/${data.id}/answer-card" target="_blank" rel="noopener">打印答题卡</a><a class="btn" href="/assignments/${data.id}/answer-card/download?format=pdf">下载 PDF</a><a class="btn" href="/assignments/${data.id}/answer-card/download?format=docx">下载 Word</a><a class="btn" data-nav href="/tasks/${data.task_id}">标准答案与布置</a><a class="btn" data-nav href="/classes/${data.class_id}">班级与历史档案</a>${archived?'':'<button class="btn" data-class-action="assignment-add">补充应交学生</button><button class="btn" data-class-action="copy-unsubmitted">复制未交名单</button>'}</div></details></div></div><div class="card-content">${recognitionControls('assignment',students)}</div>${data.counts?.submitted===0?`<p class="card-content notice">还没有照片，${archived?'恢复班级后可上传。':`<a href="${escapeHTML(href(data,'upload'))}" data-nav>上传照片</a>`}</p>`:''}<div class="table-wrap recognition-table"><table><thead><tr><th>学号</th><th>学生</th><th>成绩</th><th>批改状态与识别情况</th><th>教师核对</th><th>操作</th></tr></thead><tbody>${students.map(s=>{
      const dest=s.submission_id?`/submissions/${s.submission_id}`:href(data,'upload')+`&student=${s.id}`;
      return `<tr data-class-assignment-row="${s.id}" data-recognition-id="${s.id}"><td>${escapeHTML(s.student_code)}</td><td>${!s.submission_id&&archived?escapeHTML(s.name):`<a data-nav ${s.submission_id?'data-student-answer':`data-upload-student="${s.id}"`} href="${escapeHTML(dest)}"><strong>${escapeHTML(s.name)}</strong></a>`}${s.exempt?'<span class="tiny muted"> · 免交</span>':''}</td><td>${scoreHTML(s,data.total_points)}</td><td>${recognitionHTML(s,'assignment')}</td><td>已核对 ${numberText(s.confirmed_count)} 题</td><td><div class="button-row">${s.submission_id?`<a class="btn small" data-nav href="${dest}">查看成绩</a>`:!archived&&!s.exempt?`<a class="btn small" data-nav href="${escapeHTML(dest)}">上传照片</a>`:''}${!archived?`<button class="btn small" data-class-action="exempt" data-id="${s.id}">${s.exempt?'恢复应交':'免交'}</button>`:''}</div></td></tr>`;
    }).join('')}</tbody></table></div><div id="assignment-recognition-empty" class="card-content muted" hidden>没有符合筛选条件的学生。</div></section>`;
  }
  function cardSettings(data){
    const card=data.answer_card||{},fmt=card.format,s=fmt||card.suggestion,archived=data.class_archived,links=`<a class="btn small" href="/assignments/${data.id}/answer-card" target="_blank" rel="noopener">打印答题卡</a><a class="btn small" href="/assignments/${data.id}/answer-card/download?format=pdf">下载 PDF</a><a class="btn small" href="/assignments/${data.id}/answer-card/download?format=docx">下载 Word</a>`;
    const status=fmt?`<p><span class="badge blue">已保存</span> 学号 ${escapeHTML(card.description)} · 模板 <code>${escapeHTML(fmt.format_code)}</code></p>`:`<p class="muted">尚未保存：打印的是原有完整学号栏，身份照旧按学号 / 姓名匹配。</p>`;
    const frozen=fmt&&!card.editable?`<p class="tiny muted">本次作业已有上传照片，格式已固定，避免已打印的纸张失配；如需新格式请另开作业。</p>`:'';
    const form=!archived&&card.editable&&s?`<div class="form-grid" data-card-format><label class="field">学号总位数<input type="number" min="1" max="20" name="id_length" value="${Number(s.id_length)}"></label><label class="field">预印固定前缀<input name="id_prefix" inputmode="numeric" value="${escapeHTML(s.id_prefix)}" placeholder="留空则学生填写全部"><span class="tiny muted">须与本次名单全部学号开头一致；印在卡上，学生不用写。</span></label><label class="field">学号填写方式<select name="entry_mode">${(card.supported_modes||[]).map(m=>`<option value="${escapeHTML(m)}">${m==='handwritten'?'手写数字（每格一位）':escapeHTML(m)}</option>`).join('')}</select><span class="tiny muted">填涂识别尚未完成验收，暂不提供。</span></label><p data-card-variable class="notice"></p><div class="button-row"><button class="btn primary" type="button" data-card-save>${fmt?'保存修改':'保存本次答题卡格式'}</button>${fmt?'<button class="btn" type="button" data-card-clear>清除，改回原学号栏</button>':''}</div></div>`:'';
    const problem=!s&&card.problem?`<p class="notice warn">${escapeHTML(card.problem)}</p>`:'';
    return `<details class="card" id="answer-card-settings" ${fmt?'':'open'}><summary class="class-details-summary">答题卡设置（学号栏）${fmt?` <span class="badge blue">${escapeHTML(fmt.format_code)}</span>${card.editable?'':' <span class="tiny muted">已上传照片，格式已固定</span>'}`:''}</summary><div class="card-content">${status}${problem}${frozen}${form}<p class="tiny muted">保存后先打印预览核对，再发给学生。学号栏仍是机器读数：写错或误读成另一位学生的学号时系统不一定能发现，请抽查“学号栏匹配”的照片。上传独立答题卡时请在下方选择“独立答题卡（按题号作答）”。</p><div class="button-row">${links}</div></div></details>`;
  }
  function cardVariable(){
    const box=$('[data-card-format]');if(!box)return;const n=Number(box.querySelector('[name=id_length]').value),p=box.querySelector('[name=id_prefix]').value.trim(),left=n-p.length;
    $('[data-card-variable]').textContent=!Number.isInteger(n)||n<1?'请填写学号总位数。':left<1?'预印前缀须短于学号总位数，至少留一位由学生填写。':/\D/.test(p)?'预印前缀只能填写数字。':p?`卡上印“学号前缀：${p}”，学生填写后 ${left} 位（${left} 个方格）。`:`学生填写全部 ${n} 位（${n} 个方格）。`;
  }
  async function saveCard(button,body){
    if(state.busy)return;state.busy=true;button.disabled=true;setButtonBusy(button,true);clearActionError(button);
    try{const card=await api(`/api/assignments/${classUI.data.id}/answer-card-format`,{method:'POST',body});classUI.data.answer_card=card;$('#answer-card-settings').outerHTML=cardSettings(classUI.data);$('#answer-card-settings').open=true;cardVariable();toast(body.clear?'已改回原学号栏。':'答题卡格式已保存，请打印预览核对。');}
    catch(error){showActionError(button,error);button.disabled=false;}
    finally{state.busy=false;setButtonBusy(button,false);}
  }
  function uploadPage(data){
    const target=data.students.find(s=>s.id===Number(new URLSearchParams(location.search).get('student'))&&!s.exempt);
    return cardSettings(data)+`<section class="card"><div class="card-header"><div><h2>上传本次照片</h2><p>选择后先检查文件，再提交。照片交给后台后可以离开。</p></div></div><div class="card-content form-grid">${target?`<div id="upload-target" class="notice">指定学生：<strong>${escapeHTML(target.student_code)} ${escapeHTML(target.name)}</strong><p>上传后仍须对照原图明确确认归属，不会自动覆盖识别结果。</p><button class="btn small" data-upload-cancel-target>取消指定</button></div>`:''}${answerLayoutField('batch-answer-layout')}${uploadZone({id:'batch-photo-input',multiple:!target})}<div id="upload-file-list" role="status" aria-live="polite"></div><div class="button-row"><button class="btn primary" data-workspace-upload>上传并自动批改</button></div><div id="batch-progress" tabindex="-1" role="status" aria-live="polite"></div><div id="batch-job-status" role="status" aria-live="polite"></div><details class="more-operations"><summary>更多操作 · 识别维护</summary><p class="tiny muted">处理或重试识别可能调用云端并消耗额度；重新匹配仅用已有文字。</p><div class="button-row"><button class="btn" data-class-action="batch-identify-all">重试未完成识别</button><button class="btn" data-class-action="batch-match">重新匹配已有文字</button><button class="btn" data-class-action="batch-grade-all">重试未出成绩答卷</button></div></details></div></section>${archive()}`;
  }
  function archive(){return '<details class="card" id="assignment-photo-archive"><summary class="class-details-summary">照片档案 <span id="photo-archive-count"></span></summary><div class="card-content"><p id="batch-match-summary"></p><div id="batch-photo-queue">正在读取已上传照片…</div></div></details>';}
  function attentionPage(){return '<section class="card" id="assignment-attention"><div class="card-header"><div><h2>待核对</h2><p>归属按照片、失败与待批改按答卷、不确定按题目计数。</p></div></div><div class="card-content"><div class="recognition-toolbar"><label class="field recognition-search">搜索学生<input id="attention-search" type="search" placeholder="姓名、学号或照片编号"></label><label class="field">项目类型<select id="attention-kind"><option value="all">全部</option><option value="question">识别不确定</option><option value="identity">归属待确认</option><option value="grading_failed">批改失败</option><option value="ungraded">待批改</option></select></label><label class="field">排序<select id="attention-order"><option value="priority">异常优先</option><option value="number">按学号</option></select></label></div><p id="attention-count" role="status" aria-live="polite">正在读取状态…</p><div id="attention-results"></div></div></section>'+archive();}
  function attention(data){
    const panel=$('#attention-results');if(!panel)return;classUI.attention=data;
    if(!$('#batch-job-status')) panel.insertAdjacentHTML('beforebegin', '<div id="batch-job-status" role="status" aria-live="polite"></div>');
    if(!$('#ai-review-remaining')) panel.insertAdjacentHTML('beforebegin', '<button id="ai-review-remaining" class="btn" data-class-action="batch-ai-review">AI 复核剩余不确定题</button>');
    $('#ai-review-remaining').disabled=!!classUI.data?.class_archived || !!classUI.data?.archived || state.busy || ['queued','running'].includes(classUI.job?.state);
    const p=new URLSearchParams(location.search),q=p.get('a.q')||'',kind=p.get('a.kind')||'all',order=p.get('a.order')||'priority';
    $('#attention-search').value=q;$('#attention-kind').value=kind;$('#attention-order').value=order;
    const words=q.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean),rank={grading_failed:0,question:1,identity:2,ungraded:3};
    const items=[...data.items].filter(i=>(kind==='all'||i.kind===kind)&&words.every(w=>`${i.student_name||''} ${i.student_code||''} ${i.photo_id||''}`.toLocaleLowerCase().includes(w))).sort((a,b)=>(order==='priority'?(rank[a.kind]??4)-(rank[b.kind]??4):0)||String(a.student_code||'').localeCompare(String(b.student_code||''),'zh-CN',{numeric:true}));
    $('#attention-count').textContent=`显示 ${items.length} / ${data.summary.attention_count} 项 · 归属 ${data.summary.identity_photos} 张照片 · 失败 ${data.summary.failed_submissions} 份答卷 · 待批改 ${data.summary.ungraded_submissions} 份答卷 · 不确定 ${data.summary.uncertain_questions} 题`;
    const count=$('[data-workspace-count]');if(count){updateVisualNumber(count,data.summary.attention_count);count.classList.toggle('has-items',data.summary.attention_count>0);}
    panel.innerHTML=items.length?`<div class="attention-list">${items.map(i=>`<article class="attention-item" data-attention-item="${escapeHTML(i.id)}"><div><strong>${escapeHTML(i.student_name||`照片 #${i.photo_id}`)}</strong> <span class="muted tiny">${escapeHTML(i.student_code||'')}${i.number?` · 第 ${i.number} 题`:''}</span><p>${escapeHTML(i.reason)}</p></div>${i.kind==='identity'?`<button class="btn primary small" data-class-action="batch-confirm" data-id="${i.photo_id}">确认学生归属</button>`:`<a class="btn primary small" data-nav href="/submissions/${i.submission_id}?attention=1&queue=assignment&from=${encodeURIComponent(location.pathname+location.search)}${i.question_id?`&question=${i.question_id}&filter=uncertain`:''}">${i.kind==='question'?'核对本题':i.kind==='grading_failed'?'查看失败原因':'开始批改'}</a>`}</article>`).join('')}</div>`:`<p class="notice">${data.items.length?'没有符合筛选条件的项目。':data.summary.processing_photos||['queued','running'].includes(classUI.job?.state)?'正在后台处理；暂不展示待核对题，完成后自动刷新。':'未发现识别异常，可按需抽查。'}</p>`;
  }
  async function refresh(background=false){
    const match=location.pathname.match(/^\/assignments\/(\d+)\/?$/);if(!match)return false;
    const version=state.routeVersion,data=await api(`/api/assignments/${match[1]}`);if(version!==state.routeVersion)return false;
    if(background&&hasListInteraction())return false;
    classUI.data=data;classUI.refreshPending=false;
    if(tab()==='grades'){
      const old=$('#assignment-students'),tpl=document.createElement('template');tpl.innerHTML=grades(data);
      markChangedNumbers($('#assignment-summary'),tpl.content.querySelector('#assignment-summary'));
      markChangedNumbers($('tbody',old),tpl.content.querySelector('tbody'));
      $('#assignment-summary')?.replaceWith(tpl.content.querySelector('#assignment-summary'));
      const band=$('#assignment-exceptions'),freshBand=tpl.content.querySelector('#assignment-exceptions');
      if(band&&freshBand)band.replaceWith(freshBand);
      // Keep the search, filter and sort controls alive; only replace result data.
      $('tbody',old)?.replaceWith(tpl.content.querySelector('tbody'));
      $('.recognition-overview',old)?.replaceWith(tpl.content.querySelector('.recognition-overview'));
      mountRecognitionList('assignment',data.students);
      const count=$('[data-workspace-count]');if(count){updateVisualNumber(count,data.counts?.attention_items??'—');count.classList.toggle('has-items',data.counts?.attention_items>0);}
    }
    await loadBatchPhotos(data.id);await loadAssignmentWorkbench(data.id);return true;
  }
  async function routePage(id){
    clearTimeout(poll);clearTimeout(classJobTimer);normalize();const version=state.routeVersion;
    const data=await api(`/api/assignments/${id}`);if(version!==state.routeVersion)return;
    if(data.class_archived&&tab()!=='grades'){const p=new URLSearchParams(location.search);p.set('tab','grades');p.delete('photo');Nav.patchURL(p);}
    classUI.data=data;classUI.batchPhotos=[];classUI.batchSummary=null;classUI.job=null;classUI.attention=null;classUI.refreshPending=false;classUI.photoReadError='';classUI.startError='';
    $('#main').innerHTML=header(data)+(tab()==='upload'?uploadPage(data):tab()==='attention'?attentionPage():grades(data));
    if(tab()==='upload'){cardVariable();restoreReceipts(id);updateUpload();}
    if(tab()==='grades')mountRecognitionList('assignment',data.students);
    else {await loadBatchPhotos(data.id);if(version!==state.routeVersion)return;await loadAssignmentWorkbench(data.id);if(version!==state.routeVersion)return;const photo=Number(new URLSearchParams(location.search).get('photo'));if(photo&&!data.class_archived)await openBatchIdentity(photo);}
    if(tab()==='grades')scheduleGrades(version,id);
    if(tab()==='upload'&&location.hash==='#batch-progress')requestAnimationFrame(()=>requestAnimationFrame(()=>{
      if(version!==state.routeVersion)return;
      const panel=$('#batch-progress');
      panel?.focus({preventScroll:true});panel?.scrollIntoView({block:'center',behavior:'instant'});
    }));
  }
  function scheduleGrades(version,id){clearTimeout(poll);poll=setTimeout(async()=>{if(version!==state.routeVersion)return;if(!document.hidden&&!hasListInteraction()&&!document.activeElement?.matches('a,button')){try{await refresh(true)}catch{statusUnavailable();const p=$('#assignment-recognition-count');if(p)p.textContent='状态暂不可用，保留上次数据。';}}if(version===state.routeVersion)scheduleGrades(version,id)},3500);}
  const labels={pending:'未提交',uploading:'上传中',confirmed:'已确认',failed:'明确失败',unknown:'响应未确认'};
  const blocked=()=>['queued','running','interrupted','failed'].includes(classUI.job?.state);
  const receiptKey=aid=>`batch-upload-receipts:${aid}`;
  const failureKey=aid=>`batch-upload-failures:${aid}`;
  const uploadId=()=>Array.from(crypto.getRandomValues(new Uint8Array(16)),b=>b.toString(16).padStart(2,'0')).join('');
  async function fingerprint(file){
    return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',await file.arrayBuffer())),b=>b.toString(16).padStart(2,'0')).join('');
  }
  function rememberReceipts(){
    // Store receipt metadata only, never the photo or student name.
    try {
      const records=upload.entries.filter(e=>['uploading','unknown'].includes(e.status)).map(({id,name,digest,layout,target})=>({id,name,digest,layout,target}));
      if(records.length)sessionStorage.setItem(receiptKey(upload.aid),JSON.stringify(records));
      else sessionStorage.removeItem(receiptKey(upload.aid));
      return true;
    }catch{return false;}
  }
  function storedUploads(key){
    try{
      const records=JSON.parse(sessionStorage.getItem(key)||'[]');
      return Array.isArray(records)?records.filter(e=>e&&/^[0-9a-f]{32}$/.test(e.id)&&typeof e.name==='string'):[];
    }catch{return [];}
  }
  function restoreReceipts(aid){
    const unknown=storedUploads(receiptKey(aid));
    const failed=storedUploads(failureKey(aid)).filter(e=>!unknown.some(r=>r.id===e.id));
    upload={aid,entries:[
      ...unknown.map(e=>({...e,status:'unknown',file:null})),
      ...failed.map(e=>({...e,status:'failed',file:null}))
    ]};
    rememberFailures();
  }

  function uploadIssues(aid){
    const unknown=storedUploads(receiptKey(aid));
    const failed=storedUploads(failureKey(aid)).filter(e=>!unknown.some(r=>r.id===e.id));
    return {unknown:unknown.length,failed:failed.length};
  }
  function rememberFailures(){
    try{
      const failed=upload.entries.filter(e=>e.status==='failed').map(({id,name,digest,layout,target})=>({id,name,digest,layout,target}));
      if(failed.length)sessionStorage.setItem(failureKey(upload.aid),JSON.stringify(failed));
      else sessionStorage.removeItem(failureKey(upload.aid));
    }catch{}
  }
  function updateUpload(){
    const input=$('#batch-photo-input'), list=$('#upload-file-list'), panel=$('#batch-progress');
    if(!input||!list||!panel)return;
    const files=new DataTransfer();
    for(const e of upload.entries)if(e.file&&['pending','failed'].includes(e.status))files.items.add(e.file);
    input.files=files.files;updateUploadFiles(input);
    list.innerHTML=upload.entries.map((e,i)=>`<div class="upload-file" data-upload-state="${e.status}"><span>${escapeHTML(e.name)} · ${labels[e.status]}${e.photo?` · 照片 #${e.photo.id}`:''}${['failed','unknown'].includes(e.status)&&!e.file?' · 可重新选择原文件':''}${e.error?` · ${escapeHTML(e.error)}`:''}</span>${['pending','failed'].includes(e.status)?`<button class="btn small" type="button" data-upload-remove="${i}" aria-label="移除 ${escapeHTML(e.name)}" ${state.busy?'disabled':''}>移除</button>`:''}</div>`).join('');
    const count=status=>upload.entries.filter(e=>e.status===status).length;
    const unknown=count('unknown'), failed=count('failed'), confirmed=count('confirmed'), pending=count('pending');
    panel.innerHTML=upload.entries.length?`已确认上传 ${confirmed} 张；未提交 ${pending} 张；上传中 ${count('uploading')} 张；明确失败 ${failed} 张；响应未确认 ${unknown} 张。`:'';
    if(upload.entries.some(e=>e.status==='failed'&&!e.file))panel.insertAdjacentHTML('beforeend','<p class="notice warn">请重新选择失败照片的原文件，再点击重试。也可移除无需处理的记录。</p>');
    if(upload.entries.some(e=>e.status==='failed'&&e.file))panel.insertAdjacentHTML('beforeend',` <button class="btn small" data-upload-retry ${state.busy||blocked()?'disabled':''}>重试失败照片</button>`);
    if(unknown){
      panel.insertAdjacentHTML('beforeend',` <button class="btn small" data-upload-check ${state.busy?'disabled':''}>核对上传记录</button>`);
      if(upload.entries.some(e=>e.status==='unknown'&&e.missing&&e.file))
        panel.insertAdjacentHTML('beforeend',` <button class="btn small" data-upload-resend ${state.busy||blocked()?'disabled':''}>按原上传标识重试</button>`);
    }
  }
  async function readReceipts(){
    const aid=upload.aid,version=state.routeVersion;
    await loadBatchPhotos(aid);
    if(version!==state.routeVersion)return [];
    const found=[];
    for(const e of upload.entries.filter(e=>e.status==='unknown')){
      const saved=classUI.photoReadError?null:classUI.batchPhotos.find(p=>p.upload_id===e.id);
      if(saved){
        Object.assign(e,{status:'confirmed',photo:saved,error:'',file:null,missing:false});
        found.push(e);
      }else{
        e.missing=!classUI.photoReadError;
        e.error=e.missing?'暂未找到对应记录，可核对后按原标识重试。':'暂时无法读取照片档案，请稍后核对。';
      }
    }
    rememberReceipts();
    $('#assignment-photo-archive').open=true;renderBatchPhotos();
    return found;
  }
  async function afterUploads(accepted){
    const target=accepted.find(e=>e.target&&!e.photo.submission_id);
    if(target){
      if(!classUI.batchPhotos.some(p=>p.id===target.photo.id))await loadBatchPhotos(upload.aid);
      await openBatchIdentity(target.photo.id);
      const select=$('#class-batch-confirm [name=assignment_student_id]');
      if(select){select.value=String(target.target);select.dispatchEvent(new Event('change',{bubbles:true}));}
    }else if(!blocked()&&accepted.some(e=>!e.target&&!e.photo.submission_id))await beginBackgroundGrading();
  }
  async function submitFiles(button,mode='pending'){
    if(state.busy||blocked()&&mode!=='check')return;
    const entries=upload.entries.filter(e=>mode==='resend'?e.status==='unknown'&&e.missing&&e.file:e.status===mode&&e.file);
    if(!entries.length&&mode!=='check')return toast('没有可提交的照片。',true);
    if(entries.length>100)return toast('单次最多100张照片。',true);
    const target=Number(new URLSearchParams(location.search).get('student'));
    if(target&&mode!=='check'&&entries.length!==1)return toast('指定学生时请一次上传一张，逐张确认归属。',true);
    if(mode==='pending'&&upload.entries.some(e=>e.status==='unknown'))return toast('请先核对响应未确认的照片。',true);
    if(entries.some(({file:f})=>!/^image\/(jpeg|png)$/.test(f.type)||!/\.(jpe?g|png)$/i.test(f.name)||f.size>10*1024*1024))
      return toast('请选择不超过10MB的 JPG / PNG。',true);
    state.busy=true;setButtonBusy(button,true);clearActionError(button);
    const version=state.routeVersion,aid=upload.aid,accepted=[];
    const controls=$$('#batch-photo-input,#batch-answer-layout,[data-workspace-upload],[data-upload-cancel-target]');
    controls.forEach(el=>el.disabled=true);
    try{
      if(mode==='check')accepted.push(...await readReceipts());
      else for(const entry of entries){
        const prior=entry.status;
        entry.layout??=$('#batch-answer-layout').value;
        if(prior!=='unknown')entry.target=target;
        entry.digest??=await fingerprint(entry.file);
        Object.assign(entry,{status:'uploading',error:''});
        if(!rememberReceipts()){
          // The current page can still reconcile. Avoid hiding the reload limitation.
          toast('浏览器无法保存上传标识；请在此页核对结果后再离开。',true);
        }
        updateUpload();
        const body=new FormData();
        body.append('image',entry.file);body.append('answer_layout',entry.layout);body.append('upload_id',entry.id);
        try{
          const result=await api(`/api/assignments/${aid}/photos`,{method:'POST',body,signal:AbortSignal.timeout(30000)});
          if(!Number.isInteger(result.id)||result.upload_id!==entry.id)throw new Error('服务器未返回可核对的上传回执。');
          Object.assign(entry,{status:'confirmed',photo:result,file:null});
          accepted.push(entry);
        }catch(error){
          const failed=prior!=='unknown'&&[400,403,404,409,413,415,422].includes(error.status);
          Object.assign(entry,{status:failed?'failed':'unknown',missing:false,error:error.message});
          if(!failed){accepted.push(...await readReceipts());break;}
        }finally{rememberReceipts();rememberFailures();updateUpload();}
      }
      if(accepted.length)await afterUploads(accepted);
    }catch(error){if(version===state.routeVersion)showActionError(button,error);}
    finally{
      if(version===state.routeVersion){
        state.busy=false;setButtonBusy(button,false);
        controls.forEach(el=>{if(el.isConnected)el.disabled=false});
        updateUpload();renderGradingJob(classUI.job);
      }
    }
  }
  document.addEventListener('change',async e=>{
    if(e.target.id==='batch-photo-input'&&!state.busy){
      const files=[...e.target.files];
      const unknown=upload.entries.filter(item=>item.status==='unknown');
      const detached=upload.entries.filter(item=>!item.file&&['unknown','failed'].includes(item.status));
      state.busy=true;e.target.disabled=true;
      try{
        for(const file of files){
          if(detached.length){
            const digest=await fingerprint(file);
            const match=detached.find(item=>!item.file&&item.digest===digest);
            if(match){match.file=file;continue;}
          }
          if(unknown.length){toast('请先核对未确认项；只能重新选择与原照片内容相同的文件。',true);continue;}
          if(upload.entries.some(item=>item.file&&item.name===file.name&&item.file.size===file.size&&item.file.lastModified===file.lastModified))continue;
          upload.entries.push({id:uploadId(),name:file.name,file,status:'pending'});
        }
      }catch(error){toast(error.message,true);}
      finally{state.busy=false;}
      updateUpload();renderGradingJob(classUI.job);
    }
    if(['attention-kind','attention-order'].includes(e.target.id))updateAttention(e.target.id==='attention-kind'?'kind':'order',e.target.value);
  });
  function updateAttention(key,value){const p=new URLSearchParams(location.search);value&&value!=='all'?p.set('a.'+key,value):p.delete('a.'+key);Nav.patchURL(p);if(classUI.attention)attention(classUI.attention);}
  document.addEventListener('input',e=>{if(e.target.id==='attention-search'&&!e.isComposing)updateAttention('q',e.target.value);if(e.target.closest('[data-card-format]'))cardVariable();});
  document.addEventListener('compositionend',e=>{if(e.target.id==='attention-search')updateAttention('q',e.target.value);});
  document.addEventListener('click',e=>{
    const card=e.target.closest('[data-card-save],[data-card-clear]');
    if(card){const box=$('[data-card-format]');if(card.hasAttribute('data-card-clear')){if(confirm('清除后打印的是原有完整学号栏，确定吗？'))saveCard(card,{clear:true});}else saveCard(card,{entry_mode:box.querySelector('[name=entry_mode]').value,id_length:Number(box.querySelector('[name=id_length]').value),id_prefix:box.querySelector('[name=id_prefix]').value.trim()});return;}
    const b=e.target.closest('[data-workspace-upload],[data-upload-retry],[data-upload-check],[data-upload-resend],[data-upload-remove],[data-upload-cancel-target]');if(!b||state.busy)return;

    if(b.hasAttribute('data-upload-remove')){const [entry]=upload.entries.splice(Number(b.dataset.uploadRemove),1);if(entry?.status==='failed')rememberFailures();updateUpload();}
    else if(b.hasAttribute('data-upload-cancel-target')){const p=new URLSearchParams(location.search);p.delete('student');Nav.patchURL(p);$('#upload-target')?.remove();$('#batch-photo-input').multiple=true;}
    else submitFiles(b,b.hasAttribute('data-upload-check')?'check':b.hasAttribute('data-upload-resend')?'resend':b.hasAttribute('data-upload-retry')?'failed':'pending');
  });
  return {header,analysisHref,uploadIssues,route:routePage,refresh,attention,statusUnavailable,updateExceptionDetails,hasFiles:()=>upload.entries.some(e=>e.file||e.status==='unknown'),clearUpload:()=>{upload={entries:[],aid:null}}};
})();


