// Drive the real chat.js attach-menu code in a real browser via CDP.
const http = require('http'), fs = require('fs'), path = require('path');
const { execFile } = require('child_process');

const page = fs.readFileSync('page.html');
const srv = http.createServer((req, res) => {
  const p = req.url.split('?')[0];
  if (p === '/' ) { res.writeHead(200, {'Content-Type':'text/html'}); return res.end(page); }
  const f = path.join('.', p);
  if (!fs.existsSync(f)) { res.writeHead(404); return res.end('nf'); }
  res.writeHead(200, {'Content-Type': p.endsWith('.js') ? 'application/javascript' : 'text/css'});
  res.end(fs.readFileSync(f));
});

srv.listen(0, async () => {
  const port = srv.address().port;
  const url = `http://127.0.0.1:${port}/`;
  // Use Chrome headless with --remote-debugging and drive via CDP over websocket.
  const chrome = execFile('google-chrome', [
    '--headless=new', '--disable-gpu', '--no-sandbox', '--allow-file-access-from-files',
    `--remote-debugging-port=0`, url, '--virtual-time-budget=4000'
  ], () => {});
  // Poll DevToolsActivePort file
  const fs2 = fs;
  let wsUrl = null;
  for (let i=0;i<50 && !wsUrl;i++){
    await new Promise(r=>setTimeout(r,200));
    // chrome prints the ws url to stderr on newer versions with --user-data-dir? fallback: fetch /json/list
    try {
      const data = await new Promise((resolve,reject)=>{
        http.get(`http://127.0.0.1:${port}/json/version`, res=>{}).on('error',()=>reject());
      }).catch(()=>null);
    } catch(e){}
  }
  chrome.kill(); srv.close();
});
