const {withPage}=require('./cdp');
(async()=>{
 await withPage(8951,'page.html', async(ev)=>{
  // Instrument positionAttachMenu by monkeypatching to trace every write.
  console.log(await ev(`(()=>{
    const menu=document.getElementById('attachMenu');
    const desc=Object.getOwnPropertyDescriptor(CSSStyleDeclaration.prototype,'top')||{};
    window.__writes=[];
    const origSet=window.CSSStyleDeclaration.prototype.setProperty;
    // simpler: wrap positionAttachMenu internals via Proxy not possible; instead log after each phase
    openAttachMenu();
    const w1=menu.style.top;
    return JSON.stringify({after_sync_top:w1, hidden:menu.classList.contains('hidden')});
  })()`));
  await ev('new Promise(res=>{let n=0;const s=()=>{n++;if(n>15)return res(true);requestAnimationFrame(s)};requestAnimationFrame(s)})');
  console.log('after frames:', await ev(`JSON.stringify({top:document.getElementById('attachMenu').style.top, left:document.getElementById('attachMenu').style.left})`));
  // What does btn rect look like INSIDE rAF? Re-run the function body manually with logging.
  console.log(await ev(`(()=>{
    const menu=document.getElementById('attachMenu'), btn=document.getElementById('attachMenuBtn');
    const r=btn.getBoundingClientRect(); const mr=menu.getBoundingClientRect();
    return JSON.stringify({r:[Math.round(r.left),Math.round(r.top),Math.round(r.width),Math.round(r.height)],
      mr:[Math.round(mr.left),Math.round(mr.top),Math.round(mr.width),Math.round(mr.height)]});
  })()`));
 });
 process.exit(0);
})().catch(e=>{console.log('ERR',e);process.exit(1)});
