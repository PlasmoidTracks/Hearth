const http=require('http'),fs=require('fs'),path=require('path'),net=require('net'),crypto=require('crypto');
const {spawn}=require('child_process');
const ROOT=__dirname;
const srv=http.createServer((req,res)=>{const p=req.url.split('?')[0];const f=path.join(ROOT,p==='/'?'page.html':p);
 if(!fs.existsSync(f)){res.writeHead(404);return res.end('nf');}
 res.writeHead(200,{'Content-Type':p.endsWith('.js')?'application/javascript':'text/html'});res.end(fs.readFileSync(f));});
const getJson=(port,pn)=>new Promise((rs,rj)=>{http.get({host:'127.0.0.1',port,path:pn},r=>{let d='';r.on('data',c=>d+=c);r.on('end',()=>{try{rs(JSON.parse(d))}catch(e){rj(e)}})}).on('error',rj)});
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
 await new Promise(r=>srv.listen(8933,r));
 const chrome=spawn('google-chrome',['--headless=new','--disable-gpu','--no-sandbox','--remote-debugging-port=9224','about:blank']);
 let t=null;for(let i=0;i<60&&!t;i++){await new Promise(r=>setTimeout(r,250));t=await getJson(9224,'/json/list').catch(()=>null);}
 const page=t.find(x=>x.type==='page');const ws=await wsConnect(page.webSocketDebuggerUrl);
 let id=0;const pend={};ws.on(m=>{if(m.id&&pend[m.id]){pend[m.id](m);delete pend[m.id];}});
 const logs=[];ws.on(m=>{if(m.method==='Runtime.consoleAPICalled')logs.push((m.params.args||[]).map(a=>a.value!==undefined?a.value:a.description).join(' '));});
 ws.on(m=>{if(m.method==='Runtime.exceptionThrown')logs.push('EXC '+JSON.stringify(m.params.exceptionDetails.exception&&m.params.exceptionDetails.exception.description));});
 const cmd=(method,params={})=>new Promise(r=>{const i=++id;pend[i]=r;ws.send({id:i,method,params});});
 await cmd('Page.enable');await cmd('Runtime.enable');
 await cmd('Page.navigate',{url:'http://127.0.0.1:8933/'});
 await new Promise(r=>setTimeout(r,1500));
 const ev=async(expr)=>{const r=await cmd('Runtime.evaluate',{expression:expr,returnByValue:true});return r.result&&r.result.value!==undefined?r.result.value:JSON.stringify(r);};
 console.log('typeof openAttachMenu:', await ev('typeof openAttachMenu'));
 console.log('menu exists:', await ev('!!document.getElementById("attachMenu")'));
 console.log('open():', await ev('(openAttachMenu(), "called")'));
 console.log('after open hidden?:', await ev('document.getElementById("attachMenu").classList.contains("hidden")'));
 console.log('item exists:', await ev('!!document.querySelector("[data-am=proj_into]")'));
 const rect=await ev('JSON.stringify(document.querySelector("[data-am=proj_into]").getBoundingClientRect())');
 console.log('item rect:',rect);
 const R=JSON.parse(rect);
 await cmd('Input.dispatchMouseEvent',{type:'mousePressed',x:R.left+20,y:R.top+8,button:'left',clickCount:1,buttons:1});
 await cmd('Input.dispatchMouseEvent',{type:'mouseReleased',x:R.left+20,y:R.top+8,button:'left',clickCount:1,buttons:0});
 await new Promise(r=>setTimeout(r,300));
 console.log('after real click hidden?:', await ev('document.getElementById("attachMenu").classList.contains("hidden")'));
 console.log('uploadToProject:', await ev('String(S.uploadToProject)'));
 console.log('LOGS:',JSON.stringify(logs,null,1));
 chrome.kill();srv.close();process.exit(0);
})().catch(e=>{console.log('ERR',e);process.exit(1)});
