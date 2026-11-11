__COMMON_JS__
const SETTINGS=__SETTINGS__;
let S={}, skew=0, online=false;              // last /api/state, server-browser clock offset
const now=()=>Date.now()/1000+skew;         // "server time"

/* ---------- connection / header ---------- */
function renderStatus(){
  const live=!!S.streaming;
  $('dot').className='dot'+(live?' live':online?' on':'');
  let t;
  if(live)t='Streaming';else if(online)t='Online';
  else t=S.last_seen?'Offline · seen '+fmtWhen(S.last_seen):'Offline';
  $('status').textContent=t;
  const busy=live||lv.state==='starting'||snap.pending;
  $('live').disabled=!online||busy;$('snap').disabled=!online||busy;
  if(!online&&lv.state==='idle')$('vphmsg').textContent='The camera is offline.';
  else if(lv.state==='idle'&&!$('vphmsg').dataset.keep)$('vphmsg').textContent='Start the live view to watch the camera.';
}

/* ---------- arm switch ---------- */
let armPendingUntil=0;
function showArm(a){$('arm').checked=!!a;$('armlbl').textContent=a?'Armed':'Disarmed';$('armbox').className='armbox'+(a?' on':'');}
$('arm').onchange=async()=>{const a=$('arm').checked?1:0;showArm(a);armPendingUntil=Date.now()+6000;
  try{await postJSON('/api/arm',{armed:a});toast(a?'Armed: motion will record clips':'Disarmed',a?'ok':'');}
  catch(e){showArm(!a);toast('Could not change arm state','bad');}};

/* ---------- time-lapse (interval snapshots) ---------- */
let tlPendingUntil=0;
$('tl').onchange=async()=>{const v=+$('tl').value;tlPendingUntil=Date.now()+6000;
  try{await postJSON('/api/timelapse',{seconds:v});
    toast(v?('Time-lapse on · '+$('tl').selectedOptions[0].text.toLowerCase()):'Time-lapse off',v?'ok':'');}
  catch(e){toast('Could not change time-lapse','bad');}};

/* ---------- live view ---------- */
const lv={state:'idle',player:null,t0:0,tries:0,secs:300};   // idle|starting|playing
function destroyPlayer(){if(lv.player){try{lv.player.destroy()}catch(e){}lv.player=null;}}
function attachPlayer(){
  destroyPlayer();
  lv.player=mpegts.createPlayer({type:'mpegts',isLive:true,url:'/live.ts'},
    {enableWorker:false,liveBufferLatencyChasing:true,lazyLoad:false,enableStashBuffer:false,stashInitialSize:128});
  lv.player.attachMediaElement($('vid'));
  lv.player.on(mpegts.Events.ERROR,()=>{
    if(lv.state==='idle')return;
    if(lv.tries++<5)setTimeout(()=>{if(lv.state!=='idle')attachPlayer();},1500);
  });
  lv.player.on(mpegts.Events.MEDIA_INFO,()=>{lv.tries=0;});
  lv.player.load();lv.player.play().catch(()=>{});
}
function lvUI(){
  const st=lv.state;
  $('vid').hidden=st!=='playing';$('vph').hidden=st==='playing';
  $('vov').hidden=st!=='playing';$('vtools').hidden=st!=='playing';
  $('live').hidden=st!=='idle';$('secs').hidden=st!=='idle';$('stop').hidden=st==='idle';
  $('talk').hidden=st!=='playing';if(st!=='playing')talkStop();
  if(st==='starting')$('vph').innerHTML='<div class=spin></div><span>Waking the camera… (a few seconds)</span>';
  else if(st==='idle'&&!$('vphmsg'))$('vph').innerHTML='<span id=vphmsg></span>';
  renderStatus();
}
function endLive(msg,bad){destroyPlayer();lv.state='idle';$('vph').innerHTML='<span id=vphmsg></span>';
  $('vphmsg').textContent=msg||'';$('vphmsg').dataset.keep=msg?'1':'';
  $('vmsg').textContent='';$('vmsg').className='hint';$('lvaside').textContent='';
  if(bad){$('vmsg').textContent=msg;$('vmsg').className='hint bad';}
  lvUI();}
