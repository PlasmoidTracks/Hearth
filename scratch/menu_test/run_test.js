// Serve the harness over HTTP (file:// CORS breaks chat.js init), drive via CDP.
const http = require('http'), fs = require('fs'), path = require('path'), net = require('net');
const { spawn } = require('child_process');

const ROOT = __dirname;
const MIME = {'.html':'text/html','.js':'application/javascript','.css':'text/css'};
const srv = http.createServer((req,res)=>{
  const p = req.url.split('?')[0];
  const f = path.join(ROOT, p === '/' ? 'page.html' : p);
  if(!fs.existsSync(f)){ res.writeHead(404); return res.end('nf'); }
  res.writeHead(200, {'Content-Type': MIME[path.extname(f)] || 'text/plain'});
  res.end(fs.readFileSync(f));
});

function getJson(port, pathName){
  return new Promise((resolve,reject)=>{
    http.get({host:'127.0.0.1',port,path:pathName},res=>{
      let d=''; res.on('data',c=>d+=c); res.on('end',()=>{try{resolve(JSON.parse(d))}catch(e){reject(e)}});
    }).on('error',reject);
  });
}

// Minimal WebSocket client (only text frames, server->client and one small send).
function wsConnect(wsUrl){
  return new Promise((resolve,reject)=>{
    const u = new URL(wsUrl);
    const key = require('crypto').randomBytes(16).toString('base64');
    const sock = net.connect(parseInt(u.port), u.hostname, ()=>{
      sock.write(
        `GET ${u.pathname} HTTP/1.1\r\nHost: ${u.host}\r\nUpgrade: websocket\r\n`+
        `Connection: Upgrade\r\nSec-WebSocket-Key: ${key}\r\nSec-WebSocket-Version: 13\r\n\r\n`);
    });
    let buf = Buffer.alloc(0), handshook = false;
    const handlers = [];
    sock.on('data', d=>{
      buf = Buffer.concat([buf,d]);
      if(!handshook){
        const i = buf.indexOf('\r\n\r\n'); if(i<0) return;
        buf = buf.slice(i+4); handshook = true; resolve(api);
      }
      // parse frames
      while(buf.length >= 2){
        const len0 = buf[1] & 0x7f; let off=2, len=len0;
        if(len0===126){ if(buf.length<4) return; len=buf.readUInt16BE(2); off=4; }
        else if(len0===127){ if(buf.length<10) return; len=Number(buf.readBigUInt64BE(2)); off=10; }
        if(buf.length < off+len) return;
        const payload = buf.slice(off, off+len); buf = buf.slice(off+len);
        try{ const msg = JSON.parse(payload.toString()); handlers.forEach(h=>h(msg)); }catch(e){}
      }
    });
    function send(obj){
      const payload = Buffer.from(JSON.stringify(obj));
      const mask = require('crypto').randomBytes(4);
      const masked = Buffer.alloc(payload.length);
      for(let i=0;i<payload.length;i++) masked[i]=payload[i]^mask[i%4];
      let header;
      if(payload.length<126) header = Buffer.from([0x81, 0x80|payload.length]);
      else if(payload.length<65536){ header=Buffer.alloc(4); header[0]=0x81; header[1]=0x80|126; header.writeUInt16BE(payload.length,2); }
      else { header=Buffer.alloc(10); header[0]=0x81; header[1]=0x80|127; header.writeBigUInt64BE(BigInt(payload.length),2); }
      sock.write(Buffer.concat([header,mask,masked]));
    }
    const api = { send, on: h=>handlers.push(h), sock };
  });
}

(async ()=>{
  await new Promise(r=>srv.listen(8931,r));
  const chrome = spawn('google-chrome',['--headless=new','--disable-gpu','--no-sandbox',
    '--remote-debugging-port=9222','about:blank']);
  let targets=null;
  for(let i=0;i<60 && !targets;i++){ await new Promise(r=>setTimeout(r,250));
    targets = await getJson(9222,'/json/list').catch(()=>null); }
  if(!targets){ console.log('NO CDP'); chrome.kill(); process.exit(1); }
  const page = targets.find(t=>t.type==='page');
  const ws = await wsConnect(page.webSocketDebuggerUrl);
  let id=0; const pending={};
  ws.on(m=>{ if(m.id && pending[m.id]){ pending[m.id](m.result,m.error); delete pending[m.id]; } });
  const cmd = (method,params={})=> new Promise(res=>{ const i=++id; pending[i]=res; ws.send({id:i,method,params}); });

  await cmd('Page.enable');
  await cmd('Page.navigate',{url:'http://127.0.0.1:8931/'});
  // Wait for the in-page chain to finish (title starts with TEST::), poll via Runtime.
  let title='';
  for(let i=0;i<40;i++){
    await new Promise(r=>setTimeout(r,300));
    const t = await cmd('Runtime.evaluate',{expression:'document.title',returnByValue:true});
    title = t.result && t.result.value || '';
    if(title.startsWith('TEST::')) break;
  }
  // Also probe computed style directly for ground truth.
  const probe = await cmd('Runtime.evaluate',{expression:`(() => {
    const m=document.getElementById('attachMenu'); const cs=getComputedStyle(m);
    const r=m.getBoundingClientRect(); const b=document.getElementById('attachMenuBtn').getBoundingClientRect();
    return JSON.stringify({hidden:m.classList.contains('hidden'),vis:cs.visibility,disp:cs.display,
      menuRect:[Math.round(r.left),Math.round(r.top),Math.round(r.width),Math.round(r.height)],
      btnRect:[Math.round(b.left),Math.round(b.top)],
      inlineLeft:m.style.left, inlineTop:m.style.top}); })()`, returnByValue:true});
  console.log('TITLE:', title);
  console.log('PROBE(closed state):', probe.result.value);
  // Open via button through CDP click and re-probe.
  await cmd('Runtime.evaluate',{expression:'document.getElementById("attachMenuBtn").click()'});
  await new Promise(r=>setTimeout(r,400));
  const probe2 = await cmd('Runtime.evaluate',{expression:`(() => {
    const m=document.getElementById('attachMenu'); const cs=getComputedStyle(m); const r=m.getBoundingClientRect();
    return JSON.stringify({hidden:m.classList.contains('hidden'),vis:cs.visibility,disp:cs.display,
      rect:[Math.round(r.left),Math.round(r.top),Math.round(r.width),Math.round(r.height)],
      inlineLeft:m.style.left, inlineTop:m.style.top, inner:m.innerText.replace(/\\n/g,';')}); })()`, returnByValue:true});
  console.log('PROBE(open):', probe2.result.value);
  chrome.kill(); srv.close(); process.exit(0);
})().catch(e=>{ console.log('HARNESS ERROR', e); process.exit(1); });
