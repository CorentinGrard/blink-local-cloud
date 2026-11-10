#!/usr/bin/env python3
"""Web UI for the Blink local cloud (local_server.py).

HTTP server (default :8090) in a background thread: shows the camera, takes
snapshots, plays the liveview in-browser (mpegts.js, served locally), edits
camera settings and browses/deletes the saved media (/gallery). Commands use the
same files local_server already watches (media_request.json / media_stop /
settings.json), so nothing here imports local_server (no import cycle). Live
MPEG-TS is fanned out from LiveHub.
"""
import os, re, json, time, glob, shutil, subprocess, threading, queue as _queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
RETENTION_DAYS = float(os.environ.get("BLINK_RETENTION_DAYS", "7") or "0")

# User-facing settings: key -> (label, control, options). Written as ints to
# settings.json; local_server pushes them to the camera on its next heartbeat.
# NB: motion detection is driven by the Arm/Disarm switch (it sets both the
# "armed" and "motion" bytes together — the firmware needs both to record), so it
# is not a settings row here.
SETTINGS_UI = [
    ("nv",           "Night vision",     "select", {0: "Off", 1: "On", 2: "Auto"}),
    ("ir",           "Infrared light",   "select", {1: "Low", 4: "Medium", 7: "High"}),
    ("quality",      "Video quality",    "select", {1: "Low", 2: "Normal", 3: "High"}),
    ("flip",         "Flip image",       "toggle", None),
    ("audio_stream", "Record audio",     "toggle", None),
    ("clip_len",     "Clip length (s)",  "range",  {"min": 5, "max": 30}),
    ("sensitivity",  "Motion sensitivity", "range", {"min": 2, "max": 100}),
    ("retrigger",    "Re-arm delay (s)", "range",  {"min": 10, "max": 60}),
]

# Media files that belong to one capture share a base name; deleting an item
# removes all of these siblings.
SIBLING_EXTS = (".jpg", ".mp4", ".ts", ".thumb.jpg", ".bin")
NAME_RE = re.compile(r"[\w-][\w.-]*")

# ---------------------------------------------------------------------------
# Shared look & feel
# ---------------------------------------------------------------------------
CSS = r"""
:root{--bg:#0e1013;--card:#171a20;--card2:#1e222a;--fg:#eceef1;--mut:#9aa1ac;--acc:#4da3ff;--acc-fg:#04121f;
--ok:#36c76f;--warn:#f5a623;--bad:#ef5a5a;--off:#6b7280;--line:#282c34;--shadow:0 1px 3px rgba(0,0,0,.4);color-scheme:dark}
*{box-sizing:border-box}
[hidden]{display:none!important}
.ic{width:18px;height:18px;flex:none;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
a{color:var(--acc)}
header{position:sticky;top:0;z-index:10;background:rgba(14,16,19,.9);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);border-bottom:1px solid var(--line)}
.hbar{max-width:1100px;margin:0 auto;padding:10px 16px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:9px;font-weight:650;font-size:17px;white-space:nowrap}
nav{display:flex;gap:4px;background:var(--card);border:1px solid var(--line);border-radius:999px;padding:3px}
nav a{color:var(--mut);text-decoration:none;font-size:14px;font-weight:600;padding:5px 14px;border-radius:999px}
nav a.on{background:var(--card2);color:var(--fg)}
.hright{margin-left:auto;display:flex;align-items:center;gap:14px}
.pill{display:inline-flex;align-items:center;gap:7px;font-size:13px;color:var(--mut);white-space:nowrap}
.dot{width:9px;height:9px;border-radius:50%;background:var(--off);box-shadow:0 0 0 3px rgba(107,114,128,.2);flex:none}
.dot.on{background:var(--ok);box-shadow:0 0 0 3px rgba(54,199,111,.2)}
.dot.live{background:var(--bad);box-shadow:0 0 0 3px rgba(239,90,90,.25);animation:pulse 1.4s infinite}
@keyframes pulse{50%{opacity:.45}}
main{max-width:1100px;margin:0 auto;padding:18px 16px 90px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px;box-shadow:var(--shadow);min-width:0}
.card h2{font-size:15px;margin:0 0 12px;font-weight:650;display:flex;align-items:center;gap:8px}
.card h2 .aside{margin-left:auto;font-size:13px;font-weight:500;color:var(--mut)}
button,.btn{font:inherit;font-size:15px;font-weight:600;border:0;border-radius:10px;padding:10px 16px;cursor:pointer;background:var(--acc);color:var(--acc-fg);text-decoration:none;display:inline-flex;align-items:center;gap:6px;justify-content:center;min-height:42px}
button.sec,.btn.sec{background:var(--card2);color:var(--fg);border:1px solid var(--line)}
button.danger{background:var(--bad);color:#fff}
button.ghost{background:none;color:var(--acc);padding:6px 8px;min-height:0}
button:active{transform:translateY(1px)}button:disabled{opacity:.45;cursor:default;transform:none}
button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
.sw{position:relative;width:46px;height:26px;flex:none;display:inline-block}
.sw input{position:absolute;opacity:0;width:100%;height:100%;margin:0;cursor:pointer;z-index:1}
.sl{position:absolute;inset:0;background:#39404c;border-radius:26px;transition:.15s}
.sl:before{content:"";position:absolute;height:20px;width:20px;left:3px;top:3px;background:#fff;border-radius:50%;transition:.15s}
.sw input:checked+.sl{background:var(--acc)}.sw input:checked+.sl:before{transform:translateX(20px)}
.sw input:focus-visible+.sl{outline:2px solid var(--acc);outline-offset:2px}
#toasts{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);display:flex;flex-direction:column;gap:8px;z-index:60;width:min(92vw,420px);pointer-events:none}
.toast{background:#262b35;border:1px solid var(--line);border-left:4px solid var(--acc);border-radius:10px;padding:10px 14px;font-size:14px;box-shadow:0 6px 24px rgba(0,0,0,.45);animation:tin .18s ease-out}
.toast.ok{border-left-color:var(--ok)}.toast.bad{border-left-color:var(--bad)}
@keyframes tin{from{opacity:0;transform:translateY(8px)}}
dialog{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:14px;padding:20px;max-width:min(92vw,400px);box-shadow:0 10px 40px rgba(0,0,0,.6)}
dialog::backdrop{background:rgba(0,0,0,.6)}
dialog p{margin:0 0 18px}dialog .row{display:flex;gap:10px;justify-content:flex-end}
.mut{color:var(--mut)}
@media (max-width:560px){.hbar{gap:8px;padding:8px 12px}.brand span:not(.dot){display:none}nav a{padding:5px 11px}
.hright{gap:8px}.pill{display:none}main{padding:14px 12px 90px}.card{padding:14px;border-radius:14px}}
"""