$('live').onclick=async()=>{
  if(!window.mpegts||!mpegts.isSupported()){toast('This browser can’t play the live stream.','bad');return;}
  lv.secs=+$('secs').value;lv.state='starting';lv.t0=Date.now();lv.tries=0;lvUI();
  try{await postJSON('/api/liveview?secs='+lv.secs);}catch(e){endLive('Could not reach the server.',true);return;}
  attachPlayer();
};
$('stop').onclick=()=>{postJSON('/api/stop').catch(()=>{});endLive('Live view stopped.');};
function muteLabel(){$('mute').innerHTML=$('vid').muted?'__I_mute__ Sound off':'__I_sound__ Sound on';}
$('mute').onclick=()=>{const v=$('vid');v.muted=!v.muted;if(talk.on)talk.wasMuted=v.muted;muteLabel();};
$('fs').onclick=()=>{const el=$('stage'),v=$('vid');
  if(document.fullscreenElement)document.exitFullscreen();
  else if(el.requestFullscreen)el.requestFullscreen();else if(v.webkitEnterFullscreen)v.webkitEnterFullscreen();};
function lvTick(){                 // called from poll(): follow the server's stream state
  if(lv.state==='starting'){
    if(S.streaming){lv.state='playing';lvUI();}
    else if(Date.now()-lv.t0>35000){postJSON('/api/stop').catch(()=>{});endLive('The camera didn’t start the stream. Try again.',true);}
  }else if(lv.state==='playing'&&!S.streaming){endLive('Live view ended.');}
}
setInterval(()=>{
  if(lv.state==='playing'&&S.streaming){const left=Math.max(0,S.streaming.since+S.streaming.secs-now());
    $('countdown').textContent='· '+fmtDur(left);$('lvaside').textContent=fmtDur(left)+' left';}
},1000);

/* ---------- two-way talk: hold the button, the camera's speaker plays your mic ----------
   The mic is open only while the button is held. Audio goes to the server as
   raw 16-bit PCM in ~100 ms POSTs (in order); the server encodes it to AAC and
   writes it into the live-view stream. Live sound is muted while talking so the
   camera's mic doesn't feed your voice back to you. */
const talk={on:false,stream:null,ctx:null,ready:null,node:null,src:null,sink:null,sid:'',buf:[],n:0,
  q:Promise.resolve(),wasMuted:true,warned:false,peak:0,maxPeak:0,samples:0};
const TALK_WORKLET="registerProcessor('pcm-tap',class extends AudioWorkletProcessor{process(i){const c=i[0]&&i[0][0];if(c)this.port.postMessage(c.slice(0));return true}})";
function talkUI(){$('talk').classList.toggle('on',talk.on);
  $('talk').innerHTML=talk.on?'__I_mic__ Talking… release to stop':'__I_mic__ Hold to talk';}
function talkHint(msg,bad){$('vmsg').textContent=msg;$('vmsg').className='hint'+(bad?' bad':'');}
function talkSend(path,body){
  talk.q=talk.q.then(()=>fetch(path,{method:'POST',headers:{'Content-Type':'application/octet-stream'},body}))
    .then(r=>{if(!r.ok&&!talk.warned){talk.warned=true;
      toast(r.status===409?'Live view isn’t running.':r.status===503?'Talk needs ffmpeg on the server.':'Could not send audio ('+r.status+').','bad');talkStop();}})
    .catch(()=>{if(!talk.warned){talk.warned=true;toast('Could not send audio to the server.','bad');}});}
