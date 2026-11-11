
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
