// Node stdlib only: reproduce active -> done without a real browser, database or API.
const assert=require('node:assert/strict'), fs=require('node:fs'), vm=require('node:vm');
const reviewContext=vm.createContext({state:{routeVersion:1,busy:false,draw:false,submission:{visual_suggestions:{}}},
  attentionContext:null,isGrading:()=>false,escapeHTML:String,numberText:String});
const appSource=fs.readFileSync('static/app.js','utf8');
vm.runInContext(appSource.slice(appSource.indexOf('let visualFrame ='),appSource.indexOf('function renderVisualReview()')),reviewContext);
const reviewHTML=vm.runInContext(`visualReviewHTML({question_id:2,uncertain:true,kind:'word'})`,reviewContext);
assert.match(reviewHTML,/aria-label="AI 核对建议"/);
assert.match(reviewHTML,/<details class="visual-retry"[ ]*>/,'retry tools start collapsed');
vm.runInContext(`state.submission.visual_suggestions[2]={status:'readable',transcript:'book',suggested_score:1,max_score:1,can_adopt:true,preview_url:'/photo'};`,reviewContext);
const suggestionHTML=vm.runInContext(`visualReviewHTML({question_id:2,uncertain:true,kind:'word'})`,reviewContext);
assert.match(suggestionHTML,/采纳 AI 建议/);
assert.doesNotMatch(suggestionHTML,/<img/,'do not duplicate the answer image');
vm.runInContext(`state.submission.visual_suggestions[2].error_code='provider';`,reviewContext);
assert.doesNotMatch(vm.runInContext(`visualReviewHTML({question_id:2,uncertain:true,kind:'word'})`,reviewContext),/data-action="visual-adopt"/);
const elements=new Map();
for(const id of ['attention-results','attention-search','attention-kind','attention-order','attention-count','ai-review-remaining','identity-review-remaining','batch-job-status'])
  elements.set('#'+id,{value:'',disabled:false,textContent:'',innerHTML:'',insertAdjacentHTML(){}});
const context=vm.createContext({console,URLSearchParams,URL,setTimeout(){return 1;},clearTimeout(){},
  document:{addEventListener(){},activeElement:null,hidden:false},
  location:{pathname:'/assignments/9',search:'?tab=attention'},
  state:{routeVersion:1,busy:false},$:key=>elements.get(key),$$:()=>[],
  escapeHTML:String,numberText:String,updateVisualNumber(){},recognitionFor(){return {};},
  api:async path=>path.endsWith('/attention')?{summary:{attention_count:0,uncertain_questions:0},items:[]}:
    {job:{id:1,state:'done',stage:'ai_review',total:40,completed:40,message:'未调用 AI：裁图过大。',errors:[]}}
});
vm.runInContext(fs.readFileSync('static/classes.js','utf8'),context);
vm.runInContext(fs.readFileSync('static/workspace.js','utf8'),context);
vm.runInContext(`
  loadBatchPhotos=async()=>{};
  hasListInteraction=()=>false;
  classUI.data={id:9,students:[]};classUI.job={id:1,state:'running',stage:'ai_review'};
`,context);
(async()=>{
  await vm.runInContext('loadAssignmentWorkbench(9)',context);
  assert.equal(elements.get('#ai-review-remaining').disabled,false,'finished job must release button');
  assert.match(elements.get('#batch-job-status').innerHTML,/未调用 AI/,'attention tab must show outcome');
  vm.runInContext(`classUI.job={id:1,state:'running',stage:'ai_review'};Workspace.attention({summary:{attention_count:0,uncertain_questions:0},items:[]})`,context);
  assert.equal(elements.get('#ai-review-remaining').disabled,true);
  assert.match(elements.get('#attention-results').innerHTML,/正在后台/,'processing is not a clean bill of health');
  assert.doesNotMatch(elements.get('#attention-results').innerHTML,/未发现识别异常/);
  console.log('PASS: finished button, visible outcome, processing empty state');
})().catch(error=>{console.error(error);process.exitCode=1;});