COMMON_JS = r"""
const $=id=>document.getElementById(id);
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const clipUrl=n=>'/clips/'+encodeURIComponent(n);
const fmtSize=b=>b>=1048576?(b/1048576).toFixed(1)+' MB':Math.max(1,Math.round(b/1024))+' KB';
const fmtDur=s=>{s=Math.round(s);return Math.floor(s/60)+':'+String(s%60).padStart(2,'0');};
const fmtTime=t=>new Date(t*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
function fmtWhen(t){const d=new Date(t*1000),n=new Date(),y=new Date(n-864e5);
  const day=d.toDateString()===n.toDateString()?'Today':d.toDateString()===y.toDateString()?'Yesterday':
    d.toLocaleDateString([],{weekday:'short',day:'numeric',month:'short'});
  return day+' '+fmtTime(t);}
function toast(msg,kind,ms){const t=document.createElement('div');t.className='toast '+(kind||'');t.textContent=msg;
  $('toasts').appendChild(t);setTimeout(()=>t.remove(),ms||4000);}
async function api(path,opts){const r=await fetch(path,opts);if(!r.ok)throw new Error(r.status);
  const ct=r.headers.get('content-type')||'';return ct.includes('json')?r.json():r.text();}
const postJSON=(p,b)=>api(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});
function confirmBox(msg,okLabel){return new Promise(res=>{const d=$('confirm');$('cmsg').textContent=msg;
  $('cok').textContent=okLabel||'Delete';d.returnValue='';d.onclose=()=>res(d.returnValue==='ok');d.showModal();});}
"""

CONFIRM_HTML = """<dialog id=confirm><form method=dialog><p id=cmsg></p><div class=row>
<button class=sec value=cancel>Cancel</button><button class=danger id=cok value=ok>Delete</button></div></form></dialog>
<div id=toasts aria-live=polite></div>"""


def header(active):
    def a(href, label, key):
        return f'<a href="{href}"{" class=on aria-current=page" if key == active else ""}>{label}</a>'
    return ("<header><div class=hbar><div class=brand><span class=dot id=dot></span><span>Blink</span></div>"
            f"<nav>{a('/', 'Camera', 'cam')}{a('/gallery', 'Gallery', 'gal')}</nav>"
            "<div class=hright>__HRIGHT__</div></div></header>")


