__COMMON_JS__
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
