// Replicate the USER's flow: login, then a HARD cache-bypassing reload (CDP
// Page.reload ignoreCache=true = exactly what Ctrl+Shift+R does), then click.
const http=require('http'),net=require('net'),crypto=require('crypto'),fs=require('fs');
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
  fs.rmSync('/tmp/hearth-e2e-prof',{recursive:true,force:true});
  const chrome=spawn('google-chrome',['--headless=new','--disable-gpu','--no-sandbox','--remote-debugging-port=9301',
    '--user-data-dir=/tmp/hearth-e2e-prof','about:blank']);
  let t=null;for(let i=0;i<60&&!t;i++){await new Promise(r=>setTimeout(r,250));t=await getJson(9301,'/json/list').catch(()=>null);}
  const page=t.find(x=>x.type==='page');const ws=await wsConnect(page.webSocketDebuggerUrl);
  let id=0;const pend={};ws.on(m=>{if(m.id&&pend[m.id]){pend[m.id](m);delete pend[m.id];}});
  const logs=[];ws.on(m=>{if(m.method==='Runtime.consoleAPICalled')logs.push((m.params.args||[]).map(a=>a.value!==undefined?a.value:a.description).join(' '));});
  ws.on(m=>{if(m.method==='Runtime.exceptionThrown')logs.push('EXC '+(m.params.exceptionDetails?.exception?.description||JSON.stringify(m.params.exceptionDetails.text||'')));});
  const cmd=(method,params={})=>new Promise(r=>{const i=++id;pend[i]=r;ws.send({id:i,method,params});});
  const ev=async(expr)=>{const r=await cmd('Runtime.evaluate',{expression:expr,returnByValue:true,awaitPromise:true});
    return r.result&&r.result.result?r.result.result.value:JSON.stringify(r).slice(0,300);};
  await cmd('Page.enable');await cmd('Network.enable');await cmd('Runtime.enable');

  // Login.
  await cmd('Page.navigate',{url:BASE+'/login'});
  await new Promise(r=>setTimeout(r,1200));
  await ev(`(()=>{const u=document.querySelector('input[name=username]'),p=document.querySelector('input[name=password]');
    u.value='e2e_probe'; p.value='E2eTest!pass-42'; const f=u.closest('form'); f.requestSubmit?f.requestSubmit():f.submit();})()`);
  await new Promise(r=>setTimeout(r,1800));

  // >>> THE HARD RELOAD: Ctrl+Shift+R equivalent.
  await cmd('Page.reload',{ignoreCache:true});
  await new Promise(r=>setTimeout(r,2500));

  console.log('url:', await ev('location.href'));
  for(let i=0;i<20;i++){ if(await ev('!!document.getElementById("attachMenuBtn")')===true) break; await new Promise(r=>setTimeout(r,300)); }

  // What does the LIVE DOM look like? (button + panel classes as rendered)
  console.log('DOM:', await ev(`(()=>{const b=document.getElementById('attachMenuBtn'), m=document.getElementById('attachMenu');
    return JSON.stringify({btn: !!b, btnClass:b&&b.className, panel:!!m, panelClass:m&&m.className});})()`));

  // Click for real.
  const b=JSON.parse(await ev('JSON.stringify(document.getElementById("attachMenuBtn").getBoundingClientRect())'));
  await cmd('Input.dispatchMouseEvent',{type:'mousePressed',x:b.left+12,y:b.top+12,button:'left',clickCount:1,buttons:1});
  await cmd('Input.dispatchMouseEvent',{type:'mouseReleased',x:b.left+12,y:b.top+12,button:'left',clickCount:1,buttons:0});
  await new Promise(r=>setTimeout(r,600));

  const st = await ev(`(()=>{const m=document.getElementById('attachMenu'); if(!m) return 'NO MENU EL';
    const cs=getComputedStyle(m); const r=m.getBoundingClientRect();
    return JSON.stringify({hidden:m.classList.contains('hidden'),position:cs.position,display:cs.display,visibility:cs.visibility,
      zIndex:cs.zIndex, rect:[Math.round(r.left),Math.round(r.top),Math.round(r.width),Math.round(r.height)],
      items:Array.from(m.querySelectorAll('[data-am]')).map(x=>x.getAttribute('data-am'))});})()`);
  console.log('MENU AFTER HARD RELOAD:', st);

  const shot=await cmd('Page.captureScreenshot',{format:'png'});
  if(shot.result?.data) fs.writeFileSync('hard_reload_menu.png', Buffer.from(shot.result.data,'base64'));
  console.log('CONSOLE:', JSON.stringify(logs.filter(l=>!l.includes('tailwindcss.com')).slice(0,12)));
  chrome.kill();process.exit(0);
})().catch(e=>{console.log('HARNESS ERR',e);process.exit(1)});
