(function(){
'use strict';
const PROTOCOL='omni.instructor.review.v1';
const API_BASE='/api/review/v1';
let seq=0,catalog=[],reviewMeta=null,snapshot=null,tickTimer=null,returnPage='complete',opening=false;
function rid(){seq+=1;return 'review-ui-'+Date.now()+'-'+seq;}
function el(id){return document.getElementById(id);}
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function fmtTime(ns){const ms=Math.max(0,Math.round(Number(ns||0)/1e6)),m=Math.floor(ms/60000),s=Math.floor((ms%60000)/1000),r=ms%1000;return String(m).padStart(2,'0')+':'+String(s).padStart(2,'0')+'.'+String(r).padStart(3,'0');}
function fmt(v,d=2){const n=Number(v);return Number.isFinite(n)?n.toFixed(d):'—';}
function reviewMessage(text,bad=false){if(typeof message==='function')message(text,bad);}
async function call(op,payload={}){
  const r=await fetch(API_BASE+'/'+op,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({protocol:PROTOCOL,request_id:rid(),...payload})});
  let j={};try{j=await r.json();}catch(_e){}
  if(!r.ok||j.ok===false){const e=j&&j.error;const err=new Error((e&&e.message)||('Review request failed: '+r.status));err.code=e&&e.code;throw err;}
  return j.result;
}
function setBusy(flag){opening=flag;['reviewRefreshBtn','reviewOpenSelectedBtn'].forEach(id=>{const x=el(id);if(x)x.disabled=flag;});}
async function openReviewBrowser(preferredAttemptId=null,autoOpen=false){
  if(typeof currentUiPage==='function')returnPage=currentUiPage();
  if(returnPage==='review')returnPage='complete';
  if(typeof show==='function')show('review');
  if(window.OmniReviewRenderer)window.OmniReviewRenderer.init();
  await reviewLoadCatalog(preferredAttemptId,autoOpen);
}
async function openEndedReview(){
  const attemptId=(typeof latest!=='undefined'&&latest&&latest.current&&latest.current.attempt_id)||null;
  await openReviewBrowser(attemptId,true);
}
async function reviewLoadCatalog(preferredAttemptId=null,autoOpen=false){
  setBusy(true);
  try{
    const res=await call('list');catalog=Array.isArray(res.attempts)?res.attempts:[];
    const sel=el('reviewAttemptSelect');sel.innerHTML='';
    if(!catalog.length){sel.innerHTML='<option value="">No reviewable Ended Attempts</option>';el('reviewCatalogStatus').textContent='No Ended evidence package is currently reviewable.';return;}
    for(const a of catalog){const o=document.createElement('option');o.value=a.attempt_id;o.textContent=(a.attempt_id||'').slice(0,8)+' · '+(a.scenario||a.exercise_id||'Exercise')+' · '+Number(a.duration_s||0).toFixed(1)+' s';sel.appendChild(o);}
    if(preferredAttemptId&&catalog.some(a=>a.attempt_id===preferredAttemptId))sel.value=preferredAttemptId;
    el('reviewCatalogStatus').textContent=catalog.length+' Ended Attempt'+(catalog.length===1?'':'s')+' available · catalog '+String(res.catalog_revision||'').slice(0,12);
    if(autoOpen&&sel.value)await reviewOpenSelected();
  }catch(err){reviewMessage('Review catalog: '+err.message,true);}finally{setBusy(false);}
}
async function reviewOpenSelected(){
  if(opening)return;const sel=el('reviewAttemptSelect');if(!sel||!sel.value)return;
  setBusy(true);reviewMessage('<span class="spinner"></span>Opening immutable historical evidence…');
  try{
    if(reviewMeta)await reviewClose(true);
    const res=await call('open',{attempt_id:sel.value});reviewMeta=res.review;snapshot=res.snapshot;
    el('reviewWorkspace').classList.remove('hidden');renderMeta();renderSnapshot();startOrStopTick();clearMsg();
  }catch(err){reviewMessage('Open Review failed: '+err.message,true);}finally{setBusy(false);}
}
function renderMeta(){
  if(!reviewMeta)return;
  el('reviewIdentity').textContent='ATT '+String(reviewMeta.source_attempt_id||'').slice(0,8)+' · '+String(reviewMeta.source_state||'').toUpperCase();
  el('reviewEvidenceId').textContent='Evidence '+String(reviewMeta.evidence_id||'—')+' · revision '+String(reviewMeta.source_revision||'').slice(0,12);
  el('reviewStateEvidence').textContent=(reviewMeta.frame_count||0)+' frames';
  el('reviewActEvidence').textContent=reviewMeta.actuator_source==='derived_tlog_overlay'?'TLOG OVERLAY':reviewMeta.actuator_source==='stored_state'?'STORED STATE':'UNAVAILABLE';
  el('reviewEventCount').textContent=String(reviewMeta.event_count??'—');
  const degraded=Array.isArray(reviewMeta.degraded_channels)?reviewMeta.degraded_channels:[];
  el('reviewGapCount').textContent=degraded.length?String(degraded.length):'0';
  const gn=el('reviewGapNotice');
  if(degraded.length){gn.textContent='Degraded/unavailable channels: '+degraded.join(', ')+'. No values are inferred from control input.';gn.classList.remove('hidden');}else gn.classList.add('hidden');
  const tl=el('reviewTimeline');tl.max=Math.max(1,Math.round(Number(reviewMeta.duration_ns||0)/1e6));
  el('reviewTimeEnd').textContent=fmtTime(reviewMeta.duration_ns);
}
function renderSnapshot(){
  if(!snapshot)return;const f=snapshot.frame||{},p=f.pose||{},a=f.actuator||{};
  el('reviewTimeline').value=Math.max(0,Math.round(Number(snapshot.cursor_ns||0)/1e6));
  el('reviewTimeNow').textContent=fmtTime(snapshot.cursor_ns);
  el('reviewPlayBtn').textContent=snapshot.playback_state==='playing'?'PAUSE':'PLAY';
  el('reviewSpeed').value=String(snapshot.speed||1);
  el('reviewPos').textContent=fmt(p.lat,6)+', '+fmt(p.lon,6);
  el('reviewAlt').textContent=fmt(p.alt_msl,1)+' / '+fmt(p.agl,1)+' m';
  el('reviewHdg').textContent=fmt(p.hdg,1)+'°';el('reviewRoll').textContent=fmt(p.roll,1)+'°';el('reviewPitch').textContent=fmt(p.pitch,1)+'°';
  el('reviewFrame').textContent=String((f.frame_index??0)+1)+' / '+String(reviewMeta&&reviewMeta.frame_count||'—');
  const rec=a.availability==='recorded';
  el('reviewSrv1').textContent=rec?String(a.srv1??'—'):'—';el('reviewSrv2').textContent=rec?String(a.srv2??'—'):'—';el('reviewSrv3').textContent=rec?String(a.srv3??'—'):'—';el('reviewSrv4').textContent=rec?String(a.srv4??'—'):'—';el('reviewThrottle').textContent=rec&&a.throttle!=null?fmt(a.throttle,0)+'%':'—';
  renderSchematic(a);
  if(window.OmniReviewRenderer)window.OmniReviewRenderer.render(f);
  const gaps=Array.isArray(snapshot.gaps)?snapshot.gaps:[];
  if(gaps.length){const gn=el('reviewGapNotice');gn.textContent='Evidence gap at cursor: '+gaps.map(g=>typeof g==='string'?g:JSON.stringify(g)).join(' · ');gn.classList.remove('hidden');}
}
function pwmNorm(v){const n=Number(v);if(!Number.isFinite(n)||n<800||n>2200)return null;return Math.max(-1,Math.min(1,(n-1500)/450));}
function renderSchematic(a){
  if(!a||a.availability!=='recorded')return;
  const ail=pwmNorm(a.srv1),elev=pwmNorm(a.srv2),rud=pwmNorm(a.srv4),srv3=Number(a.srv3);
  if(ail!==null){el('reviewAilL').style.transform='rotate('+(ail*-28)+'deg)';el('reviewAilR').style.transform='rotate('+(ail*28)+'deg)';}
  if(elev!==null&&rud!==null){el('reviewTailL').style.transform='rotate('+((elev*.5+rud*.5)*-30)+'deg)';el('reviewTailR').style.transform='rotate('+((elev*.5-rud*.5)*-30)+'deg)';}
  if(Number.isFinite(srv3)&&srv3>=800&&srv3<=2200){el('reviewProp').style.transform='translateX(-50%) rotate('+((srv3-1000)*0.9)+'deg)';}
}
function revision(){return snapshot&&snapshot.review_revision;}
async function mutate(op,extra={}){
  if(!reviewMeta||!snapshot)return;
  try{snapshot=await call(op,{review_id:reviewMeta.review_id,review_revision:revision(),...extra});renderSnapshot();startOrStopTick();}
  catch(err){
    if(err.code==='STALE_REVIEW_REVISION'){
      try{snapshot=await call('snapshot',{review_id:reviewMeta.review_id});renderSnapshot();reviewMessage('Review cursor resynchronized after a stale request.');}catch(syncErr){reviewMessage(syncErr.message,true);}
    }else reviewMessage(err.message,true);
  }
}
async function reviewTogglePlay(){if(snapshot&&snapshot.playback_state==='playing')await mutate('pause');else await mutate('play',{speed:Number(el('reviewSpeed').value||1)});}
async function reviewSetSpeed(v){await mutate('speed',{speed:Number(v)});}
function reviewPreviewTimeline(ms){el('reviewTimeNow').textContent=fmtTime(Number(ms)*1e6);}
async function reviewSeekTimeline(ms){await mutate('seek',{cursor_ns:Math.round(Number(ms)*1e6)});}
async function reviewStep(direction){await mutate('step',{direction:Number(direction)});}
async function reviewEventJump(direction){await mutate('event-jump',{direction:Number(direction)});}
async function tick(){if(!reviewMeta||!snapshot||snapshot.playback_state!=='playing')return;try{snapshot=await call('snapshot',{review_id:reviewMeta.review_id});renderSnapshot();startOrStopTick();}catch(err){reviewMessage('Review playback: '+err.message,true);stopTick();}}
function startOrStopTick(){if(snapshot&&snapshot.playback_state==='playing'){if(!tickTimer)tickTimer=setInterval(tick,100);}else stopTick();}
function stopTick(){if(tickTimer){clearInterval(tickTimer);tickTimer=null;}}
async function reviewClose(silent=false){
  stopTick();
  if(reviewMeta&&snapshot){try{await call('close',{review_id:reviewMeta.review_id,review_revision:revision()});}catch(err){if(!silent)reviewMessage('Close Review: '+err.message,true);}}
  reviewMeta=null;snapshot=null;el('reviewWorkspace').classList.add('hidden');if(window.OmniReviewRenderer)window.OmniReviewRenderer.reset();
  if(!silent&&typeof show==='function')show(returnPage==='review'?'dashboard':returnPage);
}
function reviewRecenter(){if(window.OmniReviewRenderer)window.OmniReviewRenderer.recenter();}
function reviewFullscreenTarget(){
  return document.querySelector('#reviewWorkspace .reviewViewerCard');
}
async function reviewToggleFullscreen(){
  const target=reviewFullscreenTarget();
  if(!target)return;
  try{
    if(document.fullscreenElement){
      await document.exitFullscreen();
    }else if(target.requestFullscreen){
      await target.requestFullscreen();
    }else if(target.webkitRequestFullscreen){
      target.webkitRequestFullscreen();
    }else{
      reviewMessage('Fullscreen API is not available in this browser.',true);
    }
  }catch(err){
    reviewMessage('Fullscreen failed: '+err.message,true);
  }
}
function reviewSyncFullscreenUi(){
  const btn=el('reviewFullscreenBtn');
  if(btn)btn.textContent=document.fullscreenElement?'EXIT FULLSCREEN':'FULLSCREEN';
  window.setTimeout(()=>{
    try{
      const f=document.getElementById('reviewFeatherFrame');
      if(f&&f.contentWindow)f.contentWindow.dispatchEvent(new Event('resize'));
    }catch(_e){}
    if(window.OmniReviewRenderer&&window.OmniReviewRenderer.recenter)window.OmniReviewRenderer.recenter();
  },120);
}
document.addEventListener('fullscreenchange',reviewSyncFullscreenUi);
document.addEventListener('webkitfullscreenchange',reviewSyncFullscreenUi);
function liveStatus(){
  const box=el('reviewLiveContext');if(!box)return;
  let c=null;try{c=(typeof latest!=='undefined'&&latest&&latest.current)||null;}catch(_e){}
  if(!c){box.innerHTML='<strong>LIVE CONTEXT</strong> · No current live Attempt.';return;}
  const a=c.flight_authority_transfer&&c.flight_authority_transfer.owner;
  box.innerHTML='<strong>LIVE CONTEXT</strong> · ATT '+esc(String(c.attempt_id||'').slice(0,8))+' · '+esc(String(c.state||'UNKNOWN').toUpperCase())+' · authority '+esc(a&&a.kind?String(a.kind).toUpperCase():'—')+'. Historical Review controls cannot modify it.';
}
setInterval(liveStatus,1000);liveStatus();
Object.assign(window,{openReviewBrowser,openEndedReview,reviewLoadCatalog,reviewOpenSelected,reviewTogglePlay,reviewSetSpeed,reviewPreviewTimeline,reviewSeekTimeline,reviewStep,reviewEventJump,reviewClose,reviewRecenter,reviewToggleFullscreen});
})();
