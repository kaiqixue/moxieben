'use strict';

const Analysis=(()=>{
  let report=null,selections={},expanded={};
  const actual=i=>({blank:'教师已确认未作答',teacher_verdict:'教师原图判错，未补录作答文字',missing:'未取得作答文字（不代表未作答）'})[i.answer_status]??i.transcript??i.text??'';
  const fingerprint=g=>({answer_status:g.answer_status,text:g.text});
  const same=(a,b)=>a?.answer_status===b?.answer_status&&a?.text===b?.text;
  function capture(){return location.pathname.endsWith('/analysis')?{selections,expanded}:null;}
  function restore(saved){
    if(!report||!saved)return;selections={...saved.selections};expanded={...saved.expanded};let missing=false;
    for(const q of report.questions){if(selections[q.id]&&!q.common_answers.some(g=>same(fingerprint(g),selections[q.id]))){delete selections[q.id];missing=true;}apply(q.id);}
    if(missing)toast('原作答分组已更新，已回到该题全部答错学生。');
  }
  function apply(id){
    const q=report.questions.find(q=>q.id===Number(id)),card=$(`[data-analysis-question="${id}"]`);if(!q||!card)return;
    const group=q.common_answers.find(g=>same(fingerprint(g),selections[id])),selected=group?String(group.id):'all';
    $$('[data-answer-filter]',card).forEach(b=>{b.setAttribute('aria-pressed',String(b.dataset.answerFilter===selected));b.hidden=b.classList.contains('extra-answer')&&!expanded[id];});
    const more=$('[data-answer-more]',card);if(more){more.setAttribute('aria-expanded',String(!!expanded[id]));more.textContent=expanded[id]?'收起低频答案':`另有 ${q.common_answers.length-5} 种 · 展开`;}
    $$('.student-links a[data-answer-group]',card).forEach(a=>a.hidden=selected!=='all'&&a.dataset.answerGroup!==selected);
    const count=$('[data-answer-count]',card);if(count)count.textContent=group?`提交“${actual(group)}”的学生：${group.count} 人`:`全部答错学生：${q.wrong_count} 人`;
  }
  function studentLink(student,q,group){return `<a data-nav ${group!=null?`data-answer-group="${group}"`:''} href="/submissions/${student.submission_id}?question=${q.id}${student.uncertain?'&filter=uncertain':''}">${escapeHTML(student.student_code)} ${escapeHTML(student.student_name||student.name)}</a>`;}
  function questions(){return report.questions.map(q=>`<section class="card analysis-question" data-analysis-question="${q.id}"><div class="card-header"><h2>第 ${q.number} 题 · ${kindNames[q.kind]||'题目'}</h2><span>${q.valid_count?`${q.wrong_count} / ${q.valid_count} 人答错 · ${q.wrong_rate}%`:'暂无有效计分样本'}</span></div><div class="card-content"><p>参考答案：<strong>${escapeHTML(q.answer)}</strong>${q.variants?.length?` <span class="muted">（可接受：${q.variants.map(escapeHTML).join(' / ')}）</span>`:''}</p>${contextHTML(q)}${q.common_answers.length?`<div class="answer-filters" role="group" aria-label="第 ${q.number} 题常见作答"><span>常见作答：</span><button class="common-answer" data-answer-filter="all" aria-pressed="true">全部 · ${q.wrong_count} 人</button>${q.common_answers.map((g,i)=>`<button class="common-answer ${i>4?'extra-answer':''}" data-answer-filter="${g.id}" aria-pressed="false" ${i>4?'hidden':''}>${escapeHTML(actual(g))} · ${g.count} 人</button>`).join('')}${q.common_answers.length>5?'<button class="btn small" data-answer-more aria-expanded="false">展开低频答案</button>':''}</div><p data-answer-count role="status" aria-live="polite">全部答错学生：${q.wrong_count} 人</p><div class="student-links">${q.mistakes.map(m=>studentLink(m,q,m.answer_group)).join('')}</div>`:'<p class="muted">本题未发现明确错题。</p>'}${q.pending_count?`<div class="notice warn">另有 ${q.pending_count} 人本题待核对，不计入明确错题率。<div class="student-links">${report.students.filter(s=>(s.pending||[]).some(p=>p.question_id===q.id)).map(s=>studentLink({...s,uncertain:true},q)).join('')}</div></div>`:''}</div></section>`).join('');}
  function students(selected){
    const chosen=selected?report.students.filter(s=>String(s.id)===selected):report.students.filter(s=>s.errors?.length||s.pending?.length);
    return `<div class="card card-content analysis-picker"><label class="field">订正学生<select id="analysis-student"><option value="">全部有错题或待核对的学生</option>${report.students.map(s=>`<option value="${s.id}" ${String(s.id)===selected?'selected':''}>${escapeHTML(s.student_code)} ${escapeHTML(s.name)}</option>`).join('')}</select></label></div>`+chosen.map(s=>`<section class="card student-sheet"><div class="card-header"><h2>${escapeHTML(s.student_code)} ${escapeHTML(s.name)}</h2></div><div class="card-content">${[...(s.errors||[]),...(s.pending||[])].map(i=>`<article class="correction-question"><h3>第 ${i.number} 题</h3><p>原作答：${escapeHTML(actual(i))}</p><p>参考答案：${escapeHTML(i.answer)}</p><p>${studentLink(s,{id:i.question_id})}</p><div class="correction-line" aria-label="订正书写区"></div></article>`).join('')||'<p>该学生暂无明确错题或待核对题。</p>'}</div></section>`).join('');
  }
  async function render(id){
    const version=state.routeVersion;const data=await api(`/api/assignments/${id}/analysis`);if(version!==state.routeVersion)return;
    report=data;selections={};expanded={};const p=new URLSearchParams(location.search),view=p.get('view')==='students'?'students':'class',selected=p.get('student')||'',counts=data.counts;
    $('#main').innerHTML=Workspace.header(data.assignment,'analysis')+`<div class="analysis-page"><div class="analysis-tools"><div class="button-row"><a class="btn ${view==='class'?'primary':''}" ${view==='class'?'aria-current="page"':''} data-nav href="${escapeHTML(Workspace.analysisHref(id))}">全班讲评</a><a class="btn ${view==='students'?'primary':''}" ${view==='students'?'aria-current="page"':''} data-nav href="${escapeHTML(Workspace.analysisHref(id,'students'))}">按学生订正</a></div><div class="button-row"><button class="btn" data-analysis-print>打印${view==='students'?'订正单':'讲评报告'}</button><a class="btn" id="analysis-download" href="/assignments/${id}/analysis/download?view=${view}${selected?'&student='+encodeURIComponent(selected):''}">下载${view==='students'?'订正单':'讲评报告'}</a></div></div><div class="card card-content summary-strip"><span>已计分 <strong>${counts.final} / ${counts.expected}</strong> 人</span><span>明确错题 <strong>${counts.wrong_answers}</strong> 人次</span><span>待核对 <strong>${counts.pending_questions}</strong> 题</span></div><p class="muted">不确定题不混入明确错题；点击常见作答可查看提交该答案的学生。</p>${view==='class'?'<h2>优先讲评</h2>'+questions():students(selected)}</div>`;
  }
  document.addEventListener('click',e=>{
    const b=e.target.closest('[data-answer-filter],[data-answer-more],[data-analysis-print]');if(!b||!location.pathname.endsWith('/analysis'))return;
    if(b.hasAttribute('data-analysis-print'))return print();
    const id=Number(b.closest('[data-analysis-question]').dataset.analysisQuestion),q=report.questions.find(q=>q.id===id);
    if(b.hasAttribute('data-answer-more')){expanded[id]=!expanded[id];if(!expanded[id]&&q.common_answers.findIndex(g=>same(fingerprint(g),selections[id]))>=5)delete selections[id];}
    else{const g=q.common_answers.find(g=>String(g.id)===b.dataset.answerFilter);if(g)selections[id]=fingerprint(g);else delete selections[id];}
    apply(id);Nav.capture();
  });
  document.addEventListener('change',e=>{if(e.target.id==='analysis-student'){const p=new URLSearchParams(location.search);e.target.value?p.set('student',e.target.value):p.delete('student');navigate(location.pathname+'?'+p);}});
  return {render,capture,restore};
})();
