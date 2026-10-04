(function(){
'use strict';
let iframe=null,ready=false,pendingFrame=null;
function host(){return document.getElementById('reviewCesium');}
function fallback(){return document.getElementById('reviewCesiumFallback');}
function post(msg){if(iframe&&iframe.contentWindow&&ready)iframe.contentWindow.postMessage(msg,window.location.origin);}
function init(){
  if(iframe)return true; const h=host(); if(!h)return false;
  h.innerHTML=''; iframe=document.createElement('iframe');
  iframe.id='reviewFeatherFrame'; iframe.src='/review_feather/map.html'; iframe.title='Feather Flight historical replay';
  iframe.style.cssText='width:100%;height:100%;border:0;display:block;background:#071019';
  h.appendChild(iframe); return true;
}
window.addEventListener('message',ev=>{
  if(ev.origin!==window.location.origin||!iframe||ev.source!==iframe.contentWindow)return;
  if(ev.data&&ev.data.type==='omni-review-feather-ready'){ready=true;const f=fallback();if(f)f.classList.add('hidden');if(pendingFrame)post({type:'omni-review-frame',frame:pendingFrame});}
});
function render(frame){pendingFrame=frame;init();post({type:'omni-review-frame',frame});}
function command(c){init();post({type:'omni-review-command',command:c});}
function recenter(){command('follow');command('recenter');}
function reset(){pendingFrame=null;ready=false;if(iframe){iframe.remove();iframe=null;}const h=host();if(h){const f=document.createElement('div');f.id='reviewCesiumFallback';f.className='reviewViewerFallback';f.textContent='Initializing Feather Flight historical viewer…';h.appendChild(f);}}
window.OmniReviewRenderer={init,render,recenter,reset,ready:()=>ready,setFollow:on=>command(on?'follow':'free'),topView:()=>command('top'),mode:'feather-flight'};
})();
