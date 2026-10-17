import { wrap, occurrence, damp, wave, visibility } from './motion.mjs';

const $ = id => document.getElementById(id);
const text = el => ({en:el.querySelector('.en').textContent,zh:el.querySelector('.zh').textContent});
const groups = [...document.querySelectorAll('#ecosystem-index h3')].map((h,i) => ({
  title:text(h),number:i,items:[...h.nextElementSibling.querySelectorAll('li')].map(li => ({
    name:li.querySelector('a').textContent,url:li.querySelector('a').href,
    description:text(li.querySelector('li > span')),li
  }))
}));
let lang=document.documentElement.dataset.lang||'en',group=2,index=0,opened=false;
const remembered=groups.map(()=>0),reduced=matchMedia('(prefers-reduced-motion: reduce)');
const cursor={value:0,velocity:0};
let target=0,cards=[],pulses=[],frame=0,lastTime=0,inView=true;
let mobile=innerWidth<700;
let hoverTimer=0,hoverId=null,lastPointer=null,hoverAnchor=null;
function cancelHover(){clearTimeout(hoverTimer);hoverTimer=0;hoverId=null;}
const total=groups.reduce((n,g)=>n+g.items.length,0);
$('total').textContent=`${total} PROJECTS / ${groups.length} COLLECTIONS`;
groups.forEach((g,i) => {
  const b=document.createElement('button');b.type='button';
  b.addEventListener('click',()=>{
    if(group===i)return;
    cancelHover();lastPointer=null;hoverAnchor=null;
    remembered[group]=index;group=i;index=remembered[i];opened=false;
    target=index;cursor.value=index;cursor.velocity=0;pulses=[];
    mount();render();
  });
  $('categories').append(b);
});

function mount() {
  // Stable project identities prevent a recycled sleeve crossing the rack.
  $('rack').replaceChildren();
  cards=groups[group].items.map((item,id)=>{
    const node=document.createElement('button');node.type='button';node.className='cassette';
    node.dataset.project=String(id);
    node.innerHTML=`<span class="case"><span class="case-back"></span>
      <span class="document"><span class="document-top"><span>VB / RESEARCH ARCHIVE</span><span class="document-number"></span></span>
      <span class="document-title"></span><span class="document-rule"></span><span class="document-description"></span>
      <span class="document-lines"></span><span class="document-stamp">PUBLIC<br>RECORD</span></span>
      <span class="sleeve"><span class="case-id"></span><span class="case-title"></span><span class="case-subtitle">OPEN SOURCE / VB</span></span></span>`;
    node.addEventListener('click',()=>{
      if(suppressClick){suppressClick=false;return;}
      cancelHover();
      select(occurrence(id,target,groups[group].items.length),true);
    });
    node.querySelector('.case-id').textContent=`VB-${group+1}.${String(id+1).padStart(2,'0')}`;
    node.querySelector('.document-number').textContent=String(id+1).padStart(2,'0');
    node.querySelector('.case-title').textContent=item.name.slice(item.name.indexOf('/')+1);
    node.querySelector('.document-title').textContent=item.name.slice(item.name.indexOf('/')+1);
    $('rack').append(node);
    return {node,document:node.querySelector('.document'),sheet:{value:0,velocity:0},reveal:{value:0,velocity:0},id};
  });
  paint(0,performance.now()/1000);
}

