// Test only with the synthetic recognition_evidence_ui_server fixture.
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const assert=require('node:assert/strict');

(async()=>{
  const base='http://127.0.0.1:'+Number(process.env.RECOGNITION_TEST_PORT||5033);
  assert.notEqual(new URL(base).port,'5000');
  const browser=await chromium.launch({channel:'msedge',headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:1000}});
  const errors=[],writes=[];
  page.on('pageerror',error=>errors.push(error.message));
  page.on('request',r=>{if(r.method()==='POST') writes.push(new URL(r.url()).pathname);});
  try {
    const path='/submissions/1?question=2&filter=uncertain';
    await page.goto(base+path);
    await page.getByRole('button',{name:'框选作答'}).click();
    const stage=await page.locator('#photo-stage').boundingBox();
    await page.mouse.move(stage.x+stage.width*.18,stage.y+stage.height*.38);
    await page.mouse.down();
    await page.mouse.move(stage.x+stage.width*.53,stage.y+stage.height*.50,{steps:8});
    await page.mouse.up();
    assert.deepEqual(writes,[]);
    await page.getByRole('button',{name:'发送局部图复读'}).click();
    await page.locator('.visual-review img').waitFor();
    await page.waitForFunction(()=>document.querySelector('.visual-review img')?.naturalWidth>0);
    assert.equal(await page.evaluate(()=>state.submission.reviews[1].uncertain),true);
    assert.deepEqual(writes,['/api/submissions/1/reviews/2/visual-reread']);
    await page.reload();
    await page.locator('.visual-review img').waitFor();
    await page.setViewportSize({width:390,height:844});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    await page.getByRole('button',{name:'采纳复读建议'}).click();
    await page.waitForFunction(()=>!state.busy && state.submission.reviews[1].confirmed);
    assert.equal(await page.evaluate(()=>state.submission.reviews[1].transcript),'demand');
    await page.locator('[data-action="undo-review"]').click();
    await page.waitForFunction(()=>!state.busy && state.submission?.reviews[1].uncertain);
    assert.deepEqual(errors,[]);
    assert.deepEqual(writes,['/api/submissions/1/reviews/2/visual-reread','/api/submissions/1/reviews/2','/api/submissions/1/reviews/2/undo']);
    await page.getByRole('button',{name:'整页多题复读',exact:true}).click();
    await page.getByRole('button',{name:'采纳复读建议',exact:true}).waitFor();
    assert.equal(await page.evaluate(()=>state.submission.visual_suggestions[2].scope),'page');
    await page.reload();
    await page.getByRole('button',{name:'采纳复读建议',exact:true}).waitFor();
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    await page.getByRole('button',{name:'采纳复读建议',exact:true}).click();
    await page.waitForFunction(()=>!state.busy && state.submission?.reviews[1].confirmed);
    await page.locator('[data-action="undo-review"]').click();
    await page.waitForFunction(()=>!state.busy && state.submission?.reviews[1].uncertain);
    assert.deepEqual(errors,[]);
    console.log('PASS: crop, whole page, preview, refresh, adoption, undo, 390px');
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