function talkFlush(){
  if(!talk.n||!talk.ctx)return;
  const out=new Int16Array(talk.n);let o=0;for(const b of talk.buf){out.set(b,o);o+=b.length;}
  talk.samples+=talk.n;talk.buf=[];talk.n=0;
  talkSend('/api/talk?sid='+talk.sid+'&rate='+talk.ctx.sampleRate,out.buffer);
  if(talk.on){const k=Math.min(10,Math.round(Math.sqrt(talk.peak)*10));     // mic level meter
    talkHint('Mic '+'▮'.repeat(k)+'▯'.repeat(10-k));talk.peak=0;}
}
function talkCtx(){
  // Create and resume the AudioContext inside the button press itself: Safari/iOS
  // keep a context created later (after the mic prompt) suspended -> no audio.
  if(!talk.ctx){const AC=window.AudioContext||window.webkitAudioContext;talk.ctx=new AC();
    talk.ready=talk.ctx.audioWorklet.addModule(URL.createObjectURL(new Blob([TALK_WORKLET],{type:'application/javascript'})));}
  if(talk.ctx.state!=='running')talk.ctx.resume().catch(()=>{});
  return talk.ready;
}
function talkStart(){
  if(talk.on||lv.state!=='playing')return;
  if(!window.isSecureContext||!navigator.mediaDevices||!window.AudioWorkletNode){
    toast('Talking needs the dashboard opened over HTTPS in a recent browser.','bad');return;}
  let ready;
  try{ready=talkCtx();}catch(e){toast('This browser can’t capture audio.','bad');return;}
  talk.on=true;talk.warned=false;talk.samples=0;talk.peak=0;talk.maxPeak=0;
  talk.sid=Math.random().toString(36).slice(2,12);talkUI();talkHint('Opening the microphone…');
  const v=$('vid');talk.wasMuted=v.muted;v.muted=true;muteLabel();
  talkOpen(ready);
}
async function talkOpen(ready){
  try{
    const stream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true,autoGainControl:true}});
    if(!talk.on){stream.getTracks().forEach(t=>t.stop());return;}     // released while asking
    talk.stream=stream;
    await ready;
    if(talk.ctx.state!=='running')await talk.ctx.resume();
    if(!talk.on){talkStop();return;}
    const ctx=talk.ctx,chunk=Math.round(ctx.sampleRate/10);           // ~100 ms per POST
    talk.src=ctx.createMediaStreamSource(stream);
    talk.node=new AudioWorkletNode(ctx,'pcm-tap');
    talk.node.port.onmessage=e=>{
      if(!talk.on)return;
      const f=e.data,s=new Int16Array(f.length);
      for(let i=0;i<f.length;i++){const x=Math.max(-1,Math.min(1,f[i])),a=x<0?-x:x;
        if(a>talk.peak)talk.peak=a;if(a>talk.maxPeak)talk.maxPeak=a;s[i]=x<0?x*0x8000:x*0x7fff;}
      talk.buf.push(s);talk.n+=s.length;if(talk.n>=chunk)talkFlush();
    };
    // The audio graph only runs nodes that lead to the output, so route the tap
    // there through a muted gain (silent: no local echo).
    talk.sink=ctx.createGain();talk.sink.gain.value=0;
    talk.src.connect(talk.node);talk.node.connect(talk.sink);talk.sink.connect(ctx.destination);
  }catch(e){
    toast(e&&e.name==='NotAllowedError'?'Microphone access was blocked.':'Could not open the microphone'+(e&&e.name?' ('+e.name+')':'')+'.','bad');
    talkStop();
  }
}
function talkStop(){
  if(!talk.on&&!talk.stream)return;
  const was=talk.on;talk.on=false;
  talkFlush();
  for(const k of ['node','src','sink'])if(talk[k]){try{talk[k].disconnect()}catch(e){}talk[k]=null;}
  if(talk.stream){talk.stream.getTracks().forEach(t=>t.stop());talk.stream=null;}
  if(was){
    const secs=talk.ctx?talk.samples/talk.ctx.sampleRate:0,peak=talk.maxPeak,sid=talk.sid;
    $('vid').muted=talk.wasMuted;muteLabel();
    talk.q=talk.q.then(()=>fetch('/api/talk?sid='+sid+'&end=1',{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:new ArrayBuffer(0)}))
      .then(r=>r.json()).catch(()=>({}))
      .then(j=>{
        if(talk.on||talk.warned)return;
        if(secs<0.05)talkHint('No audio came from your microphone. Check the browser’s mic permission.',true);
        else if(peak<0.003)talkHint('Your microphone is silent. Check the input device or whether it is muted.',true);
        else talkHint('Sent '+secs.toFixed(1)+' s to the camera'+(j.rx_secs!=null?' (server got '+j.rx_secs.toFixed(1)+' s)':'')+'.');
      });
  }
  talkUI();
}
$('talk').addEventListener('pointerdown',e=>{if(e.button)return;e.preventDefault();
  try{$('talk').setPointerCapture(e.pointerId)}catch(_){}talkStart();});