function render() {
  $('scene').setAttribute('aria-label',lang==='zh'?'档案架：左右方向键切换，Enter 抽取，Escape 收回':'Project archive: arrow keys to browse, Enter to extract, Escape to return');
  const g=groups[group],item=g.items[index];
  [...$('categories').children].forEach((b,i)=>{
    b.textContent=groups[i].title[lang];const count=document.createElement('small');
    count.textContent=groups[i].items.length;b.append(count);b.setAttribute('aria-pressed',String(i===group));
  });
  $('collection-code').textContent=`COLLECTION 0${group+1} / ${String(g.items.length).padStart(2,'0')} RECORDS`;
  $('record-id').textContent=`VB-${group+1}.${String(index+1).padStart(2,'0')}`;
  $('status').textContent=opened?'EXTRACTED':'IN SLEEVE';
  const slash=item.name.indexOf('/');$('owner').textContent=item.name.slice(0,slash);$('project').textContent=item.name.slice(slash+1);
  $('description').textContent=item.description[lang];$('record-category').textContent=g.title[lang];$('repository').href=item.url;
  $('position').textContent=`${String(index+1).padStart(2,'0')} / ${String(g.items.length).padStart(2,'0')}`;
  $('previous').setAttribute('aria-label',lang==='zh'?'上一个项目':'Previous project');
  $('next').setAttribute('aria-label',lang==='zh'?'下一个项目':'Next project');
  cards.forEach(({node,id})=>{
    const it=g.items[id];node.classList.toggle('active',id===index);
    node.querySelector('.document-description').textContent=it.description[lang];
    node.setAttribute('aria-label',`${it.name} — ${it.description[lang]}`);
    node.setAttribute('aria-pressed',String(id===index));node.tabIndex=id===index?0:-1;
  });
  start();
}

function select(next,extract=false) {
  if(next===target){if(extract&&!opened){opened=true;render();}return;}
  pulses.push({origin:cursor.value,time:performance.now()/1000});
  target=next;index=wrap(next,groups[group].items.length);opened=extract;render();
}

// React only to actual pointer travel. pointerenter would retrigger when the
// moving rack slides another folder underneath a stationary pointer.
$('scene').addEventListener('pointermove',e=>{
  if(e.pointerType!=='mouse'||e.buttons!==0)return;
  const point={x:e.clientX,y:e.clientY};
  if(lastPointer&&Math.hypot(point.x-lastPointer.x,point.y-lastPointer.y)<2)return;
  lastPointer=point;
  if(hoverAnchor&&Math.hypot(point.x-hoverAnchor.x,point.y-hoverAnchor.y)<12)return;
  const node=e.target.closest('.cassette');
  if(!node||node.getAttribute('aria-hidden')==='true'){cancelHover();return;}
  const id=Number(node.dataset.project);
  if(id===hoverId)return;
  cancelHover();hoverId=id;
  hoverTimer=setTimeout(()=>{
    hoverTimer=0;hoverAnchor=lastPointer;
    select(occurrence(id,target,groups[group].items.length),true);
  },120);
});
$('scene').addEventListener('pointerleave',()=>{cancelHover();lastPointer=null;hoverAnchor=null;});

function paint(dt,now,snap=false) {
  const count=cards.length;
  if(snap){cursor.value=target;cursor.velocity=0;pulses=[];}
  else damp(cursor,target,dt,8);
  pulses=pulses.filter(p=>now-p.time<2.2);
  let moving=Math.abs(cursor.value-target)>.0001||Math.abs(cursor.velocity)>.001||pulses.length>0;
  cards.forEach(card=>{
    const {node,id,sheet,reveal}=card;
    const at=occurrence(id,cursor.value,count),d=at-cursor.value;
    const focus=Math.exp(-d*d/1.1),alpha=visibility(d,count);
    // Interruptions retarget the same state rather than restarting a tween.
    const extraction=opened&&id===index?1:0;
    if(snap){sheet.value=extraction;sheet.velocity=0;reveal.value=1;reveal.velocity=0;}
    else{damp(sheet,extraction,dt,9);damp(reveal,1,dt,7);}
    const rawWave=pulses.reduce((n,p)=>n+wave(at-p.origin,now-p.time),0);
    const lift=16*Math.tanh(rawWave);
    const x=d*(mobile?57:77),y=24-26*focus-lift+sheet.value*12,z=90*focus-Math.abs(d)*30;
    node.style.transform=`translate3d(${x}px,${y}px,${z}px) rotateY(${-32*(1-focus)}deg) rotateZ(${-7*(1-focus)}deg)`;
    node.style.opacity=alpha*reveal.value;
    node.style.zIndex=String(100-Math.round(Math.abs(d)*10));
    node.style.pointerEvents=alpha<.08?'none':'auto';
    node.style.visibility=alpha===0?'hidden':'visible';
    node.setAttribute('aria-hidden',String(alpha===0));
    card.document.style.transform=`translateY(${-(mobile?112:135)*sheet.value}px)`;
    moving ||= Math.abs(sheet.value-extraction)>.0001||Math.abs(sheet.velocity)>.001||Math.abs(1-reveal.value)>.001;
  });
  return moving;
}

