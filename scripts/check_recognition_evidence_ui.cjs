// Destructive teacher decision only on the synthetic 5033 fixture.
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[],writes=[];
 const base='http://127.0.0.1:5033';
 page.on('pageerror',e=>errors.push(e.message));
 page.on('request',r=>{if(r.method()!=='GET')writes.push(new URL(r.url()).pathname);});
 try{
  await page.goto(base+'/submissions/1?question=2&filter=uncertain');
  await page.locator('.recognition-evidence').waitFor();
  const evidence=await page.locator('.recognition-evidence').innerText();
  for(const text of ['识别结果不一致','整页识别','局部补识别','a kind of code','demand'])assert(evidence.includes(text));
  assert.equal(await page.locator('.review-card.active').getAttribute('data-question-id'),'2');
  await page.screenshot({path:'outputs/recognition-evidence-desktop.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  await page.screenshot({path:'outputs/recognition-evidence-mobile.png',fullPage:true});
  await page.locator('[data-action=quick-correct]').click();
  await page.waitForFunction(()=>!state.busy&&state.submission.uncertain_count===0);
  const sub=await (await page.request.get(base+'/api/submissions/1')).json();
  assert.equal(sub.score,2);assert.equal(sub.reviews[1].teacher_decision,'correct');
  await page.goto(base+'/submissions/1?question=2');
  await page.locator('.recognition-evidence').waitFor();
  assert.match(await page.locator('.recognition-evidence').innerText(),/教师判定已保存/);
  assert.deepEqual(writes,['/api/submissions/1/reviews/2']);
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({passed:true,bothReadings:true,mobile:true,teacherResolves:true,noOCR:true,errors}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