# ---------------------------------------------------------------------------
# Camera page
# ---------------------------------------------------------------------------
PAGE = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name=theme-color content="#0e1013">
<title>Blink Camera</title>
<script src="/static/mpegts.js"></script>
<style>__CSS__
.grid{display:grid;grid-template-columns:minmax(0,3fr) minmax(0,2fr);gap:16px;align-items:start}
.col{display:flex;flex-direction:column;gap:16px;min-width:0}
@media (max-width:860px){.grid{grid-template-columns:minmax(0,1fr)}}
.stage{position:relative;aspect-ratio:16/9;background:#000;border-radius:12px;overflow:hidden}
.stage video,.stage img{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;background:#000}
.ph{position:absolute;inset:0;display:flex;flex-direction:column;gap:12px;align-items:center;justify-content:center;color:var(--mut);font-size:14px;text-align:center;padding:16px}
.ph img{opacity:.35}
.overlay{position:absolute;left:10px;top:10px;display:flex;gap:6px;pointer-events:none}
.tag{background:rgba(0,0,0,.65);color:#fff;font-size:12px;font-weight:650;padding:3px 9px;border-radius:999px;display:inline-flex;align-items:center;gap:6px}
.tag .dot{width:7px;height:7px;box-shadow:none}
.vtools{position:absolute;right:10px;top:10px;display:flex;gap:6px}
.vtools button{background:rgba(0,0,0,.6);color:#fff;min-height:34px;padding:6px 10px;font-size:13px;border-radius:8px}
.spin{width:28px;height:28px;border:3px solid rgba(255,255,255,.15);border-top-color:var(--acc);border-radius:50%;animation:sp .8s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
.btns{display:flex;gap:10px;flex-wrap:wrap;margin-top:12px;align-items:center}
.btns select{min-height:42px}
.hint{color:var(--mut);font-size:13px;margin-top:8px;min-height:20px}
.hint.bad{color:var(--bad)}
.shot{cursor:zoom-in;display:block}
.recent{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}
.recent a{position:relative;display:block;aspect-ratio:16/9;border-radius:8px;overflow:hidden;background:#000;border:1px solid var(--line)}
.recent a:hover{border-color:var(--acc)}
.recent img{width:100%;height:100%;object-fit:cover;display:block}
.recent .tag{position:absolute;left:5px;bottom:5px;font-size:11px;padding:1px 7px}
.recent .empty{grid-column:1/-1;color:var(--mut);font-size:14px;padding:8px 0}
.set{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:10px 0;border-bottom:1px solid var(--line)}
.set:last-child{border-bottom:0}.set label{font-weight:500;font-size:15px}
select{font:inherit;font-size:15px;background:#0d0f13;color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:7px 10px}
input[type=range]{width:min(150px,38vw);accent-color:var(--acc)}
.rng{display:flex;align-items:center;gap:8px}.rng output{color:var(--mut);width:30px;text-align:right;font-variant-numeric:tabular-nums;font-size:14px}
.armbox{display:flex;align-items:center;gap:9px}
.armbox .lbl{font-size:13px;font-weight:650;color:var(--mut)}
.armbox.on .lbl{color:var(--warn)}
.armbox.on .sl{background:var(--warn)!important}
.setmsg{font-size:13px;color:var(--mut)}.setmsg.ok{color:var(--ok)}
</style></head><body>
__HEADER__
<main><div class=grid>
 <div class=col>
  <section class=card aria-labelledby=hlive>
   <h2 id=hlive>Live view <span class=aside id=lvaside></span></h2>
   <div class=stage id=stage>
    <video id=vid autoplay muted playsinline hidden></video>
    <div class=ph id=vph><span id=vphmsg>Start the live view to watch the camera.</span></div>
    <div class=overlay id=vov hidden><span class=tag><span class="dot live"></span>LIVE <span id=countdown></span></span></div>
    <div class=vtools id=vtools hidden><button id=mute title="Toggle sound">__I_mute__ Sound off</button><button id=fs title="Fullscreen" aria-label=Fullscreen>__I_fs__</button></div>
   </div>
   <div class=btns>
    <button id=live>__I_play__ Start live</button>
    <button class=sec id=stop hidden>__I_stop__ Stop</button>
    <select id=secs aria-label="Live view duration"><option value=60>1 min</option><option value=180>3 min</option><option value=300 selected>5 min</option><option value=600>10 min</option></select>
   </div>
   <div class=hint id=vmsg></div>
  </section>
  <section class=card aria-labelledby=hrecent>
   <h2 id=hrecent>Recent activity <a class=aside href="/gallery">See all →</a></h2>
   <div class=recent id=recent><div class=empty>Loading…</div></div>
  </section>
 </div>
 <div class=col>
  <section class=card aria-labelledby=hsnap>
   <h2 id=hsnap>Latest snapshot <span class=aside id=shotwhen></span></h2>
   <a class="stage shot" id=shotlink href="/gallery" title="Open in gallery">
    <img id=shot alt="Latest snapshot" hidden><div class=ph id=sph>No snapshot yet</div>
   </a>
   <div class=btns><button id=snap>__I_cam__ Take snapshot</button></div>
   <div class=hint id=smsg></div>
  </section>
  <section class=card aria-labelledby=hset>
   <h2 id=hset>Settings</h2>
   <div id=settings></div>
   <div class=btns><button id=save disabled>Save</button><button class=sec id=revert hidden>Revert</button><span class=setmsg id=setmsg></span></div>
  </section>
  <section class=card aria-labelledby=htl>
   <h2 id=htl>Time-lapse</h2>
   <div class=set><label for=tl>Auto snapshot</label>
    <select id=tl aria-label="Automatic snapshot interval">
     <option value=0>Off</option>
     <option value=60>Every 1 min</option>
     <option value=300>Every 5 min</option>
     <option value=900>Every 15 min</option>
     <option value=1800>Every 30 min</option>
     <option value=3600>Every hour</option>
    </select></div>
   <div class=hint id=tlmsg>Automatically saves a snapshot on a fixed interval. Off by default.</div>
  </section>
 </div>
</div></main>
__CONFIRM__
<script>__COMMON_JS__
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
$('mute').onclick=()=>{const v=$('vid');v.muted=!v.muted;$('mute').innerHTML=v.muted?'__I_mute__ Sound off':'__I_sound__ Sound on';};
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
</script></body></html>"""

ARM_HTML = ('<span class=pill><span id=status>…</span></span>'
            '<label class=armbox id=armbox title="Arm: record a clip when motion is detected">'
            '<span class=lbl id=armlbl>…</span><span class=sw><input type=checkbox id=arm aria-label="Arm motion recording">'
            '<span class=sl></span></span></label>')

# ---------------------------------------------------------------------------
# Gallery page
# ---------------------------------------------------------------------------
GALLERY = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name=theme-color content="#0e1013">
<title>Blink Gallery</title>
<script src="/static/mpegts.js"></script>
<style>__CSS__
.toolbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px}
.tabs{display:flex;gap:6px;flex-wrap:wrap}
.tabs button{min-height:34px;padding:5px 13px;font-size:14px;border-radius:999px;background:var(--card);color:var(--fg);border:1px solid var(--line)}
.tabs button.on{background:var(--acc);color:var(--acc-fg);border-color:var(--acc)}
.tabs .n{opacity:.65;font-weight:500;margin-left:2px}
.toolbar .right{margin-left:auto;display:flex;gap:10px;align-items:center}
@media (max-width:560px){.toolbar{flex-wrap:nowrap}.tabs{flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none;flex:1;min-width:0}
.tabs::-webkit-scrollbar{display:none}.tabs button{flex:none}.summary{display:none}.right button{min-height:34px;padding:5px 12px;font-size:14px}}
.summary{font-size:13px;color:var(--mut)}
.dayh{display:flex;align-items:baseline;gap:10px;margin:24px 0 10px}
.dayh h2{font-size:15px;margin:0;font-weight:650}
.dayh .mut{font-size:13px}
.dayh button{margin-left:auto}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:12px}
@media (max-width:560px){.grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}}
.it{position:relative;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;cursor:pointer;padding:0;color:inherit;font:inherit;text-align:left;display:block;width:100%;min-height:0}
.it:hover{border-color:#3a4150}
.it.sel{border-color:var(--acc);box-shadow:0 0 0 2px var(--acc)}
.th{position:relative;aspect-ratio:16/9;background:#000}
.th img{width:100%;height:100%;object-fit:cover;display:block}
.badge{position:absolute;left:7px;top:7px;background:rgba(0,0,0,.68);color:#fff;font-size:11.5px;font-weight:650;padding:2px 8px;border-radius:999px}
.badge.motion{background:rgba(245,166,35,.92);color:#1c1200}
.dur{position:absolute;right:7px;bottom:7px;background:rgba(0,0,0,.72);color:#fff;font-size:12px;font-weight:600;padding:1px 6px;border-radius:5px;font-variant-numeric:tabular-nums}
.play{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:30px;color:#fff;text-shadow:0 2px 10px rgba(0,0,0,.8);opacity:.85}
.chk{position:absolute;right:7px;top:7px;width:24px;height:24px;border-radius:50%;border:2px solid #fff;background:rgba(0,0,0,.45);display:none;align-items:center;justify-content:center;font-size:14px;color:#fff}
.selecting .chk{display:flex;color:transparent}
body.selmode #toasts{bottom:84px}.it.sel .chk{background:var(--acc);border-color:var(--acc);color:var(--acc-fg)}
.meta{padding:7px 10px;font-size:13px;color:var(--mut);display:flex;justify-content:space-between;gap:6px}
.meta b{color:var(--fg);font-weight:600}
.empty{color:var(--mut);text-align:center;padding:70px 16px}
.selbar{position:fixed;left:50%;bottom:16px;transform:translateX(-50%);background:#232833;border:1px solid var(--line);border-radius:14px;padding:8px 8px 8px 16px;display:flex;align-items:center;gap:10px;box-shadow:0 8px 30px rgba(0,0,0,.5);z-index:15;width:max-content;max-width:calc(100vw - 24px)}
.selbar[hidden]{display:none}
#lb{position:fixed;inset:0;background:rgba(5,6,8,.96);display:none;flex-direction:column;z-index:30}
#lb.on{display:flex}
#lbtop{display:flex;align-items:center;gap:8px;padding:10px 12px;color:var(--mut);font-size:14px}
#lbtop .t{color:var(--fg);font-weight:600;margin-right:auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#lbtop button,#lbtop a{background:rgba(255,255,255,.08);color:var(--fg);min-height:38px;padding:7px 12px;font-size:14px;border:0;border-radius:9px}
#lbm{flex:1;min-height:0;display:flex;align-items:center;justify-content:center;padding:0 8px;position:relative;touch-action:pan-y}
#lbm img,#lbm video{max-width:100%;max-height:100%;border-radius:8px;background:#000}
.nav{position:absolute;top:50%;transform:translateY(-50%);background:rgba(255,255,255,.1);color:#fff;width:44px;height:64px;border-radius:10px;font-size:22px;padding:0;z-index:2}
.nav.p{left:10px}.nav.n{right:10px}
#lbcount{text-align:center;color:var(--mut);font-size:13px;padding:8px}
@media (max-width:560px){.nav{display:none}#lbtop .lbl{display:none}}
</style></head><body>
__HEADER__
<main>
 <div class=toolbar>
  <div class=tabs role=tablist id=tabs></div>
  <div class=right><span class=summary id=summary></span><button class=sec id=selbtn>Select</button></div>
 </div>
 <div id=list><div class=empty>Loading…</div></div>
</main>
<div class=selbar id=selbar hidden><span id=selcount>0 selected</span>
 <button class=sec id=selall>Select all</button><button class=danger id=seldel disabled>Delete</button><button class=sec id=selcancel>Done</button></div>
<div id=lb role=dialog aria-modal=true aria-label="Media viewer">
 <div id=lbtop><span class=t id=lbt></span>
  <a id=dl class=btn download title=Download>__I_dl__<span class=lbl>Download</span></a>
  <button id=lbdel title=Delete>__I_trash__<span class=lbl>Delete</span></button>
  <button id=cls title="Close (Esc)" aria-label=Close>__I_x__</button></div>
 <div id=lbm><button class="nav p" id=prev aria-label=Previous>‹</button><div id=lbc style="display:contents"></div><button class="nav n" id=next aria-label=Next>›</button></div>
 <div id=lbcount></div>
</div>
__CONFIRM__
<script>__COMMON_JS__
const RETENTION=__RETENTION__;
let items=[],shown=[],filt='all',cur=-1,player=null,selecting=false,sel=new Set(),sig='';
const FILTERS=[['all','All'],['snapshot','Snapshots'],['clip','Clips'],['motion','Motion']];
const match=(it,f)=>f==='all'||(f==='motion'?it.motion:it.kind===f);
const label=it=>it.motion?'Motion':it.kind==='clip'?'Clip':'Snapshot';
const dayKey=t=>new Date(t*1000).toDateString();
function dayTitle(t){const d=new Date(t*1000),n=new Date(),y=new Date(n-864e5);
  if(d.toDateString()===n.toDateString())return 'Today';if(d.toDateString()===y.toDateString())return 'Yesterday';
  return d.toLocaleDateString([],{weekday:'long',day:'numeric',month:'long'});}

function renderTabs(){
  $('tabs').innerHTML=FILTERS.map(([k,l])=>{const n=items.filter(i=>match(i,k)).length;
    return `<button role=tab aria-selected=${k===filt} data-f=${k} class="${k===filt?'on':''}">${l} <span class=n>${n}</span></button>`;}).join('');
  $('tabs').querySelectorAll('button').forEach(b=>b.onclick=()=>{filt=b.dataset.f;render();});
}
function render(){
  renderTabs();
  shown=items.filter(i=>match(i,filt));
  const total=items.reduce((a,i)=>a+i.bytes,0);
  $('summary').textContent=items.length?`${items.length} items · ${fmtSize(total)}`+(RETENTION>0?` · kept ${RETENTION} days`:''):'';
  $('list').className=selecting?'selecting':'';
  if(!shown.length){$('list').innerHTML=`<div class=empty>${items.length?'Nothing in this category.':'Nothing saved yet. Snapshots, live views and motion clips will show up here.'}</div>`;updSel();return;}
  const days=[];shown.forEach((it,i)=>{const k=dayKey(it.ts);if(!days.length||days[days.length-1].k!==k)days.push({k,t:it.ts,idx:[]});days[days.length-1].idx.push(i);});
  $('list').innerHTML=days.map(d=>{
    const sz=d.idx.reduce((a,i)=>a+shown[i].bytes,0);
    return `<div class=dayh><h2>${dayTitle(d.t)}</h2><span class=mut>${d.idx.length} item${d.idx.length>1?'s':''} · ${fmtSize(sz)}</span>`+
      (selecting?`<button class=ghost data-day="${esc(d.k)}">Select day</button>`:'')+`</div><div class=grid>`+
      d.idx.map(i=>{const it=shown[i];
        return `<button class="it${sel.has(it.name)?' sel':''}" data-i=${i} aria-label="${esc(label(it)+' '+fmtWhen(it.ts))}"><div class=th>`+
        (it.thumb?`<img loading=lazy src="${clipUrl(it.thumb)}" alt="">`:'')+
        `<span class="badge${it.motion?' motion':''}">${label(it)}</span>`+
        (it.kind==='clip'?'<span class=play>▶</span>':'')+
        (it.duration?`<span class=dur>${fmtDur(it.duration)}</span>`:'')+
        `<span class=chk>✓</span></div><div class=meta><b>${fmtTime(it.ts)}</b><span>${fmtSize(it.bytes)}</span></div></button>`;}).join('')+`</div>`;
  }).join('');
  $('list').querySelectorAll('.it').forEach(b=>b.onclick=()=>{const it=shown[+b.dataset.i];
    if(selecting){sel.has(it.name)?sel.delete(it.name):sel.add(it.name);b.classList.toggle('sel');updSel();}else openAt(+b.dataset.i);});
  $('list').querySelectorAll('[data-day]').forEach(b=>b.onclick=()=>{
    const names=shown.filter(i=>dayKey(i.ts)===b.dataset.day).map(i=>i.name);
    const all=names.every(n=>sel.has(n));names.forEach(n=>all?sel.delete(n):sel.add(n));render();});
  updSel();
}

/* ---------- selection & delete ---------- */
function updSel(){$('selbar').hidden=!selecting;document.body.classList.toggle('selmode',selecting);$('selcount').textContent=sel.size+' selected';$('seldel').disabled=!sel.size;
  $('selbtn').textContent=selecting?'Done':'Select';}
function setSelecting(on){selecting=on;sel.clear();render();}
$('selbtn').onclick=()=>setSelecting(!selecting);$('selcancel').onclick=()=>setSelecting(false);
$('selall').onclick=()=>{const all=shown.every(i=>sel.has(i.name));shown.forEach(i=>all?sel.delete(i.name):sel.add(i.name));render();};
async function del(names){
  try{const r=await postJSON('/api/delete',{names});
    const gone=new Set(r.deleted||[]);items=items.filter(i=>!gone.has(i.name));sig='';
    toast(`Deleted ${gone.size} item${gone.size===1?'':'s'}`,'ok');return gone;
  }catch(e){toast('Delete failed','bad');return new Set();}
}
$('seldel').onclick=async()=>{const n=sel.size;
  if(!await confirmBox(`Delete ${n} item${n>1?'s':''}? This can’t be undone.`))return;
  await del([...sel]);sel.clear();setSelecting(false);};

/* ---------- viewer ---------- */
function killPlayer(){if(player){try{player.destroy()}catch(e){}player=null;}}
function openAt(i){
  if(i<0||i>=shown.length)return;cur=i;killPlayer();const it=shown[i];
  if(it.kind==='snapshot')$('lbc').innerHTML=`<img src="${clipUrl(it.name)}" alt="Snapshot ${esc(fmtWhen(it.ts))}">`;
  else{$('lbc').innerHTML='<video id=lbv controls autoplay playsinline></video>';const v=$('lbv');
    if(it.name.endsWith('.ts')&&window.mpegts&&mpegts.isSupported()){
      player=mpegts.createPlayer({type:'mpegts',isLive:false,url:clipUrl(it.name)});
      player.attachMediaElement(v);player.load();player.play().catch(()=>{});}
    else{v.src=clipUrl(it.name);v.play().catch(()=>{});}}
  $('lbt').textContent=label(it)+' · '+fmtWhen(it.ts);
  $('lbcount').textContent=`${i+1} / ${shown.length} · ${fmtSize(it.bytes)}`+(it.duration?' · '+fmtDur(it.duration):'');
  $('dl').href=clipUrl(it.name);$('dl').setAttribute('download',it.name);
  $('prev').style.visibility=i>0?'visible':'hidden';$('next').style.visibility=i<shown.length-1?'visible':'hidden';
  $('lb').classList.add('on');document.body.style.overflow='hidden';
  history.replaceState(null,'','#'+encodeURIComponent(it.name));
}
function closeLb(){killPlayer();$('lbc').innerHTML='';$('lb').classList.remove('on');document.body.style.overflow='';cur=-1;
  history.replaceState(null,'',location.pathname);if(sig==='')load();}
$('cls').onclick=closeLb;$('prev').onclick=()=>openAt(cur-1);$('next').onclick=()=>openAt(cur+1);
$('lbm').onclick=e=>{if(e.target.id==='lbm')closeLb();};
$('lbdel').onclick=async()=>{const it=shown[cur];if(!it)return;
  if(!await confirmBox(`Delete this ${label(it).toLowerCase()}? This can’t be undone.`))return;
  killPlayer();const gone=await del([it.name]);if(!gone.size)return;
  const i=cur;shown=items.filter(x=>match(x,filt));render();
  if(shown.length)openAt(Math.min(i,shown.length-1));else closeLb();};
document.addEventListener('keydown',e=>{if(cur<0||$('confirm').open)return;
  if(e.key==='Escape')closeLb();else if(e.key==='ArrowLeft')openAt(cur-1);else if(e.key==='ArrowRight')openAt(cur+1);
  else if(e.key==='Delete')$('lbdel').click();});
let tx=null,ty=0;
$('lbm').addEventListener('touchstart',e=>{tx=e.touches[0].clientX;ty=e.touches[0].clientY;},{passive:true});
$('lbm').addEventListener('touchend',e=>{if(tx===null)return;const dx=e.changedTouches[0].clientX-tx,dy=e.changedTouches[0].clientY-ty;tx=null;
  if(Math.abs(dx)>60&&Math.abs(dx)>1.5*Math.abs(dy))openAt(cur+(dx<0?1:-1));
  else if(dy>110&&Math.abs(dy)>1.5*Math.abs(dx))closeLb();},{passive:true});

/* ---------- data ---------- */
let first=true;
async function load(){
  if(cur>=0||selecting)return;                 // don't reshuffle under the user
  try{const r=await api('/api/media');
    const s=JSON.stringify(r.items.map(i=>[i.name,i.duration]));
    if(s!==sig){sig=s;items=r.items;render();}
    if(first){first=false;const h=decodeURIComponent(location.hash.slice(1));
      if(h){const i=shown.findIndex(x=>x.name===h||x.thumb===h);if(i>=0)openAt(i);}}
  }catch(e){if(first)$('list').innerHTML='<div class=empty>Could not load the gallery.</div>';}
}
async function pollState(){try{const s=await api('/api/state');const on=s.last_seen&&s.now-s.last_seen<30;
  $('dot').className='dot'+(s.streaming?' live':on?' on':'');$('status').textContent=s.streaming?'Streaming':on?'Online':'Offline';}catch(e){}}
window.addEventListener('hashchange',()=>{const h=decodeURIComponent(location.hash.slice(1));
  if(!h){if(cur>=0)closeLb();return;}if(cur>=0&&shown[cur]&&shown[cur].name===h)return;
  let i=shown.findIndex(x=>x.name===h);if(i<0&&filt!=='all'){filt='all';render();i=shown.findIndex(x=>x.name===h);}if(i>=0)openAt(i);});
load();setInterval(load,15000);pollState();setInterval(pollState,10000);
</script></body></html>"""

GALLERY_HRIGHT = '<span class=pill><span id=status>…</span></span>'


ICONS = {
    "play":  '<path d="M7 4v16l13-8z" fill="currentColor" stroke="none"/>',
    "stop":  '<rect x="6" y="6" width="12" height="12" rx="1.5" fill="currentColor" stroke="none"/>',
    "cam":   '<path d="M4 8h3l2-3h6l2 3h3v11H4z"/><circle cx="12" cy="13" r="3.5"/>',
    "mute":  '<path d="M4 9h4l5-4v14l-5-4H4z"/><path d="M17 9.5l4 5M21 9.5l-4 5"/>',
    "sound": '<path d="M4 9h4l5-4v14l-5-4H4z"/><path d="M16.5 8.5a5 5 0 0 1 0 7M19 6a8.5 8.5 0 0 1 0 12"/>',
    "fs":    '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
    "dl":    '<path d="M12 4v11m-5-5 5 5 5-5M5 20h14"/>',
    "trash": '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>',
    "x":     '<path d="M6 6l12 12M18 6 6 18"/>',
}


def _build(tpl, active, hright, **extra):
    for k, v in ICONS.items():
        tpl = tpl.replace(f"__I_{k}__", f'<svg class=ic viewBox="0 0 24 24" aria-hidden=true>{v}</svg>')
    s = (tpl.replace("__HEADER__", header(active).replace("__HRIGHT__", hright))
            .replace("__CSS__", CSS).replace("__CONFIRM__", CONFIRM_HTML)
            .replace("__COMMON_JS__", COMMON_JS))
    for k, v in extra.items():
        s = s.replace(f"__{k}__", v)
    return s


def start(hub, logdir, media_req, media_stop, media_ip, port=8090,
          get_settings=None, settings_file=None):
    page = _build(PAGE, "cam", ARM_HTML, SETTINGS=json.dumps(
        [{"key": k, "label": lbl, "control": c, "options": o} for k, lbl, c, o in SETTINGS_UI]))
    gallery = _build(GALLERY, "gal", GALLERY_HRIGHT,
                     RETENTION=json.dumps(RETENTION_DAYS if RETENTION_DAYS > 0 else 0))

    def newest(pat):
        # skip liveview thumbnails (<base>.thumb.jpg) -- they are not snapshots
        fs = [f for f in glob.glob(os.path.join(logdir, pat)) if not f.endswith(".thumb.jpg")]
        fs.sort(key=os.path.getmtime, reverse=True)
        return fs[0] if fs else None

    def latest_snapshot():
        f = newest("*.jpg")
        if not f: return None
        try: return {"name": os.path.basename(f), "ts": os.path.getmtime(f)}
        except OSError: return None

    # --- clip durations: ffprobe in a background thread, cached by (name, mtime)
    durations = {}
    probe_q = _queue.Queue()
    queued = set()
    ffprobe = shutil.which("ffprobe")

    def _prober():
        while True:
            key = probe_q.get()
            name, _mt = key
            d = None
            try:
                r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                                    "-of", "default=nw=1:nk=1", os.path.join(logdir, name)],
                                   capture_output=True, text=True, timeout=15)
                d = round(float(r.stdout.strip()), 1)
            except Exception:
                d = 0          # unknown; don't retry
            durations[key] = d
            queued.discard(key)

    if ffprobe:
        threading.Thread(target=_prober, daemon=True).start()

    def duration(name, mtime):
        key = (name, mtime)
        if key in durations:
            return durations[key] or None
        if ffprobe and key not in queued:
            queued.add(key); probe_q.put(key)
        return None

    def list_media():
        """All saved snapshots + clips in logdir, newest first. A liveview capture
        yields <base>.ts (+ <base>.mp4 if remuxed) and <base>.thumb.jpg; the mp4
        represents the clip when present (plays natively), else the .ts. Motion
        clips are named *_motion_* by local_server."""
        items = []
        for f in glob.glob(os.path.join(logdir, "*")):
            n = os.path.basename(f)
            if n.endswith(".thumb.jpg"):
                continue
            try: st = os.stat(f)
            except OSError: continue
            if n.endswith(".jpg"):
                items.append({"kind": "snapshot", "name": n, "thumb": n, "motion": False,
                              "ts": st.st_mtime, "bytes": st.st_size, "duration": None})
            elif n.endswith((".ts", ".mp4")):
                base = n.rsplit(".", 1)[0]
                if n.endswith(".ts") and os.path.exists(os.path.join(logdir, base + ".mp4")):
                    continue
                thumb = base + ".thumb.jpg"
                items.append({"kind": "clip", "name": n, "motion": "_motion_" in n,
                              "thumb": thumb if os.path.exists(os.path.join(logdir, thumb)) else None,
                              "ts": st.st_mtime, "bytes": st.st_size,
                              "duration": duration(n, st.st_mtime)})
        for it in items:   # filename prefix = capture time; more accurate than mtime
            m = re.match(r"(\d{8}-\d{6})_", it["name"])
            if m:
                try: it["ts"] = time.mktime(time.strptime(m.group(1), "%Y%m%d-%H%M%S"))
                except Exception: pass
        items.sort(key=lambda it: it["ts"], reverse=True)
        return items

    def delete_media(names):
        """Delete each named item plus its sibling files (same capture base)."""
        deleted = []
        real_dir = os.path.realpath(logdir)
        for n in names if isinstance(names, list) else []:
            if not isinstance(n, str) or not NAME_RE.fullmatch(n):
                continue
            if n.endswith(".thumb.jpg"): base = n[:-len(".thumb.jpg")]
            elif n.endswith((".jpg", ".mp4", ".ts")): base = n.rsplit(".", 1)[0]
            else: continue
            removed = False
            for ext in SIBLING_EXTS:
                p = os.path.join(logdir, base + ext)
                if os.path.dirname(os.path.realpath(p)) != real_dir:
                    continue
                try:
                    os.remove(p); removed = True
                except OSError:
                    pass
            if removed:
                deleted.append(n)
        return deleted

    settings_lock = threading.Lock()

    def update_settings(upd):
        """Merge `upd` into settings.json. Locked so concurrent saves don't drop
        each other's changes; written to a temp file and renamed so the server's
        heartbeat never reads a half-written file."""
        with settings_lock:
            try:
                with open(settings_file) as f:
                    cur = dict(json.load(f))
            except Exception:
                cur = {}
            cur.update(upd)
            os.makedirs(os.path.dirname(settings_file), exist_ok=True)
            tmp = settings_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(cur, f)
            os.replace(tmp, settings_file)

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *a): pass

        def _send(self, code, ctype, body, cache=False):
            if isinstance(body, str): body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if not cache: self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try: self.wfile.write(body)
            except Exception: pass

        def _json(self, obj, code=200): self._send(code, "application/json", json.dumps(obj))

        def _file(self, path, ctype):
            try: b = open(path, "rb").read()
            except Exception: return self.send_error(404)
            self._send(200, ctype, b)

        def _clip(self, name):
            """Serve a captured clip/snapshot from logdir, with HTTP Range support
            (iOS/Safari won't play an MP4 without it). Bare filenames only."""
            if not re.fullmatch(r"[\w.-]+\.(mp4|jpg|ts)", name) or name.startswith("."):
                return self.send_error(404)
            path = os.path.join(logdir, name)
            try: size = os.path.getsize(path)
            except OSError: return self.send_error(404)
            start, end = 0, size - 1
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2): end = min(int(m.group(2)), size - 1)
                else:                                  # suffix range: last N bytes
                    start = max(0, size - int(m.group(2)))
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0"); self.end_headers()
                    return
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(200)
            self.send_header("Content-Type", {"mp4": "video/mp4", "ts": "video/mp2t"}.get(
                name.rsplit(".", 1)[-1], "image/jpeg"))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Cache-Control", "private, max-age=86400")
            self.end_headers()
            if self.command == "HEAD":
                return
            try:
                with open(path, "rb") as f:
                    f.seek(start); left = end - start + 1
                    while left > 0:
                        chunk = f.read(min(65536, left))
                        if not chunk: break
                        self.wfile.write(chunk); left -= len(chunk)
            except Exception:
                pass

        def do_HEAD(self):
            p = self.path.split("?", 1)[0]
            if p.startswith("/clips/"): self._clip(p[len("/clips/"):])
            else: self.send_error(404)

        def _body(self):
            try:
                n = int(self.headers.get("Content-Length", 0))
                return json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return {}

        def _query(self):
            q = {}
            if "?" in self.path:
                for kv in self.path.split("?", 1)[1].split("&"):
                    if "=" in kv: k, v = kv.split("=", 1); q[k] = v
            return q

        def do_GET(self):
            p = self.path.split("?", 1)[0]
            if p in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", page)
            elif p == "/gallery":
                self._send(200, "text/html; charset=utf-8", gallery)
            elif p == "/static/mpegts.js":
                self._file(os.path.join(STATIC, "mpegts.js"), "application/javascript")
            elif p == "/api/state":
                try: st = json.load(open(os.path.join(logdir, "state.json")))
                except Exception: st = {}
                st["latest_snapshot"] = latest_snapshot()
                st["now"] = time.time()
                try:
                    cur = get_settings() if get_settings else {}
                    st["armed"] = int(bool(cur.get("armed")))
                    st["snapshot_interval"] = int(float(cur.get("snapshot_interval") or 0))
                except Exception: pass
                self._json(st)
            elif p == "/api/settings":
                cur = {}
                try: cur = get_settings() if get_settings else {}
                except Exception: cur = {}
                self._json({"current": cur})
            elif p == "/api/media":
                items = list_media()
                try: limit = int(self._query().get("limit", 0))
                except ValueError: limit = 0
                self._json({"items": items[:limit] if limit > 0 else items})
            elif p == "/live.ts":
                self._live()
            elif p.startswith("/clips/"):
                self._clip(p[len("/clips/"):])
            else:
                self.send_error(404)

        def _live(self):
            q = hub.subscribe()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                idle = 0
                while True:
                    try: data = q.get(timeout=1.0)
                    except _queue.Empty:
                        idle += 1
                        if idle > 90: break
                        continue
                    idle = 0
                    self.wfile.write(data); self.wfile.flush()
            except Exception:
                pass
            finally:
                hub.unsubscribe(q)

        def _same_origin(self):
            """CSRF guard for every POST. Require a JSON body type: a cross-site
            page can only send one after a CORS preflight, which we never grant.
            Also reject an Origin / Sec-Fetch-Site that says the request came
            from another site."""
            if "application/json" not in self.headers.get("Content-Type", ""):
                return False
            if self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
                return False
            origin = self.headers.get("Origin")
            if origin and origin != "null":
                host = origin.split("://", 1)[-1]
                if host != self.headers.get("Host", ""):
                    return False
            return True

        def do_POST(self):
            p = self.path.split("?", 1)[0]
            q = self._query()
            if not self._same_origin():
                return self.send_error(403)
            if p == "/api/snapshot":
                json.dump({"mode": "snapshot", "ts": time.time()}, open(media_req, "w"))
                self._json({"msg": "ok"})
            elif p == "/api/liveview":
                try: secs = max(5, min(600, int(q.get("secs", 60))))
                except Exception: secs = 60
                json.dump({"mode": "liveview", "ip": media_ip, "live": True,
                           "secs": secs, "ts": time.time()}, open(media_req, "w"))
                self._json({"msg": "ok"})
            elif p == "/api/stop":
                try: open(media_stop, "w").close()
                except Exception: pass
                try: os.remove(media_req)
                except Exception: pass
                self._json({"msg": "ok"})
            elif p == "/api/delete":
                self._json({"deleted": delete_media(self._body().get("names"))})
            elif p == "/api/arm":
                # Arm/disarm the camera. The firmware records on motion only when
                # BOTH the armed and motion bytes are set, so arming sets both =1
                # and disarming both =0. Pushed to the camera on its next heartbeat.
                if not settings_file: return self.send_error(400)
                armed = 1 if self._body().get("armed") else 0
                try:
                    update_settings({"armed": armed, "motion": armed})
                    self._json({"armed": armed, "ts": time.time()})
                except Exception as e:
                    self._json({"msg": str(e)}, 500)
            elif p == "/api/timelapse":
                # Interval (time-lapse) snapshots: store the interval in seconds
                # (0 = off) in settings.json; local_server's scheduler reads it live.
                if not settings_file: return self.send_error(400)
                try: secs = max(0, int(self._body().get("seconds", 0)))
                except Exception: secs = 0
                try:
                    update_settings({"snapshot_interval": secs})
                    self._json({"snapshot_interval": secs, "ts": time.time()})
                except Exception as e:
                    self._json({"msg": str(e)}, 500)
            elif p == "/api/settings":
                if not settings_file: return self.send_error(400)
                want, upd = self._body(), {}
                for k, _lbl, _c, _o in SETTINGS_UI:
                    if k in want:
                        try: upd[k] = int(want[k])
                        except Exception: pass
                try:
                    update_settings(upd)
                    self._json({"msg": "saved", "ts": time.time()})
                except Exception as e:
                    self._json({"msg": str(e)}, 500)
            else:
                self.send_error(404)

    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[webui] http://0.0.0.0:{port}  (media_ip={media_ip})", flush=True)
    return srv
