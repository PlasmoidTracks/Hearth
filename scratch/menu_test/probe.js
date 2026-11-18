const {withPage}=require('./cdp');
(async()=>{
 await withPage(8945,'page.html', async(ev,cmd)=>{
  await ev('openAttachMenu()');
  await new Promise(z=>setTimeout(z,300));
  console.log(await ev(`(()=>{const b=document.getElementById('attachMenuBtn').getBoundingClientRect();
   const m=document.getElementById('attachMenu').getBoundingClientRect();
   return JSON.stringify({btn:[Math.round(b.top),Math.round(b.bottom)],menu:[Math.round(m.top),Math.round(m.bottom)],
    winH:window.innerHeight, inlineTop:document.getElementById('attachMenu').style.top});})()`));
 });
 process.exit(0);
})().catch(e=>{console.log('ERR',e);process.exit(1)});
