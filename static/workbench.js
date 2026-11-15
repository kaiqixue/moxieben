'use strict';

const Workbench = (()=>{
  let rows=[],order=[],latest=null,timer,stale=false;
  const sorted=data=>[...data].sort((a,b)=>(a.status==='unavailable')-(b.status==='unavailable')||(b.attention_items||0)-(a.attention_items||0)||String(b.created_at).localeCompare(String(a.created_at))||b.id-a.id);
  const isArchived = row => row.archived || row.class_archived;
  const visibleTodoCount = 4;
  const legacyList=()=>!new URLSearchParams(location.search).has('todos')&&
    ['class','q','attention','archived','all','view'].some(key=>new URLSearchParams(location.search).has(key));
  const isHome=()=>location.pathname==='/';
  const activeRows=()=>rows.filter(r=>!isArchived(r));
  function matchingRows() {
    const p=new URLSearchParams(location.search),query=(p.get('q')||'').toLocaleLowerCase().trim().split(/\s+/).filter(Boolean);
    const history=p.get('view')==='history';
    const byId=new Map(rows.map(r=>[r.id,r]));
    return order.map(id=>byId.get(id)).filter(Boolean).filter(r=>
      (!p.get('class')||String(r.class_id)===p.get('class'))&&(history?isArchived(r):!isArchived(r)||p.get('archived')==='1')&&
      (p.get('attention')!=='1'||r.attention_items>0||r.status==='unavailable')&&query.every(w=>`${r.class_name} ${r.task_name} ${r.term}`.toLocaleLowerCase().includes(w)));
  }
  function progress(r){
    if(stale||r.status==='unavailable'||!Number.isFinite(r.submitted)||!Number.isFinite(r.expected))return '<strong data-visual-number="submitted">—</strong><span class="tiny muted">提交状态暂不可用</span>';
    const ratio=r.expected>0?Math.min(100,Math.max(0,r.submitted/r.expected*100)):0;
    return `<strong data-visual-number="submitted">${r.submitted} / ${r.expected} 人</strong><span class="tiny muted">已交 / 应交</span><span class="workbench-progress" aria-hidden="true"><span class="workbench-progress-fill" style="--progress:${ratio}%"></span></span>`;
  }
  const todoTypes=[
    ['upload-unknown','响应待确认','张','核对上传记录','amber'],
    ['upload-failed','上传失败','张','处理上传失败','red'],
    ['grading_failed','批改失败','份','查看失败答卷','red'],
    ['identity','待归属照片','张','确认归属','amber'],
    ['question','识别不确定','题','核对题目','blue'],
    ['ungraded','待批改答卷','份','查看待批改','amber'],
    ['missing','未交学生','人','查看未交名单','']
  ];
  function todoList(){
    if(!isHome())return;
    const all=new URLSearchParams(location.search).get('todos')==='all';
    const active=activeRows();
    overview(active);
    const localIssues=new Map(active.map(r=>[r.id,Workspace.uploadIssues(r.id)]));
    const tasks=todoTypes.flatMap(([kind,label,unit,action,tone])=>active.flatMap(r=>{
      const assignment=`/assignments/${r.id}`;
      const issues=localIssues.get(r.id);
      const first=r.first_uncertain;
      const count={'upload-unknown':issues.unknown,'upload-failed':issues.failed,grading_failed:r.failed,
        identity:r.pending_identity_photos,question:r.uncertain_questions,ungraded:r.ungraded,missing:r.expected-r.submitted}[kind];
      const local=kind.startsWith('upload-');
      if(!Number.isFinite(count)||count<=0||(!local&&(stale||r.status==='unavailable')))return [];
      let href=`${assignment}?tab=attention&a.kind=${kind}`;
      if(local)href=`${assignment}?tab=upload#batch-progress`;
      else if(kind==='missing')href=`${assignment}?tab=grades&g.view=not_uploaded`;
      else if(kind==='question'&&first?.submission_id&&first.question_id){
        const params=new URLSearchParams({question:first.question_id,filter:'uncertain',attention:'1',queue:'assignment'});
        href=`/submissions/${first.submission_id}?${params}`;
      }
      return [`<li class="workbench-todo" data-todo-kind="${kind}">
        <div class="workbench-todo-name">${badge(label,tone)} <strong>${escapeHTML(r.task_name)}</strong><small>${escapeHTML(r.class_name)}${local?' · 此浏览器会话':''}</small></div>
        <strong class="workbench-todo-count">${numberText(count)} ${unit}</strong>
        <a id="todo-${kind}-${r.id}" class="btn small ${kind==='question'?'primary':''}" data-nav href="${escapeHTML(href)}">${action}</a>
      </li>`];
    }));
    const unknown=stale||active.some(r=>r.status==='unavailable');
    const message=unknown?'部分状态暂不可用，请重新读取。':!rows.length?'还没有班级作业。':
      !active.length?'归档作业可在作业管理查看。':
      active.some(r=>r.processing>0)?'作业正在后台处理，完成后可查看成绩。':'当前没有待处理事项，可在作业列表查看成绩。';
    const shown=all?tasks:tasks.slice(0,visibleTodoCount);
    const html=(unknown||!shown.length?`<p class="workbench-todo-message${!unknown&&active.length&&!active.some(r=>r.processing>0)?' is-complete':''}${unknown?' notice warn':''}" role="status">${message}</p>`:'')+
      (shown.length?`<ul class="workbench-todo-list">${shown.join('')}</ul>`:'')+
      (tasks.length>visibleTodoCount?`<div class="workbench-todo-footer"><span class="muted">显示 ${shown.length} / ${tasks.length} 条</span><button class="btn small" id="workbench-toggle-todos" data-workbench-todos aria-expanded="${all}">${all?'收起待办':'查看全部待办'}</button></div>`:'');
    replaceVisualContent($('#workbench-todos'),html);
  }
  function table() {
    const p=new URLSearchParams(location.search),matching=matchingRows();
    const shown=p.get('all')==='1'?matching:matching.slice(0,20),body=$('#workbench-results');if(!body)return;
    body.innerHTML=shown.length?`<div class="workbench-cards">${shown.map(r=>`<article class="workbench-assignment" data-workbench-row="${r.id}"><span class="class-monogram" aria-hidden="true">${escapeHTML([...r.class_name].slice(0,2).join(''))}</span><div class="workbench-assignment-title"><a data-nav data-workbench-title id="assignment-title-${r.id}" href="/assignments/${r.id}?tab=grades"><strong>${escapeHTML(r.task_name)}</strong></a><p class="muted">${escapeHTML(r.class_name)} · ${escapeHTML(r.term||'本次作业')} · ${dateText(r.created_at)}${r.class_archived?' · 班级已归档':r.archived?' · 已归档':''}</p><div data-workbench-status>${status(r)}</div></div><div class="workbench-submitted" data-workbench-submitted>${progress(r)}</div><div data-workbench-actions>${actions(r)}</div></article>`).join('')}</div>`:empty(rows.length?'没有符合条件的作业':'还没有班级作业',rows.length?'试试清空筛选或查看全部作业。':'新建作业并布置给班级后，就会出现在这里。',rows.length?'<button class="btn" data-workbench-reset>清空筛选</button>':'<a class="btn primary" href="/tasks/new?workflow=1" data-nav>新建并布置作业</a>');
    $('#workbench-count').textContent=`显示 ${shown.length} / ${matching.length} 次班级作业${p.get('all')==='1'?'':' · 默认最多20条'}`;
    $('#workbench-all').hidden=matching.length<=20&&p.get('all')!=='1';
    $('#workbench-all').textContent=p.get('all')==='1'?'收起为最近20条':'全部作业';
    for(const link of $$('[data-workbench-view]')){
      const params=new URLSearchParams(p);
      if(link.dataset.workbenchView==='history')params.set('view','history');
      else params.delete('view');
      link.href='/assignments'+(params.size?'?'+params:'');
    }
    p.get('all')==='1'?p.delete('all'):p.set('all','1');
    $('#workbench-all').setAttribute('href','/assignments'+(p.size?'?'+p:''));
  }
  function actions(r){
    const assignment=`/assignments/${r.id}`;
    const active=!isArchived(r);
    const archive=r.class_archived?'':`<button type="button" class="btn small" data-workbench-archive="${r.id}">${r.archived?'恢复作业':'归档本次作业'}</button>`;
    return `<div class="button-row">${active?`<a class="btn small" data-nav id="assignment-upload-${r.id}" href="${assignment}?tab=upload">上传照片</a>${r.attention_items?`<a class="btn small primary" data-nav id="assignment-attention-${r.id}" href="${assignment}?tab=attention">核对异常</a>`:''}`:''}<a class="btn small ${!active||!r.attention_items?'primary':''}" data-nav id="assignment-grades-${r.id}" href="${assignment}?tab=grades">查看成绩</a><a class="btn small" data-nav id="assignment-analysis-${r.id}" href="${assignment}/analysis">班级讲评</a>${archive}</div>`;
  }
  async function toggleArchive(button){
    const row=rows.find(r=>r.id===Number(button.dataset.workbenchArchive));
    if(!row)return;
    const archived=!row.archived;
    await busyAction(button,async()=>{
      await api(`/api/assignments/${row.id}`,{method:'PATCH',body:{archived}});
      row.archived=archived;
      rows=[...rows];latest=rows;
      table();
      ($(`[data-workbench-archive="${row.id}"]`)||$('[data-home-param="archived"]'))?.focus();
      toast(archived?'本次作业已归档，可在「历史作业」中查看和恢复。':'作业已恢复。');
    });
  }
  function status(r){return stale||r.status==='unavailable'?badge('状态暂不可用','amber'):`<div class="recognition-status">${badge(`已计分 ${numberText(r.completed)} 份`)}${r.attention_items?badge(`${r.attention_items} 项待核对`,'amber'):r.processing?'':`<span class="tiny muted">${r.submitted===0?'尚未收到答卷':'未发现识别异常'}</span>`}${r.processing?badge(`处理中 ${r.processing} 项`,'blue','data-processing'):''}</div>`;}
  function overview(active){
    const panel=$('#workbench-overview');
    if(stale){
      replaceVisualContent(panel,'<strong>工作概览暂不可用</strong>');
      return;
    }
    let partial=false;
    const metrics=[['question','识别不确定','题','uncertain_questions'],['identity','待确认归属','张','pending_identity_photos']];
    const html=metrics.map(([kind,label,unit,field])=>{
      const known=active.filter(r=>r.status==='ok'&&Number.isFinite(r[field]));
      const missing=known.length<active.length;
      partial ||= missing;
      const count=known.reduce((sum,r)=>sum+r[field],0);
      const text=`${label} <strong>${missing?(known.length?`已知 ${count} ${unit}`:'暂不可用'):`${count} ${unit}`}</strong>`;
      return count?`<button class="btn small" type="button" data-workbench-metric="${kind}">${text}</button>`:`<span>${text}</span>`;
    }).join('');
    replaceVisualContent(panel,html+(partial?'<small>部分作业状态暂不可用</small>':''));
  }
  function recent(){
    const recentRows=activeRows().sort((a,b)=>String(b.created_at).localeCompare(String(a.created_at))||b.id-a.id).slice(0,3);
    const html=recentRows.map(r=>`<li class="workbench-recent-row"><div><strong>${escapeHTML(r.task_name)}</strong><small>${escapeHTML(r.class_name)} · ${stale||r.status!=='ok'?'提交状态暂不可用':`${numberText(r.submitted)} / ${numberText(r.expected)} 人已交`}</small></div><div class="button-row"><a class="btn small" id="recent-grades-${r.id}" href="/assignments/${r.id}?tab=grades" data-nav>成绩</a><a class="btn small" id="recent-analysis-${r.id}" href="/assignments/${r.id}/analysis" data-nav>讲评</a></div></li>`).join('');
    replaceVisualContent($('#workbench-recent'),html?`<ul>${html}</ul>`:`<p class="workbench-todo-message">${stale?'近期作业暂不可用。':'暂无近期作业。'}</p>`);
  }
  function homeShell(){
    $('#breadcrumb').textContent='工作台';
    $('#main').innerHTML=heading('教师工作台','')+
      `<nav class="workbench-quick" aria-label="快捷操作">
        <a class="btn primary" href="/tasks/new?workflow=1" data-nav>新建作业</a>
        <button class="btn" id="workbench-upload" type="button" data-workbench-upload>上传答卷</button>
        <a class="btn" href="/classes" data-nav>管理班级</a>
      </nav>
      <div id="workbench-overview" class="workbench-overview" role="region" aria-label="工作概览"></div>
      <p id="workbench-update" class="notice" role="status" hidden></p>
      <div class="workbench-home-grid">
        <section class="card workbench-tasks" aria-labelledby="workbench-todo-title"><div class="card-header"><h2 id="workbench-todo-title">待处理任务</h2></div><div id="workbench-todos"></div></section>
        <section class="card workbench-recent" aria-labelledby="workbench-recent-title"><div class="card-header"><h2 id="workbench-recent-title">近期创建</h2></div><div id="workbench-recent"></div><div class="workbench-library-footer"><a class="btn small" href="/assignments" data-nav>查看全部作业</a></div></section>
      </div>`;
    todoList();recent();
  }
  function libraryShell(){
    const p=new URLSearchParams(location.search),classes=[...new Map(rows.map(r=>[r.class_id,r.class_name])).entries()];
    const history=p.get('view')==='history';
    $('#breadcrumb').textContent='作业管理';
    $('#main').innerHTML=heading('作业管理','','<a class="btn primary" href="/tasks/new?workflow=1" data-nav>新建作业</a>')+
      `<nav class="workspace-nav" aria-label="作业视图"><a data-nav href="/assignments" data-workbench-view="all" ${history?'':'aria-current="page"'}>全部作业</a><a data-nav href="/assignments?view=history" data-workbench-view="history" ${history?'aria-current="page"':''}>历史作业</a><a href="/tasks" data-nav>标准答案与布置 / 临时答卷</a></nav>
      <p id="workbench-update" class="notice" role="status" hidden></p>
      <section class="card workbench-library" id="workbench-library"><div class="card-header"><div><h2>班级作业列表</h2><p id="workbench-count" role="status" aria-live="polite"></p></div><a class="btn small" id="workbench-all" href="/assignments?all=1" data-nav>全部作业</a></div>
      <div class="workbench-filters"><label class="field">班级<select id="workbench-class" data-home-param="class"><option value="">全部班级</option>${classes.map(([id,name])=>`<option value="${id}" ${String(id)===p.get('class')?'selected':''}>${escapeHTML(name)}</option>`).join('')}</select></label><label class="field recognition-search">搜索作业<input id="workbench-search" data-home-param="q" type="search" maxlength="120" value="${escapeHTML(p.get('q')||'')}" placeholder="班级、作业名称或学期"></label>
      <label class="inline-check"><input data-home-param="attention" type="checkbox" ${p.get('attention')==='1'?'checked':''}>只看待核对</label>
      ${history?'':`<label class="inline-check"><input data-home-param="archived" type="checkbox" ${p.get('archived')==='1'?'checked':''}>包含归档记录</label>`}</div><div id="workbench-results"></div></section>`;table();
  }
  function uploadHref(id){return `/assignments/${id}?tab=upload`;}
  async function openUpload(){
    if(document.querySelector('#choose-upload'))return;
    const button=$('#workbench-upload'),version=state.routeVersion;
    const dialog=document.createElement('dialog');
    dialog.id='choose-upload';
    dialog.setAttribute('aria-labelledby','choose-upload-title');
    dialog.innerHTML='<div class="dialog-header"><h2 id="choose-upload-title">选择本次作业</h2></div><p id="choose-upload-content" role="status">正在读取可上传作业…</p><form method="dialog"><button class="btn">关闭</button></form>';
    document.body.append(dialog);
    dialog.addEventListener('close',()=>{dialog.remove();button?.focus({preventScroll:true});},{once:true});
    dialog.showModal();
    const content=$('#choose-upload-content');
    const response=await api('/api/workbench/assignments').catch(()=>null);
    if(!dialog.open||state.routeVersion!==version)return;
    const list=response?.assignments.filter(r=>!isArchived(r)&&r.status==='ok'&&Number.isSafeInteger(r.id))||[];
    if(!response||(!list.length&&response.assignments.some(r=>!isArchived(r)&&r.status!=='ok'))){content.textContent='暂时无法读取可上传的作业，请稍后重试。';return;}
    if(list.length===1){dialog.close();await navigate(uploadHref(list[0].id));return;}
    if(!list.length){
      content.innerHTML='请先<a href="/tasks/new?workflow=1" data-upload-choice>新建并布置作业</a>。';
    }else{
      content.outerHTML=`<div id="choose-upload-content"><label class="field">班级与作业<select id="choose-upload-id">${list.map(r=>`<option value="${r.id}">${escapeHTML(r.class_name)} · ${escapeHTML(r.task_name)}</option>`).join('')}</select></label><div class="button-row"><button class="btn primary" type="button" data-upload-choice>前往上传</button></div></div>`;
    }
    dialog.addEventListener('click',async event=>{
      const choice=event.target.closest('[data-upload-choice]');
      if(!choice)return;
      event.preventDefault();
      const id=Number($('#choose-upload-id')?.value);
      if(list.length&&!list.some(row=>row.id===id))return;
      dialog.close();
      await navigate(list.length?uploadHref(id):'/tasks/new?workflow=1');
    });
  }
  function showReadError(){
    const panel=$('#workbench-update');
    panel.hidden=false;
    panel.innerHTML='状态暂不可用。 <button class="btn small" type="button" data-workbench-retry>重新读取</button>';
    if(isHome()){todoList();recent();}
    else for(const row of $$('[data-workbench-row]')){
      replaceVisualContent($('[data-workbench-status]',row),status({}));
      replaceVisualContent($('[data-workbench-submitted]',row),progress({}));
    }
  }
  function schedule(version){clearTimeout(timer);timer=setTimeout(async()=>{
    if(version!==state.routeVersion)return;
    const interacting=()=>state.busy||$('#choose-upload')?.open||document.hidden||hasListInteraction()||document.activeElement?.matches('a,button,summary');
    if(interacting()){schedule(version);return;}
    const previous=rows;
    try{
      const data=await api('/api/workbench/assignments');
      if(version!==state.routeVersion)return;
      if(previous!==rows||interacting()){schedule(version);return;}
      latest=data.assignments;rows=latest;stale=false;
      const panel=$('#workbench-update');
      if(isHome()){
        order=sorted(rows).map(r=>r.id);
        todoList();recent();panel.hidden=true;
      }else{
        const visible=matchingRows().slice(0,new URLSearchParams(location.search).get('all')==='1'?rows.length:20).map(r=>r.id);
        const displayed=$$('[data-workbench-row]').map(row=>Number(row.dataset.workbenchRow));
        if(JSON.stringify(visible)!==JSON.stringify(displayed))table();
        for(const el of $$('[data-workbench-row]')){
          const r=rows.find(r=>r.id===Number(el.dataset.workbenchRow));
          replaceVisualContent($('[data-workbench-status]',el),status(r));
          replaceVisualContent($('[data-workbench-actions]',el),actions(r));
          replaceVisualContent($('[data-workbench-submitted]',el),progress(r));
        }
        const changed=JSON.stringify(sorted(rows).map(r=>r.id))!==JSON.stringify(order);
        panel.hidden=!changed;
        panel.innerHTML=changed?'列表有更新。 <button class="btn small" type="button" data-workbench-refresh>刷新列表</button>':'';
      }
    }catch(error){
      if(version!==state.routeVersion)return;
      if(previous===rows&&!interacting()){stale=true;showReadError();}
    }
    schedule(version);
  },3000);}
  async function render(){
    if(isHome()&&legacyList())Nav.relocate('/assignments'+location.search+location.hash);
    const version=state.routeVersion;
    clearTimeout(timer);
    try{
      const data=await api('/api/workbench/assignments');if(version!==state.routeVersion)return;
      rows=data.assignments;latest=rows;stale=false;order=sorted(rows).map(r=>r.id);
    }catch(error){if(version!==state.routeVersion)return;stale=true;}
    if(isHome())homeShell();else libraryShell();
    if(stale)showReadError();
    schedule(version);
  }
  async function mountCreation(){
    if(new URLSearchParams(location.search).get('workflow')!=='1')return;
    const version=state.routeVersion;
    $('h1').textContent='新建并布置作业';
    const form=$('#new-task-form');form.insertAdjacentHTML('afterbegin','<fieldset class="class-choice-list" id="new-task-classes"><legend>布置班级</legend><span class="muted">正在读取班级…</span></fieldset>');
    try{const data=await api('/api/classes');if(version!==state.routeVersion)return;const classes=data.classes.filter(c=>!c.archived);$('#new-task-classes').innerHTML='<legend>布置班级（至少选择一个）</legend>'+classes.map(c=>`<label class="inline-check"><input name="workflow_classes" type="checkbox" value="${c.id}">${escapeHTML(c.name)}</label>`).join('')+(classes.length?'':'<p>请先<a href="/classes" data-nav>建立班级并录入名单</a>。</p>');}
    catch(error){if(version===state.routeVersion)$('#new-task-classes').innerHTML=`<legend>班级读取失败</legend><p role="alert">${escapeHTML(error.message)}</p><button class="btn" type="button" data-workbench-classes-retry>重新读取</button>`;}
  }
  document.addEventListener('input',e=>{const el=e.target;if(el.dataset.homeParam&&el.type==='search'&&!e.isComposing){const p=new URLSearchParams(location.search);el.value?p.set(el.dataset.homeParam,el.value):p.delete(el.dataset.homeParam);Nav.patchURL(p);table();}});
  document.addEventListener('compositionend',e=>{if(e.target.dataset.homeParam)e.target.dispatchEvent(new Event('input',{bubbles:true}));});
  document.addEventListener('change',e=>{const el=e.target;if(!el.dataset.homeParam||el.type==='search')return;const p=new URLSearchParams(location.search),value=el.type==='checkbox'?(el.checked?'1':''):el.value;value?p.set(el.dataset.homeParam,value):p.delete(el.dataset.homeParam);Nav.patchURL(p);table();});
  document.addEventListener('click',e=>{
    const b=e.target.closest('button');
    if(!b)return;
    if(b.hasAttribute('data-assignment-return')){Nav.backFromAssignment();return;}
    if(b.hasAttribute('data-workbench-upload')){openUpload();return;}
    if(b.dataset.workbenchMetric){
      const selector=`[data-todo-kind="${b.dataset.workbenchMetric}"] a`;
      if(!$(selector)){
        const p=new URLSearchParams(location.search);p.set('todos','all');Nav.patchURL(p);todoList();
      }
      $(selector)?.focus();
      return;
    }
    if(b.hasAttribute('data-workbench-archive')){toggleArchive(b);return;}
    if(b.hasAttribute('data-workbench-todos')){
      const p=new URLSearchParams(location.search);p.has('todos')?p.delete('todos'):p.set('todos','all');
      Nav.patchURL(p);todoList();$('#workbench-toggle-todos')?.focus();
    }else if(b.hasAttribute('data-workbench-retry'))render();
    else if(b.hasAttribute('data-workbench-classes-retry')){$('#new-task-classes')?.remove();mountCreation();}
    else if(b.hasAttribute('data-workbench-reset'))navigate(location.pathname,true);
    else if(b.hasAttribute('data-workbench-refresh')){rows=latest||rows;order=sorted(rows).map(r=>r.id);table();$('#workbench-update').hidden=true;}
  });
  return {render,mountCreation,legacyList};
})();


