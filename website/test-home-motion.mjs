/** Browser regression: node test-home-motion.mjs [site URL]. */
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE_PATH??'playwright');
const base=new URL(process.argv[2]??'http://127.0.0.1:8790');
const browser=await chromium.launch({headless:true,channel:process.env.PLAYWRIGHT_CHANNEL||undefined});
try {
  const context=await browser.newContext({viewport:{width:1440,height:1000},extraHTTPHeaders:{DNT:'1','Sec-GPC':'1'}});
  await context.route('**/*',route=>new URL(route.request().url()).origin===base.origin?route.continue():route.abort());
  await context.route('**/api/track**',route=>route.fulfill({status:204}));
  await context.addInitScript(() => {
    window.routeFrames=[];
    let started;
    const capture=()=>{
      const route=document.querySelector('#route-virtuoso');
      if(route){started??=performance.now();window.routeFrames.push(Number.parseFloat(getComputedStyle(route).strokeDashoffset));}
      if(!started||performance.now()-started<4500)requestAnimationFrame(capture);
    };
    requestAnimationFrame(capture);
  });
  await context.route('**/assets/home/home.mjs*',async route=>{
    await new Promise(resolve=>setTimeout(resolve,2800));
    await route.continue();
  });
  const page=await context.newPage(),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.goto(base.href);
  await page.locator('.system-motion').waitFor({state:'attached'});
  assert.equal(await page.locator('.wf-copy:not([data-original])').count(),0,'Offscreen cards must not compete with the first-screen entrance');
  await page.waitForTimeout(2000);
  const firstFrames=await page.evaluate(()=>window.routeFrames);
  assert(firstFrames[0]>95,'The first painted output route starts hidden, even before the module arrives');
  assert(firstFrames.at(-1)<1,'The CSS entrance completes even while the module is delayed');
  assert(firstFrames.every((value,i)=>!i||value<=firstFrames[i-1]+.1),'Routes never reset from complete to hidden');
  await context.unroute('**/assets/home/home.mjs*');
  const animationState=()=>page.locator('.system-motion').evaluate(e=>e.getAnimations({subtree:true}).map(a=>({time:a.currentTime,state:a.playState})));
  const before=await animationState();
  await page.waitForTimeout(250);
  assert((await animationState()).every((a,i)=>a.state==='running'&&a.time>before[i].time),'Signals advance');
  assert(await page.locator('.system-motion').evaluate(e=>e.getAnimations({subtree:true}).every(a=>a.effect.getKeyframes().every(f=>Object.keys(f).every(k=>['offset','computedOffset','easing','composite','opacity','transform'].includes(k))))),'Continuous signals animate only transform and opacity');
  // Check actual packet positions against the SVG after resizing, not just CSS rules.
  for(const width of [1440,390]) {
    await page.setViewportSize({width,height:1000});
    await page.locator('.hero-flow').scrollIntoViewIfNeeded();
    await page.waitForTimeout(120);
    const deviation=await page.evaluate(()=>{
      const figure=document.querySelector('.hero-flow'),svg=figure.querySelector('svg');
      const routes=[...svg.querySelectorAll('.system-routes path')];
      const points=routes.map(route=>Array.from({length:301},(_,i)=>route.getPointAtLength(route.getTotalLength()*i/300)));
      const plane=figure.querySelector('.system-motion'),animations=plane.getAnimations({subtree:true});
      animations.forEach(a=>{a.pause();a.currentTime=4400;});
      return new Promise(resolve=>requestAnimationFrame(()=>{
        const bounds=svg.getBoundingClientRect(),scale=bounds.width/svg.viewBox.baseVal.width;
        const deviations=[...plane.querySelectorAll('.system-packet')].map((packet,i)=>{
          if(Number(getComputedStyle(packet).opacity)<.1)return 0;
          const r=packet.getBoundingClientRect(),x=(r.x+r.width/2-bounds.x)/scale,y=(r.y+r.height/2-bounds.y)/scale;
          return Math.min(...points[Math.floor(i/5)].map(p=>Math.hypot(p.x-x,p.y-y)));
        });
        animations.forEach(a=>a.play());
        resolve(Math.max(...deviations));
      }));
    });
    assert(deviation<2,'Signal layers stay on their routes after resizing');
  }
  await page.locator('#ecosystem').scrollIntoViewIfNeeded();
  await page.locator('.archive').waitFor({state:'visible'});
  await page.waitForFunction(()=>document.querySelector('.system-motion').getAnimations({subtree:true}).every(a=>a.playState==='paused'));
  const held=await animationState();await page.waitForTimeout(200);
  assert.deepEqual(await animationState(),held,'Offscreen hero does no continuous animation work');
  assert.equal(await page.locator('[data-original] .wf-record').count(),53);
  await page.locator('.hero-flow').scrollIntoViewIfNeeded();
  await page.waitForFunction(()=>document.querySelector('.system-motion').getAnimations({subtree:true}).every(a=>a.playState==='running'));
  await page.emulateMedia({reducedMotion:'reduce'});
  await page.waitForFunction(()=>document.querySelector('.hero-flow').classList.contains('system-reduced'));
  assert.equal(await page.locator('.system-motion').isVisible(),false);
  assert(await page.locator('.system-routes path').evaluateAll(es=>es.every(e=>getComputedStyle(e).strokeDasharray==='none')),'Reduced motion shows complete static routes');
  await page.reload();
  await page.waitForTimeout(150);
  assert.equal(await page.locator('.system-motion').count(),0,'Reduced motion skips signal setup entirely');
  await page.goto(new URL('/?lang=zh#eco-tools',base).href);
  await page.locator('.archive').waitFor({state:'visible'});
  assert.equal(await page.locator('[data-original] .wf-record').count(),53,'Deep links retain all original projects');
  assert.equal(await page.locator('#eco-tools').isVisible(),true);
  assert.deepEqual(errors,[]);
  console.log('PASS: deferred archive; compositor-only signal properties; desktop/mobile route alignment; offscreen pause/resume; reduced motion; direct collection anchors.');
} finally {await browser.close();}