['pointerup','pointercancel','lostpointercapture'].forEach(ev=>$('talk').addEventListener(ev,talkStop));
$('talk').addEventListener('contextmenu',e=>e.preventDefault());
$('talk').addEventListener('keydown',e=>{if((e.key===' '||e.key==='Enter')&&!e.repeat){e.preventDefault();talkStart();}});
$('talk').addEventListener('keyup',e=>{if(e.key===' '||e.key==='Enter'){e.preventDefault();talkStop();}});
window.addEventListener('blur',()=>{if(talk.stream)talkStop();});   // not while the mic prompt is up
document.addEventListener('visibilitychange',()=>{if(document.hidden)talkStop();});

/* ---------- snapshot ---------- */
const snap={pending:false,prev:null,t0:0};
function showSnapshot(ls){
  if(!ls){$('shot').hidden=true;$('sph').hidden=false;$('shotwhen').textContent='';return;}
  if($('shot').dataset.name!==ls.name){$('shot').dataset.name=ls.name;$('shot').src=clipUrl(ls.name);}
  $('shot').hidden=false;$('sph').hidden=true;
  $('shotwhen').textContent=fmtWhen(ls.ts);$('shotlink').href='/gallery#'+encodeURIComponent(ls.name);
}
$('snap').onclick=async()=>{
  snap.pending=true;snap.prev=(S.latest_snapshot||{}).name||null;snap.t0=Date.now();
  $('snap').innerHTML='<span class=spin style="width:16px;height:16px;border-width:2px"></span> Capturing…';
  $('smsg').textContent='';$('smsg').className='hint';renderStatus();
  try{await postJSON('/api/snapshot');}catch(e){snapDone('Could not reach the server.',true);}
};
function snapDone(msg,bad){snap.pending=false;$('snap').innerHTML='__I_cam__ Take snapshot';
  $('smsg').textContent=bad?msg:'';$('smsg').className='hint'+(bad?' bad':'');if(!bad&&msg)toast(msg,'ok');renderStatus();}
function snapTick(){
  if(!snap.pending)return;
  const ls=S.latest_snapshot;
  if(ls&&ls.name!==snap.prev){snapDone('Snapshot saved');loadRecent();}
  else if(Date.now()-snap.t0>45000)snapDone('No snapshot received — the camera may be busy or offline.',true);
}

/* ---------- recent activity ---------- */
let recentSig='';
async function loadRecent(){
  try{const r=await api('/api/media?limit=6');const sig=r.items.map(i=>i.name).join();
    if(sig===recentSig)return;recentSig=sig;
    $('recent').innerHTML=r.items.length?r.items.map(it=>{
      const label=it.motion?'Motion':it.kind==='clip'?'Clip':'Snapshot';
      return `<a href="/gallery#${encodeURIComponent(it.name)}" title="${esc(label+' · '+fmtWhen(it.ts))}">`+
        (it.thumb?`<img loading=lazy src="${clipUrl(it.thumb)}" alt="">`:'')+
        `<span class=tag>${it.kind==='clip'?'▶ ':''}${fmtTime(it.ts)}</span></a>`;}).join('')
      :'<div class=empty>Nothing recorded yet.</div>';
  }catch(e){}
}

