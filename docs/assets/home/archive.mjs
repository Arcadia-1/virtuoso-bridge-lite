import { createWaterfall } from './waterfall.mjs?v=20261029-smooth';

const $ = id => document.getElementById(id);
const text = el => ({en:el.querySelector('.en').textContent,zh:el.querySelector('.zh').textContent});
const groups = [...document.querySelectorAll('#ecosystem-index h3')].map((h,i) => ({
  title:text(h),number:i,items:[...h.nextElementSibling.querySelectorAll('li')].map(li => ({
    name:li.querySelector('a').textContent,url:li.querySelector('a').href,
    description:text(li.querySelector('li > span')),li
  }))
}));
let lang=document.documentElement.dataset.lang||'en';
const total=groups.reduce((n,g)=>n+g.items.length,0);
let waterfall;
function initialize(){
  if(waterfall)return;
  waterfall=createWaterfall(document.querySelector('.archive'),groups,()=>lang);
  approaching.disconnect();
}
// Do not construct hundreds of offscreen card copies during the hero entrance.
const approaching=new IntersectionObserver(entries=>{
  if(entries.some(entry=>entry.isIntersecting))initialize();
},{rootMargin:'700px 0px'});
approaching.observe(document.getElementById('ecosystem'));
function translate(){
  $('total').textContent=`${total} PROJECTS / ${groups.length} COLLECTIONS`;
  waterfall?.translate();
}
new MutationObserver(()=>{
  const next=document.documentElement.dataset.lang;
  if(next!==lang){lang=next;translate();}
}).observe(document.documentElement,{attributes:true,attributeFilter:['data-lang']});
function revealIndexAnchor(){
  const heading=document.getElementById(location.hash.slice(1));
  if(!heading?.matches('#ecosystem-index h3'))return;
  initialize();
  $('full-index').open=true;
  heading.scrollIntoView({block:'start'});
}
addEventListener('hashchange',revealIndexAnchor);

translate();revealIndexAnchor();
