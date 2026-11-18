// End-to-end against the REAL running server (http://127.0.0.1:8082):
// login with the throwaway probe user, open /chat, click the 📎 button for real.
const http=require('http'),net=require('net'),crypto=require('crypto');
const {spawn}=require('child_process');
const BASE='http://127.0.0.1:8082';
function getJson(port,pn){return new Promise((rs,rj)=>{http.get({host:'127.0.0.1',port,path:pn},r=>{let d='';r.on('data',c=>d+=c);r.on('end',()=>{try{rs(JSON.parse(d))}catch(e){rj(e)}})}).on('error',rj)});}
function wsConnect(wsUrl){return new Promise((res)=>{const u=new URL(wsUrl);const key=crypto.randomBytes(16).toString('base64');
 const sock=net.connect(parseInt(u.port),u.hostname,()=>{sock.write(`GET ${u.pathname} HTTP/1.1\r\nHost: ${u.host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: ${key}\r\nSec-WebSocket-Version: 13\r\n\r\n`)});
 let buf=Buffer.alloc(0),hs=false;const H=[];
 sock.on('data',d=>{buf=Buffer.concat([buf,d]);if(!hs){const i=buf.indexOf('\r\n\r\n');if(i<0)return;buf=buf.slice(i+4);hs=true;res(api);}
  while(buf.length>=2){const l0=buf[1]&0x7f;let off=2,len=l0;if(l0===126){if(buf.length<4)return;len=buf.readUInt16BE(2);off=4;}else if(l0===127){if(buf.length<10)return;len=Number(buf.readBigUInt64BE(2));off=10;}if(buf.length<off+len)return;
   const pl=buf.slice(off,off+len);buf=buf.slice(off+len);try{const m=JSON.parse(pl.toString());H.forEach(h=>h(m));}catch(e){}}});
 function send(o){const p=Buffer.from(JSON.stringify(o));const m=crypto.randomBytes(4);const mm=Buffer.alloc(p.length);for(let i=0;i<p.length;i++)mm[i]=p[i]^m[i%4];
  let h;if(p.length<126)h=Buffer.from([0x81,0x80|p.length]);else{h=Buffer.alloc(4);h[0]=0x81;h[1]=0x80|126;h.writeUInt16BE(p.length,2);}sock.write(Buffer.concat([h,m,mm]));}
 const api={send,on:h=>H.push(h),sock};});}

(async()=>{
  const chrome=spawn('google-chrome',['--headless=new','--disable-gpu','--no-sandbox','--remote-debugging-port=9300','about:blank']);
  let t=null;for(let i=0;i<60&&!t;i++){await new Promise(r=>setTimeout(r,250));t=await getJson(9300,'/json/list').catch(()=>null);}
  const page=t.find(x=>x.type==='page');const ws=await wsConnect(page.webSocketDebuggerUrl);
  let id=0;const pend={};ws.on(m=>{if(m.id&&pend[m.id]){pend[m.id](m);delete pend[m.id];}});
  const logs=[];ws.on(m=>{if(m.method==='Runtime.consoleAPICalled')logs.push((m.params.args||[]).map(a=>a.value!==undefined?a.value:a.description).join(' '));});
  ws.on(m=>{if(m.method==='Runtime.exceptionThrown')logs.push('EXC '+(m.params.exceptionDetails?.exception?.description||JSON.stringify(m.params.exceptionDetails.text||'')));});
  const cmd=(method,params={})=>new Promise(r=>{const i=++id;pend[i]=r;ws.send({id:i,method,params});});
  const ev=async(expr)=>{const r=await cmd('Runtime.evaluate',{expression:expr,returnByValue:true,awaitPromise:true});
    return r.result&&r.result.result?r.result.result.value:JSON.stringify(r).slice(0,300);};
  await cmd('Page.enable');await cmd('Runtime.enable');

  // 1) Load login page (this also sets the CSRF cookie inside the browser).
  await cmd('Page.navigate',{url:BASE+'/login'});
  await new Promise(r=>setTimeout(r,1200));
  console.log('page title:', await ev('document.title'));

  // 2) Fill the real login form and submit it like a user would.
  const filled = await ev(`(()=>{const u=document.querySelector('input[name=username]'),p=document.querySelector('input[name=password]');
    if(!u||!p) return 'NO FIELDS'; u.value='e2e_probe'; p.value='E2eTest!pass-42';
    const f=u.closest('form'); f.requestSubmit ? f.requestSubmit() : f.submit(); return 'submitted';})()`);
  console.log('login submit:', filled);
  await new Promise(r=>setTimeout(r,1800));
  console.log('url after login:', await ev('location.href'));

  // 3) If not on /chat, navigate there directly.
  if(!String(await ev('location.href')).includes('/chat')) { await cmd('Page.navigate',{url:BASE+'/'}); await new Promise(r=>setTimeout(r,2000)); }
  console.log('final url:', await ev('location.href'));

  // 4) Wait for chat boot (composer exists + chat.js wired).
  for(let i=0;i<20;i++){ const ok = await ev('!!document.getElementById("attachMenuBtn")'); if(ok===true) break; await new Promise(r=>setTimeout(r,300)); }
  console.log('attach button exists:', await ev('!!document.getElementById("attachMenuBtn")'));
  console.log('typeof openAttachMenu:', await ev('typeof window.openAttachMenu'));

  // 5) REAL trusted click on the button.
  const b=JSON.parse(await ev('JSON.stringify(document.getElementById("attachMenuBtn").getBoundingClientRect())'));
  console.log('button rect:', JSON.stringify(b));
  await cmd('Input.dispatchMouseEvent',{type:'mousePressed',x:b.left+12,y:b.top+12,button:'left',clickCount:1,buttons:1});
  await cmd('Input.dispatchMouseEvent',{type:'mouseReleased',x:b.left+12,y:b.top+12,button:'left',clickCount:1,buttons:0});
  await new Promise(r=>setTimeout(r,500));

  const state = await ev(`(()=>{const m=document.getElementById('attachMenu'); if(!m) return 'NO MENU EL';
    const cs=getComputedStyle(m); const r=m.getBoundingClientRect();
    return JSON.stringify({hidden:m.classList.contains('hidden'),position:cs.position,display:cs.display,visibility:cs.visibility,
      rect:[Math.round(r.left),Math.round(r.top),Math.round(r.width),Math.round(r.height)],
      items:Array.from(m.querySelectorAll('[data-am]')).map(x=>x.getAttribute('data-am')),
      text:m.innerText.replace(/\\n/g,';')});})()`);
  console.log('MENU STATE:', state);

  const shot=await cmd('Page.captureScreenshot',{format:'png'});
  if(shot.result?.data) require('fs').writeFileSync('live_menu.png', Buffer.from(shot.result.data,'base64'));
  console.log('CONSOLE:', JSON.stringify(logs.slice(0,15)));
  chrome.kill();process.exit(0);
})().catch(e=>{console.log('HARNESS ERR',e);process.exit(1)});
