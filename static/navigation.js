'use strict';

// Each browser history entry owns its URL and transient view. No business data lives here.
const Nav = (() => {
  let current, restoring=false, replay=false, leaving=null, scheduled,intent=0;
  const key=()=>globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
  const validEntry=e=>e?.v===1&&typeof e.id==='string'&&Number.isSafeInteger(e.index)&&typeof e.path==='string';
  function safePath(value) {
    if(typeof value!=='string'||!value.startsWith('/')||value.startsWith('//'))return null;
    const u=new URL(value,location.origin);
    if(u.origin!==location.origin||!/^\/(?:$|tasks(?:\/\d+|\/new)?\/?$|classes(?:\/\d+)?\/?$|students\/\d+\/?$|assignments(?:\/\d+(?:\/analysis)?)?\/?$|submissions\/\d+\/?$|settings\/?$)/.test(u.pathname))return null;
    if(u.username||u.password||/^\/submissions\//.test(u.pathname))return null;
    u.searchParams.delete('from');return u.pathname+u.search+u.hash;
  }
  function store(entry) {
    current=entry;
    try{history.replaceState(entry,'',entry.path)}catch{}
    try{sessionStorage.setItem('navigation:last',JSON.stringify({id:entry.id,index:entry.index,path:entry.path}));sessionStorage.setItem('navigation:'+entry.id,JSON.stringify(entry))}catch{}
  }
  function snapshot() {
    const active=document.activeElement;
    let focus=active?.id?`#${CSS.escape(active.id)}`:null;
    if(!focus&&active?.matches('a,button')){
      const row=active.closest('[data-recognition-id],[data-attention-item],[data-analysis-question]');
      if(row){const attr=[...row.attributes].find(a=>a.name.startsWith('data-')),parent=`[${attr.name}="${CSS.escape(attr.value)}"]`;focus=active.matches('a')?`${parent} a[href="${CSS.escape(active.getAttribute('href'))}"]`:active.dataset.answerFilter?`${parent} [data-answer-filter="${CSS.escape(active.dataset.answerFilter)}"]`:`${parent} button`;}
    }
    return {top:scrollY,focus,anchor:active?.closest('[data-recognition-id]')?.dataset.recognitionId,
      analysis:typeof Analysis!=='undefined'?Analysis.capture():null};
  }
  function capture(){if(current&&current.path===location.pathname+location.search+location.hash)store({...current,view:snapshot()});}
  function patchURL(params) {
    const path=location.pathname+(params.size?'?'+params:'')+location.hash;
    store({...current,path});lastPath=path;
  }
  function relocate(path) {
    const target=safePath(path);
    if(target){store({...current,path:target});lastPath=target;}
  }
  function restore(view) {
    if(view?.analysis&&typeof Analysis!=='undefined')Analysis.restore(view.analysis);
    requestAnimationFrame(()=>{
      let focus;try{focus=view?.focus&&document.querySelector(view.focus)}catch{}
      if(focus)focus.focus({preventScroll:true});
      else if(view?.anchor){
        const rows=$$('[data-recognition-id]');const nearby=rows.find(el=>Number(el.dataset.recognitionId)>=Number(view.anchor))||rows.at(-1);
        $('a,button',nearby||document)?.focus({preventScroll:true});if(nearby?.dataset.recognitionId!==view.anchor)toast('原位置已变化，已定位到附近。');
      }
      else $('#main')?.focus({preventScroll:true});
      scrollTo({top:view?.top||0,behavior:'instant'});
    });
  }
  function pendingFiles(){return [...document.querySelectorAll('input[type=file]')].some(el=>el.files?.length)||(typeof Workspace!=='undefined'&&Workspace.hasFiles());}
  async function mayLeave() {
    if(state.busy){toast('正在上传或保存，请稍候再离开。',true);return false;}
    if(!hasDirty()&&!pendingFiles())return true;
    if(leaving)return leaving;
    const focus=document.activeElement,canSave=state.drafts.size>0&&!state.newTaskDirty&&!pendingFiles();
    const d=document.createElement('dialog');d.id='leave-dialog';d.setAttribute('aria-labelledby','leave-title');
    d.innerHTML=`<h2 id="leave-title">${canSave?'答案修改尚未保存':'还有未提交的内容'}</h2><p>${canSave?'保存后会更新成绩和修改记录。':'离开会丢失当前填写或选择的文件，已上传照片不受影响。'}</p><p class="notice error" data-leave-error hidden role="alert"></p><div class="button-row">${canSave?'<button class="btn primary" data-leave="save">保存并离开</button>':''}<button class="btn danger" data-leave="discard">${canSave?'放弃修改':'放弃离开'}</button><button class="btn" data-leave="stay">${canSave?'继续编辑':'继续填写'}</button></div>`;
    document.body.append(d);d.showModal();d.querySelector('[data-leave=stay]').focus();
    leaving=new Promise(resolve=>{
      const finish=ok=>{d.close();d.remove();leaving=null;if(!ok&&focus?.isConnected)focus.focus({preventScroll:true});resolve(ok)};
      d.addEventListener('cancel',e=>{e.preventDefault();if(!state.busy)finish(false)});
      d.addEventListener('click',async e=>{
        const action=e.target.closest('[data-leave]')?.dataset.leave;if(!action||state.busy)return;
        if(action==='stay')return finish(false);
        if(action==='discard')return finish(true);
        [...d.querySelectorAll('button')].forEach(b=>b.disabled=true);state.busy=true;
        try{
          await saveAllReviews();
          if(state.drafts.size||state.reviewSave?.error)throw new Error(state.reviewSave?.error||'还有未保存修改。');
          finish(true);
        }catch(error){const message=d.querySelector('[data-leave-error]');message.hidden=false;message.textContent=error.message;}
        finally{state.busy=false;updateReviewLock();if(d.isConnected)[...d.querySelectorAll('button')].forEach(b=>b.disabled=false);}
      });
    });return leaving;
  }
  function fallback() {const s=state.submission;return s?.identity?.assignment_id?`/assignments/${s.identity.assignment_id}?tab=grades`:s?`/tasks/${s.task_id}`:'/';}
  function returnInfo() {
    const saved=current?.returnContract?.v===1?current.returnContract:null;
    const legacy=safePath(new URLSearchParams(location.search).get('from'));
    const path=safePath(saved?.path)||legacy||fallback();
    const kind=path.split('?')[0]==='/'?'home':path.split('?')[0]==='/assignments'?'assignments':path.includes('/analysis')?'analysis':path.includes('tab=attention')||/[?&]view=(attention|uncertain)/.test(path)?'attention':path.startsWith('/assignments/')?'grades':path.startsWith('/students/')?'student_history':'task';
    return {...saved,path,kind,label:({home:'返回工作台',assignments:'返回作业管理',analysis:'返回讲评',attention:'返回待核对列表',grades:'返回成绩名单',student_history:'返回历史作业',task:'返回任务'})[kind]};
  }
  async function render(entry,view) {
    store(entry);await route();if(current.id===entry.id&&current.path===entry.path)restore(view||entry.view);
  }
  async function go(value,replace=false,options={}) {
    const request=++intent;
    const target=new URL(value,location.origin);
    if(target.origin!==location.origin)return;
    if(!options.approved&&!await mayLeave())return false;
    if(request!==intent)return false;
    capture();
    const source=current;let contract=options.returnContract;
    if(/^\/submissions\/\d+/.test(target.pathname)){
      contract=contract||(replace&&source?.returnContract)||{v:1,id:source.id,index:source.index,path:safePath(source.path)||'/',view:source.view};
    }
    const targetAssignment=target.pathname.match(/^\/assignments\/(\d+)/)?.[1];
    const sourceAssignment=location.pathname.match(/^\/assignments\/(\d+)/)?.[1];
    let assignmentSource;
    if(targetAssignment){
      if(targetAssignment===sourceAssignment)assignmentSource=source.assignmentSource;
      else if(location.pathname==='/'||location.pathname==='/assignments'||/^\/classes\/\d+/.test(location.pathname)){
        assignmentSource={id:source.id,index:source.index,path:safePath(source.path),view:source.view};
      }
    }
    const entry={v:1,id:replace?source.id:key(),index:source.index+(replace?0:1),previous:replace?source.previous:source.id,path:target.pathname+target.search+target.hash,returnContract:contract,assignmentSource,view:options.view};
    state.drafts.clear();state.newTaskDirty=false;state.submission=null;
    if(typeof Workspace!=='undefined')Workspace.clearUpload();
    $('#class-dialog')?.remove();
    try{history[replace?'replaceState':'pushState'](entry,'',entry.path)}catch{location.assign(entry.path);return true;}
    await render(entry);return true;
  }
  async function back(options={}) {
    const request=++intent;
    if(!options.approved&&!await mayLeave())return;
    if(request!==intent)return;
    const info=returnInfo();
    const source=info.path.match(/^\/(assignments|tasks|students|classes)\/(\d+)/);
    if(source){
      try{await api(`/api/${source[1]}/${source[2]}`);}
      catch(error){
        if(error.status!==404){toast('返回来源暂时无法读取，请稍后重试；当前内容保留。',true);return;}
        let target=fallback();if(target.split('?')[0]===info.path.split('?')[0])target='/';
        if(target!=='/')try{await api('/api'+target.split('?')[0]);}catch(e){if(e.status!==404){toast('返回来源暂时无法读取，请重试。',true);return;}target='/';}
        await go(target,true,{approved:true});toast('原列表已不存在，已返回可用的作业入口。');return;
      }
    }
    if(request!==intent)return;
    if(info.id===current.previous&&info.index===current.index-1){state.drafts.clear();state.newTaskDirty=false;if(typeof Workspace!=='undefined')Workspace.clearUpload();capture();history.back();}
    else await go(info.path,true,{approved:true,view:info.view});
  }
  function assignmentReturn() {
    const source=current?.assignmentSource;
    return safePath(source?.path)?source:{path:'/assignments'};
  }
  async function backFromAssignment(){
    if(!await mayLeave())return;
    const source=assignmentReturn();
    if(source.id===current.previous&&source.index===current.index-1)history.back();
    else await go(source.path,true,{approved:true,view:source.view});
  }
  function init() {
    let entry=validEntry(history.state)?history.state:null,last;
    const path=location.pathname+location.search+location.hash;
    try{last=JSON.parse(sessionStorage.getItem('navigation:last')||'null');if(!entry&&last?.path===path){const cached=JSON.parse(sessionStorage.getItem('navigation:'+last.id)||'null');if(validEntry(cached))entry=cached;}}catch{}
    store(entry?{...entry,path}:{v:1,id:key(),index:(last?.index??-1)+1,path});
    history.scrollRestoration='manual';
    document.addEventListener('scroll',()=>{clearTimeout(scheduled);scheduled=setTimeout(capture,150)},{passive:true});
    window.addEventListener('pagehide',capture);
    window.addEventListener('popstate',async e=>{
      const target=e.state;
      if(restoring){restoring=false;return;}
      if(!validEntry(target)){location.reload();return;}
      const previous=current,delta=target.index-previous.index;
      if(!replay&&(hasDirty()||pendingFiles()||state.busy)){
        restoring=true;history.go(-delta);
        await new Promise(resolve=>{const check=()=>restoring?setTimeout(check,10):resolve();check()});
        if(await mayLeave()){state.drafts.clear();state.newTaskDirty=false;replay=true;history.go(delta);}return;
      }
      replay=false;state.drafts.clear();state.newTaskDirty=false;state.submission=null;
      if(typeof Workspace!=='undefined')Workspace.clearUpload();
      $('#class-dialog')?.remove();await render({...target,path:location.pathname+location.search+location.hash});
    });
  }
  return {init,go,back,patchURL,relocate,capture,restore,safePath,returnInfo,assignmentReturn,backFromAssignment,mayLeave,snapshot,pendingFiles,get current(){return current}};
})();

/* Only show the route-focus ring after real keyboard input; programmatic focus on
   #main must not leave a permanent blue frame around the page heading. */
(()=>{
  const set=mode=>document.body.classList.toggle('using-keyboard',mode==='keyboard');
  document.addEventListener('keydown',event=>{
    if(event.key==='Tab'||event.key==='Enter'||event.key.startsWith('Arrow')||event.key===' ')set('keyboard');
  },{capture:true});
  document.addEventListener('pointerdown',()=>set('pointer'),{capture:true});
})();
