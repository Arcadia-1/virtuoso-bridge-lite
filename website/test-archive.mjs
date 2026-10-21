/** Browser regression: node test-archive.mjs [site URL] [screenshot directory]. */
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
import {join} from 'node:path';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE_PATH??'playwright');
const base=new URL(process.argv[2]??'http://127.0.0.1:8790');
const output=process.argv[3];
if(output)await mkdir(output,{recursive:true});
const browser=await chromium.launch({headless:true,channel:process.env.PLAYWRIGHT_CHANNEL||undefined,args:['--disable-background-timer-throttling','--disable-renderer-backgrounding','--disable-backgrounding-occluded-windows']});
try {
  const context=await browser.newContext({viewport:{width:1440,height:1000},extraHTTPHeaders:{DNT:'1','Sec-GPC':'1'}});
  await context.addInitScript(()=>Object.defineProperty(navigator,'doNotTrack',{get:()=> '1'}));
  await context.route('**/*',route=>new URL(route.request().url()).origin===base.origin?route.continue():route.abort());
  const page=await context.newPage(),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(new URL('/?lang=en#ecosystem',base).href);
  await page.locator('.wf-window').scrollIntoViewIfNeeded();
  await page.mouse.move(2,2);
  await page.screenshot(output?{path:join(output,'integrated-waterfall.png')}:{});
  assert.equal(await page.locator('.wf-column[data-set="1"] .wf-copy:first-child .wf-record').count(),53);
  const links=await page.locator('.wf-column[data-set="1"] .wf-copy:first-child .wf-record').evaluateAll(es=>es.map(e=>e.href).sort());
  assert.deepEqual(links,await page.locator('#ecosystem-index li a').evaluateAll(es=>es.map(e=>e.href).sort()));
  const positions=()=>page.locator('.wf-column[data-set="1"] .wf-track').evaluateAll(es=>es.map(e=>new DOMMatrix(getComputedStyle(e).transform).m42));
  const initial=await positions();await page.waitForTimeout(600);
  assert((await positions()).every((v,i)=>v>initial[i]),'All columns flow downward');
  for(const i of [0,4,1,3,2]){
    await page.locator('#categories button').nth(i).click();await page.waitForTimeout(760);
    const v=await page.locator('.wf-window').boundingBox(),c=await page.locator(`.wf-column[data-set="1"][data-group="${i}"]`).boundingBox();
    assert(Math.abs(c.x+c.width/2-v.x-v.width/2)<2,'Selected collection centered');
  }
  const hit=await page.locator('.wf-column[data-set="1"][data-group="2"] .wf-record').evaluateAll(es=>{
    const v=document.querySelector('.wf-window').getBoundingClientRect();
    return es.map((e,i)=>{const r=e.getBoundingClientRect();return {i,x:r.x+40,y:r.y+60}}).find(r=>r.y>v.top+80&&r.y<v.bottom-100);
  });
  const card=page.locator('.wf-column[data-set="1"][data-group="2"] .wf-record').nth(hit.i).locator('.wf-card');
  const before=await card.boundingBox();
  await page.mouse.move(hit.x,hit.y);await page.waitForTimeout(350);
  const after=await card.boundingBox();
  assert.equal(after.width,before.width,'Hover must not enlarge card');
  assert.equal(after.height,before.height,'Hover must not enlarge card');
  const matrix=await card.evaluate(e=>{const m=new DOMMatrix(getComputedStyle(e).transform);return [m.a,m.d,m.f]});
  assert.deepEqual(matrix,[1,1,-6],'Only a six pixel lift');
  const hoveredName=await card.locator('.wf-title').textContent();
  assert.equal(await page.locator('.wf-detail-title').textContent(),hoveredName,'Hover details match current project');
  const held=await positions();await page.waitForTimeout(750);assert.deepEqual(await positions(),held,'Stationary hover pauses all columns');
  if(output)await page.screenshot({path:join(output,'integrated-waterfall-hover.png')});
  await page.mouse.move(2,2);await page.waitForTimeout(500);assert.notDeepEqual(await positions(),held,'Leaving resumes');
  assert.equal(await page.locator('.wf-toggle,.wf-bottom').count(),0,'No footer control row');
  const columnFocus=await page.locator('.wf-column[data-set="1"]').evaluateAll(es=>es.map(e=>({center:e.classList.contains('is-center'),blur:getComputedStyle(e).filter})));
  assert.equal(columnFocus.filter(c=>c.center).length,1);
  assert(columnFocus.every(c=>c.blur==='none'),'No whole-column blur');
  const edge=await page.locator('.wf-window').evaluate(e=>{const s=getComputedStyle(e,'::before');return {width:parseFloat(s.width),column:parseFloat(e.style.getPropertyValue('--wf-column-width')),mask:s.maskImage,filter:s.backdropFilter}});
  assert(edge.width<=edge.column/2+1&&edge.width>0,'Blur restricted to outer half');
  assert(edge.mask.includes('gradient')&&edge.filter.includes('blur'),'Graduated edge blur');
  await page.locator('.wf-detail-title').hover();
  const inDetail=await positions();await page.waitForTimeout(600);assert.notDeepEqual(await positions(),inDetail,'Right detail hover does not pause flow');
  await page.locator('.wf-detail-link').focus();
  const onLink=await positions();await page.waitForTimeout(400);assert.notDeepEqual(await positions(),onLink,'Right link focus does not pause flow');
  assert.match(await page.locator('.wf-detail-link').getAttribute('href'),/^https:\/\/github.com\//);
  await page.emulateMedia({reducedMotion:'reduce'});const reduced=await positions();await page.waitForTimeout(300);assert.deepEqual(await positions(),reduced);
  await page.locator('[data-language-toggle]').click();
  await page.waitForFunction(()=>document.querySelector('.wf-kind').textContent==='项目档案');
  assert.match(await page.locator('.wf-column[data-set="1"] .wf-description').first().textContent(),/Rust 重写/);
  await page.locator('#search').fill('PANDA layout');assert.equal(await page.locator('#ecosystem-index li:visible').count(),1);
  await page.locator('#index-clear').click();assert.equal(await page.locator('#ecosystem-index li:visible').count(),53);
  // A canonical keyboard link must be brought into the clipped viewport.
  await page.locator('#categories button').last().focus();await page.keyboard.press('Tab');
  const keyboardLink=page.locator('.wf-column[data-set="1"] .wf-copy:first-child .wf-record').nth(35);
  await keyboardLink.focus();await page.waitForTimeout(100);
  const k=await keyboardLink.boundingBox(),v=await page.locator('.wf-window').boundingBox();
  assert(k.y>=v.y&&k.y+k.height<v.y+v.height,'Keyboard card is readable inside window');
  await page.keyboard.press('Escape');
  for(const width of [1024,390,320]){
    await page.setViewportSize({width,height:900});await page.locator('.wf-window').scrollIntoViewIfNeeded();
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'No horizontal page overflow');
    await page.locator('#categories button').nth(4).click();
    if(output&&width===390)await page.screenshot({path:join(output,'integrated-waterfall-mobile.png')});
  }
  assert.deepEqual(errors,[]);
  console.log('PASS: 53 matching destinations; five collection pans; downward flow; stationary hover pauses with no enlargement; centered column; synchronized detail panel; resume; keyboard; Chinese content/search; reduced motion; mobile widths.');
} finally {await browser.close();}