function tick(time) {
  frame=0;
  const dt=Math.min((time-lastTime)/1000,.05);lastTime=time;
  if(paint(dt,time/1000))frame=requestAnimationFrame(tick);
  else {paint(0,time/1000,true);$('scene').dataset.motion='idle';}
}
function start() {
  if(reduced.matches||document.hidden||!inView){stop();paint(0,performance.now()/1000,true);return;}
  if(!frame){$('scene').dataset.motion='running';lastTime=performance.now();frame=requestAnimationFrame(tick);}
}
function stop(){cancelAnimationFrame(frame);frame=0;$('scene').dataset.motion='idle';}
const move=delta=>{cancelHover();const focusInRack=$('rack').contains(document.activeElement);select(target+delta);if(focusInRack)cards[index].node.focus({preventScroll:true});};
$('next').onclick=()=>move(1);$('previous').onclick=()=>move(-1);
$('scene').addEventListener('keydown',e=>{
  cancelHover();
  if(e.key==='ArrowRight'||e.key==='ArrowLeft'){e.preventDefault();move(e.key==='ArrowRight'?1:-1);}
  else if(e.key==='Escape'){opened=false;render();}
  else if(e.key==='Enter'&&e.target===$('scene')){e.preventDefault();opened=!opened;render();}
});
document.querySelector('.reader').addEventListener('keydown',e=>{if(e.key==='Escape'){opened=false;render();}});
let touchStart=null,suppressClick=false;
$('scene').addEventListener('pointerdown',e=>{suppressClick=false;if(e.pointerType==='touch'){cancelHover();touchStart={x:e.clientX,y:e.clientY};}});
$('scene').addEventListener('pointerup',e=>{
  if(!touchStart)return;const dx=e.clientX-touchStart.x,dy=e.clientY-touchStart.y;touchStart=null;
  if(Math.abs(dx)>48&&Math.abs(dx)>Math.abs(dy)*1.5){suppressClick=true;move(dx<0?1:-1);}
});
$('scene').addEventListener('pointercancel',()=>touchStart=null);
new MutationObserver(()=>{const next=document.documentElement.dataset.lang;if(next!==lang){lang=next;render();search();}}).observe(document.documentElement,{attributes:true,attributeFilter:['data-lang']});
function search(){const term=$('search').value.trim().toLowerCase();let matches=0;groups.forEach(g=>{g.items.forEach(it=>{const match=(it.name+' '+it.description.en+' '+it.description.zh).toLowerCase().includes(term);it.li.hidden=!match;if(match)matches++;});const ul=g.items[0].li.parentElement;ul.hidden=g.items.every(it=>it.li.hidden);ul.previousElementSibling.hidden=ul.hidden;});$('search-status').textContent=term?(lang==='zh'?`${matches} 个匹配项目`:`${matches} matching projects`):'';}
$('search').addEventListener('input',search);
addEventListener('resize',()=>{mobile=innerWidth<700;start();});
new IntersectionObserver(([entry])=>{inView=entry.isIntersecting;start();},{threshold:0}).observe($('scene'));
document.addEventListener('visibilitychange',()=>{if(document.hidden)cancelHover();start();});reduced.addEventListener('change',start);
mount();render();
