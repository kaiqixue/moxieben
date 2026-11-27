const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const listeners={},calls=[];
const q={question_id:1,number:1,kind:'essay',points:1,context:'题目',answer:'',variants:['another version'],writing_config:{},transcript:'I like it.',confirmed:false,confirmed_blank:false,word_count:3,revision:'rev1',ai_state:'completed',ai_result:{event_id:7,score:.8,components:{content:.4,language:.4},errors:[{quote:'<img>',start:0,end:5,type:'grammar',explanation:'<script>',suggestion:'fix'}],feedback:'feedback'}};
const sub={id:2,reviews:[q],identity:null};
const context=vm.createContext({document:{addEventListener(name,fn){(listeners[name]??=[]).push(fn);}},state:{submission:sub,activeQuestion:1,routeVersion:1,busy:false},
  api:async(path,options)=>{calls.push([path,options]);return path.endsWith('/writing')?{questions:[q]}:path.endsWith('/submissions/2')?sub:{submission:sub,writing:{questions:[q]}};},
  escapeHTML:s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),numberText:n=>String(n),badge:s=>s,contextHTML:()=>'',kindNames:{essay:'作文'},isGrading:()=>false,toast(){},renderSubmission(){},renderReviews(){assert.equal(context.state.busy,false);},selectReviewQuestion(){},
  busyAction:async(button,action)=>{if(context.state.busy)return;context.state.busy=true;try{await action();}finally{context.state.busy=false;}},$:()=>null});
vm.runInContext(fs.readFileSync('static/writing.js','utf8'),context);
const app=fs.readFileSync('static/app.js','utf8');
vm.runInContext(app.slice(app.indexOf('function answerRow('),app.indexOf('function contextHTML(')),context);
vm.runInContext("const Translation={creationFields:()=>''};",context);
(async()=>{
  for(const points of [undefined,null,'']){context.testQuestion={kind:'essay',points};const html=vm.runInContext("answerRow(1,'',testQuestion)",context);assert.match(html,/class="points-input"[^>]*value="1"/);assert(!/placeholder="参考答案[^>]* required/.test(html));}
  context.testQuestion={kind:'essay',points:25,variants:['first','second']};const html=vm.runInContext("answerRow(1,'',testQuestion)",context);assert.match(html,/value="25"/);assert.match(html,/first \| second/);
  await vm.runInContext('Writing.loadSubmission(state.submission)',context);
  const review=vm.runInContext('Writing.reviewHTML(state.submission.reviews[0])',context);assert.match(review,/待核对/);assert.match(review,/教师确认后/);assert.match(review,/&lt;script&gt;/);assert(!review.includes('<script>'));
  assert.equal(vm.runInContext("Writing.highlighted('a b a',[{start:4,end:5,quote:'a'}])",context),'a b <mark>a</mark>');
  const button={dataset:{writingAction:'grade',id:'1'},closest(){return this;}};
  const action=listeners.click[0];await Promise.all([action({target:button}),action({target:button})]);
  const posts=calls.filter(([,options])=>options?.method==='POST');assert.equal(posts.length,1);assert.equal(posts[0][1].body.expected_revision,'rev1');
  const field={dataset:{writingField:'transcript'},value:'changed',type:'textarea',closest:()=>({dataset:{questionId:'1'}})};
  listeners.input[0]({target:field});assert.equal(vm.runInContext('Writing.hasDirty()',context),true);
  listeners.input[0]({target:{...field,dataset:{writingField:'reason'},value:'retain teacher reason'}});
  context.newRegion={page_id:3,box:[0,0,10,10],block_ids:[2]};
  await vm.runInContext("Writing.associate(1,'more literal text',newRegion)",context);
  const linked=calls.filter(([path])=>path.endsWith('/writing-confirm')).at(-1)[1].body;
  assert.equal(linked.transcript,'changed\nmore literal text');assert.equal(linked.regions[0].page_id,3);
  assert.equal(vm.runInContext('Writing.hasDirty()',context),true,'teacher score/reason draft remains after association');
  assert.match(vm.runInContext('Writing.reviewHTML(state.submission.reviews[0])',context),/retain teacher reason/);
  // Answer-only import restarts paper numbering per group; system numbers must stay unique.
  const groups=[{kind:'phrase',items:[{number:1,answer:'first'},{number:2,answer:'second'}]},
    {kind:'sentence',items:[{number:1,answer:'A complete sentence.'}]},
    {kind:'summary_cloze',items:[{number:1,versions:[{answer:'reference one'},{answer:'reference two'}],
      context:'Fill the summary.',writing_config:{source_passage:'Original article.',word_max:60}}]}];
  const preview={_answerDraft:{groups,name:'Imported paper',source_name:'paper.docx'}},editor={innerHTML:''};
  const nodes={'#answer-word-preview':preview,'#answer-rows':editor,'#new-task-form':{elements:{task_name:{value:''}}},
    '#word-source-name':{},'#task-passage':{},'#task-instructions':{},'#word-confirmation':{},'#word-confirmed':{}};
  const choices=groups.flatMap((group,g)=>group.items.map((item,i)=>{
    const fieldset={section:{value:['A','B','Summary'][g]}};
    const element={dataset:{group:String(g),item:String(i)},versions:(item.versions||[{answer:item.answer}]).map(v=>({value:v.answer})),closest:()=>fieldset};
    return {closest:()=>element};
  }));
  const importContext=vm.createContext({state:{},escapeHTML:context.escapeHTML,kindNames:{phrase:'短语',sentence:'句子',summary_cloze:'概要填空'},
    Writing:{creationFields:()=>'',isWriting:kind=>kind==='summary_cloze'},Translation:{creationFields:()=>''},
    $:(selector,parent)=>selector==='[data-answer-word-section]'?parent.section:nodes[selector],
    $$:(selector,parent)=>selector==='[data-answer-word-choice]:checked'?choices:selector==='textarea[data-answer-version]'?parent.versions:[],
    updateEditorTotal(){},toast(){},confirm:()=>true});
  vm.runInContext(app.slice(app.indexOf('function answerRow('),app.indexOf('function contextHTML(')),importContext);
  vm.runInContext(app.slice(app.indexOf('function applyAnswerWord('),app.indexOf('function renderNewTask(')),importContext);
  vm.runInContext('applyAnswerWord()',importContext);
  assert.deepEqual([...editor.innerHTML.matchAll(/class="number-input"[^>]*value="(\d+)"/g)].map(m=>Number(m[1])),[1,2,3,4]);
  assert.deepEqual([...editor.innerHTML.matchAll(/class="paper-number-input"[^>]*value="(\d+)"/g)].map(m=>m[1]),['1','2','1','1']);
  assert.match(editor.innerHTML,/reference two/);assert.equal(nodes['#word-confirmed'].checked,false);
  console.log('PASS: writing defaults/references, escaped feedback, exact annotation offsets, revision and double-click guard, dirty drafts');
})().catch(error=>{console.error(error);process.exitCode=1;});