/* ---------- settings ---------- */
let saved={}, applyWaitTs=0;
function ctlHTML(s,v){
  if(s.control==='toggle')return `<span class=sw><input type=checkbox id=set_${s.key} ${v?'checked':''}><span class=sl></span></span>`;
  if(s.control==='select')return `<select id=set_${s.key}>`+Object.entries(s.options).map(([k,l])=>`<option value=${k} ${+k===+v?'selected':''}>${esc(l)}</option>`).join('')+`</select>`;
  return `<span class=rng><input type=range id=set_${s.key} min=${s.options.min} max=${s.options.max} value=${v??s.options.min}><output>${v??''}</output></span>`;
}
function readForm(){const o={};SETTINGS.forEach(s=>{const el=$('set_'+s.key);if(!el)return;
  o[s.key]=s.control==='toggle'?(el.checked?1:0):parseInt(el.value);});return o;}
function dirty(){const f=readForm();return SETTINGS.some(s=>saved[s.key]!==undefined&&+f[s.key]!==+saved[s.key]);}
function refreshDirty(){const d=dirty();$('save').disabled=!d;$('revert').hidden=!d;if(d&&!applyWaitTs){$('setmsg').textContent='Unsaved changes';$('setmsg').className='setmsg';}
  else if(!d&&$('setmsg').textContent==='Unsaved changes')$('setmsg').textContent='';}
function renderSettings(cur){
  saved=cur;
  $('settings').innerHTML=SETTINGS.map(s=>`<div class=set><label for=set_${s.key}>${esc(s.label)}</label>${ctlHTML(s,cur[s.key])}</div>`).join('');
  $('settings').querySelectorAll('input,select').forEach(el=>{el.oninput=el.onchange=()=>{
    if(el.type==='range')el.nextElementSibling.value=el.value;refreshDirty();};});
  refreshDirty();
}
async function loadSettings(){try{const r=await api('/api/settings');renderSettings(r.current||{});}catch(e){}}
$('revert').onclick=()=>renderSettings(saved);
$('save').onclick=async()=>{
  const body=readForm();$('save').disabled=true;
  try{const r=await postJSON('/api/settings',body);
    saved=Object.assign({},saved,body);applyWaitTs=r.ts||now();refreshDirty();
    $('setmsg').textContent=online?'Saved · sending to camera…':'Saved · will apply when the camera is back online';$('setmsg').className='setmsg';
  }catch(e){toast('Saving settings failed','bad');refreshDirty();}
};
function settingsTick(){
  if(applyWaitTs&&S.settings_pushed&&S.settings_pushed>applyWaitTs){applyWaitTs=0;
    $('setmsg').textContent='Applied to camera ✓';$('setmsg').className='setmsg ok';
    setTimeout(()=>{if($('setmsg').textContent==='Applied to camera ✓')$('setmsg').textContent='';},5000);}
}

/* ---------- polling ---------- */
let lastMediaTs=null;
async function poll(){
  try{S=await api('/api/state');skew=(S.now||Date.now()/1000)-Date.now()/1000;
    online=!!S.last_seen&&now()-S.last_seen<30;
    if(Date.now()>armPendingUntil&&S.armed!==undefined)showArm(S.armed);
    if(Date.now()>tlPendingUntil&&S.snapshot_interval!==undefined)$('tl').value=String(S.snapshot_interval||0);
    showSnapshot(S.latest_snapshot);lvTick();snapTick();settingsTick();
    const lm=S.last_media&&S.last_media.ts;if(lm!==lastMediaTs){lastMediaTs=lm;loadRecent();}
  }catch(e){online=false;S={};$('status').textContent='Server unreachable';}
  renderStatus();
}
lvUI();loadSettings();poll();setInterval(poll,2000);setInterval(loadRecent,30000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)poll();});
