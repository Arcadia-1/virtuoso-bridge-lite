/** Browser regression: node test-archive.mjs [site URL]. */
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE_PATH??'playwright');
const expected=JSON.parse(readFileSync(new URL('./homepage-content.json',import.meta.url),'utf8')).projects;
const base=new URL(process.argv[2]??'http://127.0.0.1:8790');
const browser=await chromium.launch({headless:true,channel:process.env.PLAYWRIGHT_CHANNEL||undefined});
try {
  const context=await browser.newContext({viewport:{width:1440,height:1000},extraHTTPHeaders:{DNT:'1','Sec-GPC':'1'}});
  await context.route('**/*',route=>new URL(route.request().url()).origin===base.origin?route.continue():route.abort());
  const page=await context.newPage(),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(new URL('/?lang=en#ecosystem',base).href);
  await page.locator('[data-ready]').waitFor();
  await page.mouse.move(2,2);
  const positions=()=>page.locator('.wf-column[id] .wf-window').evaluateAll(es=>es.map(e=>e.scrollTop));
  const periods=await page.locator('[data-original]').evaluateAll(es=>es.map(e=>e.offsetHeight));
  const links=await page.locator('[data-original] .wf-record').evaluateAll(es=>es.map(e=>e.href).sort());
  assert.deepEqual(links,expected.flatMap(g=>g.items.map(i=>i.url)).sort());
  assert.equal(await page.locator('.wf-detail,#full-index,#ecosystem-index').count(),0);
  assert.deepEqual(await page.locator('#categories small').allTextContents(),['14','11','14','4','10']);
  const bounds=await page.locator('.wf-column[id]').evaluateAll(es=>es.map(e=>e.getBoundingClientRect().toJSON()));
  assert(bounds.every(r=>r.x>=0&&r.right<=1440),'All five columns are completely visible on desktop');
  const initial=await positions();await page.waitForTimeout(650);
  const deltas=(await positions()).map((v,i)=>(initial[i]-v+periods[i])%periods[i]);
  assert(deltas.every(d=>d>2&&d<25),`Five columns move slowly downward: ${deltas}`);
  const motion=page.locator('[data-wf-motion]');
  await motion.focus();await page.keyboard.press('Enter');
  assert.equal(await motion.innerText(),'Resume');
  for (const i of [0,4,1,3,2]) {
    await page.locator('#categories a').nth(i).click();
    await page.waitForTimeout(750);
    const centered=await page.locator('.wf-column[id]').nth(i).evaluate(e=>{
      const a=e.getBoundingClientRect(),b=e.closest('.wf-browser').getBoundingClientRect();
      return Math.abs(a.x+a.width/2-b.x-b.width/2)<2;
    });
    assert(centered,'Every selected collection, including either end, pans to the center');
    assert.equal(await page.locator('#categories a').nth(i).getAttribute('aria-current'),'true');
  }
  const previous=page.locator('[data-wf-step="-1"]'),next=page.locator('[data-wf-step="1"]');
  for(const [direction,index] of [[-1,1],[-1,0],[1,1],[1,2],[1,3],[1,4]]){
    await (direction<0?previous:next).click();await page.waitForTimeout(720);
    assert.equal(await page.locator('#categories a').nth(index).getAttribute('aria-current'),'true');
    const centered=await page.locator('.wf-column[id]').nth(index).evaluate(e=>{const a=e.getBoundingClientRect(),b=e.closest('.wf-browser').getBoundingClientRect();return Math.abs(a.x+a.width/2-b.x-b.width/2)<2;});
    assert(centered,'Side arrows center the chosen collection');
    assert.equal(await previous.isDisabled(),index===0);assert.equal(await next.isDisabled(),index===4);
  }
  await previous.focus();await page.keyboard.press('Enter');await page.waitForTimeout(720);
  assert.equal(await page.locator('#categories a').nth(3).getAttribute('aria-current'),'true','Keyboard activates arrow without a focus error');
  await page.locator('#categories a').nth(2).click();await page.waitForTimeout(720);
  const edge=await page.locator('.wf-browser').evaluate(e=>{const s=getComputedStyle(e,'::before');return {width:parseFloat(s.width),column:parseFloat(e.style.getPropertyValue('--wf-column-width')),mask:s.maskImage,filter:s.backdropFilter}});
  assert(edge.width<=edge.column/2+1&&edge.width>0&&edge.mask.includes('gradient')&&edge.filter.includes('blur'),'Graduated blur covers only the outer half-column');
  assert(await page.locator('.wf-window').evaluateAll(es=>es.every(e=>getComputedStyle(e).scrollbarWidth==='none')),'No visible per-column scrollbars');
  const stopped=await positions();await page.waitForTimeout(350);
  assert.deepEqual(await positions(),stopped,'Persistent keyboard pause');
  await page.locator('[data-language-toggle]').click();
  await page.waitForFunction(()=>document.querySelector('[data-wf-motion] span').textContent==='继续');
  assert.match(await page.locator('[data-original] .wf-description').first().innerText(),/Rust 重写/);
  await page.locator('[data-language-toggle]').click();
  await page.locator('#ecosystem').scrollIntoViewIfNeeded();
  await page.waitForTimeout(300);
  const translated=await positions();await page.waitForTimeout(350);
  assert.deepEqual(await positions(),translated,'Language changes preserve pause');
  await motion.focus();await page.keyboard.press('Space');await page.mouse.move(2,2);
  const restarted=await positions();await page.waitForTimeout(400);
  assert.notDeepEqual(await positions(),restarted,'Space resumes flow');
  // Hover keeps the wall moving; cards still retain their own immutable links.
  const target=await page.locator('.wf-column[id]').nth(2).locator('.wf-record').evaluateAll(es=>{
    const v=es[0].closest('.wf-window').getBoundingClientRect();
    return es.map((e,i)=>{const r=e.getBoundingClientRect();return {i,x:r.x+50,y:r.y+60,href:e.href};}).find(r=>r.y>v.top+70&&r.y<v.bottom-70);
  });
  await page.mouse.move(target.x,target.y);await page.waitForTimeout(350);
  const held=await positions();await page.waitForTimeout(600);
  assert.notDeepEqual(await positions(),held,'Stationary hover does not pause the five columns');
  const hovered=page.locator('.wf-column[id]').nth(2).locator('.wf-record').nth(target.i);
  assert.deepEqual(await hovered.locator('.wf-card').evaluate(e=>{const m=new DOMMatrix(getComputedStyle(e).transform);return[m.a,m.d,m.f]}),[1,1,-6]);
  await context.route('https://github.com/**',route=>route.fulfill({status:200,contentType:'text/html',body:'Verified destination'}));
  const popupPromise=context.waitForEvent('page');await page.mouse.click(target.x,target.y);
  const popup=await popupPromise;await popup.waitForURL(target.href);await popup.close();await page.bringToFront();
  await page.mouse.move(2,2);await page.waitForTimeout(400);
  assert.notDeepEqual(await positions(),held,'Mouse selection does not permanently lock the wall');
  // Every canonical project is keyboard reachable and scrolls into its own column.
  await page.keyboard.press('Tab');
  for(const link of await page.locator('[data-original] .wf-record').all()){
    await link.focus();
    const visible=await link.evaluate(e=>{const a=e.getBoundingClientRect(),b=e.closest('.wf-window').getBoundingClientRect();return a.top>=b.top-1&&a.bottom<=b.bottom+1;});
    assert(visible,`Keyboard project clipped: ${await link.getAttribute('href')}`);
  }
  const focused=await positions();await page.waitForTimeout(300);
  assert.deepEqual(await positions(),focused,'Keyboard focus pauses motion');
  await page.emulateMedia({reducedMotion:'reduce'});
  await page.waitForFunction(()=>document.querySelector('[data-wf-motion]').disabled);
  assert(await motion.isDisabled());
  await page.mouse.move(2,2);await page.locator('h2').first().click();
  await page.locator('#ecosystem').scrollIntoViewIfNeeded();
  const reduced=await positions();await page.waitForTimeout(350);
  assert.deepEqual(await positions(),reduced,'Reduced motion stays static');
  for(const width of [1024,390,320]){
    await page.setViewportSize({width,height:900});
    await page.locator('#categories a').last().click();
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'No whole-page horizontal overflow');
    const last=await page.locator('.wf-column[id]').last().boundingBox();
    assert(last.x>=0&&last.x+last.width<=width,'Narrow screens can navigate to the fifth collection');
  }
  assert.deepEqual(errors,[]);
  const touchContext=await browser.newContext({viewport:{width:390,height:844},isMobile:true,hasTouch:true,extraHTTPHeaders:{DNT:'1'}});
  const touch=await touchContext.newPage();await touch.goto(new URL('/?lang=en#ecosystem',base).href);await touch.locator('[data-ready]').waitFor();
  const control=touch.locator('[data-wf-motion]');await control.tap();
  assert.equal(await control.innerText(),'Resume');
  const box=await control.boundingBox();assert(box.width>=44&&box.height>=44);
  await control.tap();assert.equal(await control.innerText(),'Pause');
  await touch.locator('[data-wf-step="-1"]').tap();
  await touch.waitForTimeout(720);
  assert.equal(await touch.locator('#categories a').nth(1).getAttribute('aria-current'),'true','Touch arrow selects the preceding collection');
  assert.equal(await control.innerText(),'Pause','Touching an arrow does not persistently pause motion');
  await touch.locator('.wf-window').first().dispatchEvent('pointerdown',{pointerType:'touch'});
  assert.equal(await control.innerText(),'Resume','Touch browsing keeps the wall still until explicitly resumed');
  await touchContext.close();
  console.log('PASS: all 53 destinations; five visible lanes; downward loop during hover; side arrows and keyboard; lift; real GitHub click; keyboard access to every project; persistent pause; bilingual, reduced-motion and narrow/touch browsing.');
} finally {await browser.close();}
