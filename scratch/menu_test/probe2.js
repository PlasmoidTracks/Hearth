const {withPage}=require('./cdp');
(async()=>{
 await withPage(8947,'page.html', async(ev)=>{
  console.log('has new code:', await ev('positionAttachMenu.toString().includes("Unpin first")'));
  console.log('rAF fires:', await ev('new Promise(res=>requestAnimationFrame(()=>res(true)))'));
  await ev('openAttachMenu()');
  // Wait several frames to be sure rAF had its chance.
  await ev('new Promise(res=>{let n=0;const step=()=>{n++; if(n>10) return res(true); requestAnimationFrame(step)};requestAnimationFrame(step)})');
  console.log(await ev(`(()=>{const b=document.getElementById('attachMenuBtn').getBoundingClientRect();
   const m=document.getElementById('attachMenu').getBoundingClientRect();
   return JSON.stringify({btn:[Math.round(b.top),Math.round(b.bottom)],menu:[Math.round(m.top),Math.round(m.bottom)],
    winH:window.innerHeight, inlineTop:document.getElementById('attachMenu').style.top});})()`));
 });
 process.exit(0);
})().catch(e=>{console.log('ERR',e);process.exit(1)});
