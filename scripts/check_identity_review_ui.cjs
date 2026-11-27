// Synthetic fixture only: no database writes, student images, or cloud requests.
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {execFileSync}=require('node:child_process');
(async()=>{
 const root=path.resolve(__dirname,'..'),browser=await chromium.launch({channel:'msedge',headless:true});
 const html=execFileSync(path.join(root,'.venv','Scripts','python.exe'),['-c',"from flask import Flask,render_template; a=Flask(__name__,template_folder='templates'); c=a.test_request_context(); c.push(); print(render_template('index.html',csrf_token='fixture',tenant_mode=False,settings_available=True,teacher_login_enabled=False))"],{cwd:root,encoding:'utf8',env:{...process.env,PYTHONIOENCODING:'utf-8'}});
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[],writes=[];
 let fail=false,unknown=false,release,hold=false;
 const students=[{id:1,student_id:1,name:'张同学',student_code:'001'}];
 const photos=[1,2].map(id=>({id,url:'/photo.svg',status:'identified',submission_id:null,evidence_loaded:true,identity_names:['张同字'],identified_text:'姓名：张同字',match_details:{method:'pending',reason:'姓名与名单相差一字，尚不能确定归属',identity_band:{code:'001'},candidates:[{id:1,name:'张同学',student_code:'001'}]}}));
 const pending=()=>photos.filter(p=>!p.submission_id);
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/*',async route=>{
  const req=route.request(),url=new URL(req.url()),p=url.pathname;let data;
  if(url.hostname!=='identity.test')return route.abort();
  if(req.method()==='POST')writes.push({path:p,body:req.postDataJSON()});
  if(p==='/api/assignments/1')data={id:1,class_id:1,task_id:1,class_name:'虚构验收班',task_name:'身份复核验收',students,counts:{expected:1,submitted:0},total_points:1};
  else if(p==='/api/assignments/1/photos')data={photos,summary:{pending:pending().length,matched:0}};
  else if(p==='/api/assignments/1/attention')data={summary:{identity_photos:pending().length,attention_count:pending().length,failed_submissions:0,ungraded_submissions:0,uncertain_questions:0},items:pending().map(p=>({id:`identity-${p.id}`,kind:'identity',photo_id:p.id,reason:p.match_details.reason}))};
  else if(p==='/api/assignments/1/grading-jobs')data={job:null};
  else if(p==='/api/assignments/1/identity-review-jobs')data={job:{id:44,state:'queued',total:2}};
  else if(/^\/api\/batch-photos\/\d+\/identity-review(?:\/retry)?$/.test(p)){
   if(hold)await new Promise(resolve=>release=resolve);
   if(unknown){unknown=false;return route.abort();}
   if(fail)return route.fulfill({status:503,json:{error:'模拟模型不可用，请重试'}});
   const photo=photos.find(x=>x.id===Number(p.split('/')[3]));
   photo.identity_review={status:'completed',read_name:'张同学',read_code:'001',read_class:'虚构验收班',confidence:'high',model:'fixture-vision',crop:{kind:'full_page'},elapsed_ms:1200,reason:'重读完成，仍请教师核对'};data={photo};
  }else if(/^\/api\/batch-photos\/\d+\/confirm$/.test(p)){
   const photo=photos.find(x=>x.id===Number(p.split('/')[3]));photo.submission_id=21;data={photo,submission_id:21};
  }else if(/^\/api\/batch-photos\/\d+$/.test(p))data=photos.find(x=>x.id===Number(p.split('/').pop()));
  else if(p==='/api/classes')data={classes:[]};
  else if(p.startsWith('/api/'))return route.fulfill({status:404,json:{error:'Unexpected fixture endpoint '+p}});
  else if(p==='/photo.svg')return route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="700" height="600"><rect width="700" height="600" fill="#fff"/><text x="30" y="60" font-size="30">姓名：张同学 学号：001（虚构）</text></svg>'});
  else if(p.startsWith('/static/'))return route.fulfill({path:path.join(root,p),contentType:p.endsWith('.js')?'text/javascript':'text/css'});
  else return route.fulfill({contentType:'text/html',body:html});
  return route.fulfill({json:data});
 });
 const card=id=>page.locator(`#attention-results [data-identity-evidence="${id}"]`);
 const settled=()=>page.waitForFunction(()=>!state.busy);
 try {
  await page.goto('http://identity.test/assignments/1?tab=attention');await card(1).waitFor();
  assert.match(await card(1).innerText(),/原始 OCR.*姓名：张同字/s);assert.match(await card(1).innerText(),/学号：001/);
  assert.equal(await card(1).locator('img').count(),1);assert.equal(writes.length,0,'GET never invokes paid AI');
  hold=true;await card(1).locator('[data-identity-review]').click();await page.waitForFunction(()=>state.busy);
  assert.equal(await card(1).locator('[data-identity-review]').isDisabled(),true);hold=false;release();await settled();
  assert.match(await card(1).innerText(),/AI 重读.*张同学/s);assert.match(await card(1).innerText(),/整页复核/);
  fail=true;await card(2).locator('[data-identity-review]').click();await settled();assert.match(await card(2).innerText(),/模拟模型不可用/);
  assert.match(await card(2).locator('[data-identity-review]').innerText(),/重新/);
  fail=false;unknown=true;await card(2).locator('[data-identity-review]').click();await settled();
  assert.match(await card(2).innerText(),/响应未确认/);assert(writes.some(w=>w.path.endsWith('/identity-review/retry')),'retry is explicit');const before=writes.length;
  await card(2).locator('[data-identity-review]').click();await settled();assert.equal(writes.length,before,'unknown response reconciles by GET');
  await page.locator('#attention-search').fill('2');await page.evaluate(()=>renderAttention(classUI.attention));
  assert.equal(await page.locator('#attention-search').inputValue(),'2');assert.equal(await page.locator('#attention-search').evaluate(el=>el===document.activeElement),true);
  await page.locator('#attention-search').fill('');await card(1).locator('[data-identity-confirm]').click();await settled();
  const confirm=writes.find(w=>w.path.endsWith('/confirm'));assert.equal(confirm.body.assignment_student_id,1);assert.equal(confirm.body.confirmed,true);assert.equal(confirm.body.remember_identity,false);
  assert.equal(await card(1).count(),0);assert.equal(await page.locator('#class-dialog').count(),0,'inline candidate does not open full roster');
  fs.mkdirSync(path.join(root,'outputs'),{recursive:true});
  await page.evaluate(()=>document.querySelector('#toast').style.display='none');
  for(const width of [1440,900,390]){await page.setViewportSize({width,height:900});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await page.screenshot({path:path.join(root,`outputs/identity-attention-${width}.png`),fullPage:true});}
  await page.locator('#identity-review-remaining').click();await settled();assert(writes.some(w=>w.path.endsWith('/identity-review-jobs')));
  await page.goto('http://identity.test/assignments/1?tab=upload');await page.locator('#assignment-photo-archive > summary').click();await page.locator('#batch-photo-queue [data-identity-evidence="2"]').waitFor();
  assert.equal(await page.locator('#batch-photo-queue [data-identity-evidence="2"] img').count(),1);
  await page.evaluate(()=>{
   const p=classUI.batchPhotos.find(p=>p.id===2);
   p.match_details.candidates=[{id:1,student_code:'001',name:'张同学',observed_name:'张同字',distance:1,supplementary:false},
    {id:3,student_code:'003',name:'李别字',observed_name:'张同字',distance:2,supplementary:true}];
   p.identity_review={...(p.identity_review||{}),verification:{status:'completed',candidate_name:'张同学',result:{name:'张同学',decision:'match',confidence:'high',evidence:'签名末字笔画可辨'}}};
   refreshIdentityEvidence(2);
  });
  const evidence=page.locator('#batch-photo-queue [data-identity-evidence="2"]');
  assert.match(await evidence.innerText(),/唯一最佳候选/);
  assert.match(await evidence.innerText(),/签名候选核验.*张同学/s);
  const more=evidence.locator('details.identity-other-candidates');assert.equal(await more.count(),1);
  assert.equal(await more.locator('button').isVisible(),false);
  await more.locator('summary').click();assert.equal(await more.locator('button').isVisible(),true);
  await more.locator('summary').click();
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await page.screenshot({path:path.join(root,'outputs/identity-upload-390.png'),fullPage:true});
  await page.evaluate(()=>renderGradingJob({id:90,state:'done',stage:'ai_review',completed:2,total:28,ai_incomplete:true,errors:[],message:'模型返回截断，尚未完成'}));assert.match(await page.locator('#batch-job-status').innerText(),/AI 复核未完成/);
  assert.equal(await page.evaluate(()=>inferredPhoto({submission_id:1,identity_source:'machine',match_details:{method:'ai_identity'}})),true,'AI assignments keep correction entry');assert.deepEqual(errors,[]);console.log('PASS: original/OCR/AI/reason inline; loading, errors, unknown GET reconciliation; one-click existing manual endpoint; focus survives polling; explicit batch; 1440/900/390px attention/upload layouts');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
