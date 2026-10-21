import { matchesQuery } from './home.mjs';
import { createWaterfall } from './waterfall.mjs';

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
const waterfall=createWaterfall(document.querySelector('.archive'),groups,()=>lang);
function translate(){
  $('total').textContent=`${total} PROJECTS / ${groups.length} COLLECTIONS`;
  waterfall.translate();
  search();
}
new MutationObserver(()=>{
  const next=document.documentElement.dataset.lang;
  if(next!==lang){lang=next;translate();}
}).observe(document.documentElement,{attributes:true,attributeFilter:['data-lang']});
function search(){
  const term=$('search').value.trim().toLowerCase();let matches=0;
  groups.forEach(g=>{
    let count=0;
    g.items.forEach(it=>{
      const match=matchesQuery(it.name+' '+it.description.en+' '+it.description.zh+' '+g.title.en+' '+g.title.zh,term);
      it.li.hidden=!match;if(match){matches++;count++;}
    });
    const ul=g.items[0].li.parentElement,heading=ul.previousElementSibling;
    ul.hidden=count===0;heading.hidden=count===0;
    heading.querySelector('.count').textContent=term?`${count} / ${g.items.length}`:g.items.length;
  });
  $('index-clear').disabled=!term;
  $('search').placeholder=lang==='zh'?'项目名称或关键词':'Name or keyword';
  $('eco-empty').hidden=matches!==0;
  $('search-status').textContent=term
    ?(lang==='zh'?`${matches} 个匹配项目`:`${matches} matching projects`)
    :(lang==='zh'?`显示全部 ${total} 个项目`:`Showing all ${total} projects`);
}
$('search').addEventListener('input',search);
$('index-clear').addEventListener('click',()=>{$('search').value='';search();$('search').focus();});
document.querySelector('.eco-categories').addEventListener('click',e=>{
  if(!e.target.closest('a'))return;
  $('search').value='';search();
});
function revealIndexAnchor(){
  const heading=document.getElementById(location.hash.slice(1));
  if(!heading?.matches('#ecosystem-index h3'))return;
  $('full-index').open=true;
  if(heading.hidden){$('search').value='';search();}
  heading.scrollIntoView({block:'start'});
}
addEventListener('hashchange',revealIndexAnchor);

translate();revealIndexAnchor();
$('eco-search-controls').hidden=false;
