// Netlist timing: 1 ns period, 0.5 ns pulse width, 20 ps edges, 0.9 V.
// Output is the ideal logical inverse, not a transistor-level simulation result.
const input=document.getElementById('wave-input'),output=document.getElementById('wave-output');
function pulse(t){const phase=t%1;return phase<.02?phase/.02:phase<.52?1:phase<.54?1-(phase-.52)/.02:0;}
function demoPath(inverted){return Array.from({length:1501},(_,i)=>{const t=i/500;const level=inverted?1-pulse(t):pulse(t);return `${i?'L':'M'}${(50+t/3*428).toFixed(2)},${((inverted?173:88)-50*level).toFixed(2)}`;}).join(' ');}
input.setAttribute('d',demoPath(false));output.setAttribute('d',demoPath(true));
const lab=document.getElementById('lab'),run=document.getElementById('run-demo'),status=document.getElementById('demo-status');
let timer;
run.addEventListener('click',()=>{
  clearTimeout(timer);lab.classList.remove('playing');
  requestAnimationFrame(()=>{lab.classList.add('playing');run.disabled=true;status.innerHTML='<span class="en">Playing demo…</span><span class="zh">正在播放演示…</span>';});
  timer=setTimeout(()=>{run.disabled=false;status.innerHTML='<span class="en">Demo complete · Replay ↺</span><span class="zh">演示结束 · 可重新播放 ↺</span>';lab.classList.remove('playing');},matchMedia('(prefers-reduced-motion: reduce)').matches?100:2400);
});
document.addEventListener('visibilitychange',()=>{if(document.hidden){clearTimeout(timer);lab.classList.remove('playing');run.disabled=false;status.innerHTML='<span class="en">Ready</span><span class="zh">就绪</span>';}});
// #ecosystem is the visible entry; the complete index stays discoverable.
document.querySelectorAll('a[href="#ecosystem"]').forEach(a=>a.addEventListener('click',()=>document.getElementById('ecosystem').scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth'})));

// Small, one-shot paper movements; content remains visible without scripting.
const paperMotion=matchMedia('(prefers-reduced-motion: reduce)');
const paperSections=[...document.querySelectorAll('.light-content,.resources')];
const paperTimers=new Set();
const paperObserver=new IntersectionObserver(entries=>{
  for(const entry of entries){
    if(!entry.isIntersecting)continue;
    paperObserver.unobserve(entry.target);
    if(paperMotion.matches)continue;
    entry.target.classList.add('motion-enter');
    const timer=setTimeout(()=>{entry.target.classList.remove('motion-enter');paperTimers.delete(timer);},1400);
    paperTimers.add(timer);
  }
},{threshold:.12});
paperSections.forEach(section=>paperObserver.observe(section));
function settlePaper(){paperTimers.forEach(clearTimeout);paperTimers.clear();paperSections.forEach(section=>section.classList.remove('motion-enter'));}
paperMotion.addEventListener('change',settlePaper);
document.addEventListener('visibilitychange',()=>{if(document.hidden)settlePaper();});


// Screen decoration only runs when visible; never flashes or moves the text.
const crt=document.querySelector('.crt-console');
let crtVisible=false;
function updateScreen(){crt.classList.toggle('screen-active',crtVisible&&!document.hidden&&!paperMotion.matches);}
new IntersectionObserver(([entry])=>{crtVisible=entry.isIntersecting;updateScreen();},{threshold:.1}).observe(crt);
document.addEventListener('visibilitychange',updateScreen);
paperMotion.addEventListener('change',updateScreen);

// Align signal paths to the real node positions at every responsive size.
const screen=crt.querySelector('.crt-screen');
const signalSvg=document.createElementNS('http://www.w3.org/2000/svg','svg');
signalSvg.classList.add('crt-signals');signalSvg.setAttribute('aria-hidden','true');screen.append(signalSvg);
const signalPaths=Array.from({length:4},(_,i)=>{
  const base=document.createElementNS(signalSvg.namespaceURI,'path');base.classList.add('crt-wire');
  const pulse=document.createElementNS(signalSvg.namespaceURI,'path');pulse.classList.add('crt-pulse',i===0?'signal-in':'signal-out');pulse.setAttribute('pathLength','1');
  signalSvg.append(base,pulse);return {base,pulse};
});
function layoutSignals(){
  const origin=screen.getBoundingClientRect();
  const offset={x:origin.left+screen.clientLeft,y:origin.top+screen.clientTop};
  const box=el=>{const b=el.getBoundingClientRect();return {x:b.left+b.width/2-offset.x,top:b.top-offset.y,bottom:b.bottom-offset.y};};
  const agent=box(crt.querySelector('.crt-agent')),bridge=box(crt.querySelector('.crt-core'));
  const nodes=[...crt.querySelectorAll('.crt-targets b')].map(box);
  signalSvg.setAttribute('viewBox',`0 0 ${screen.clientWidth} ${screen.clientHeight}`);
  const bus=bridge.bottom+22;
  const routes=[`M${agent.x} ${agent.bottom}V${bridge.top}`,...nodes.map(n=>`M${bridge.x} ${bridge.bottom}V${bus}H${n.x}V${n.top}`)];
  routes.forEach((d,i)=>{signalPaths[i].base.setAttribute('d',d);signalPaths[i].pulse.setAttribute('d',d);});
}
new ResizeObserver(layoutSignals).observe(screen);layoutSignals();
