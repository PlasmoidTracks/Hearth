const {withPage}=require('./cdp');
(async()=>{
 await withPage(8949,'page.html', async(ev)=>{
  console.log(await ev(`(()=>{
    const out=[];
    const menu=document.getElementById('attachMenu'), btn=document.getElementById('attachMenuBtn');
    menu.classList.remove('hidden');
    menu.style.left = Math.max(4, btn.getBoundingClientRect().left) + 'px';
    menu.style.top  = Math.max(4, window.innerHeight - btn.offsetHeight - menu.offsetHeight - 12) + 'px';
    out.push('fallback top='+menu.style.top+' winH='+window.innerHeight+' btnH='+btn.offsetHeight+' menuH='+menu.offsetHeight);
    requestAnimationFrame(()=>{
      const r=btn.getBoundingClientRect();
      menu.style.top='0px';
      const mr=menu.getBoundingClientRect();
      out.push('rAF: r.height='+r.height+' mr.width='+mr.width+' mr.top='+mr.top);
      if(!mr.width || !r.height){ out.push('GUARD RETURNED'); }
      else {
        const h=menu.offsetHeight; let top=r.top-h-6;
        if(top<4) top=Math.min(r.bottom+6, window.innerHeight-h-4);
        menu.style.top=Math.max(4,top)+'px';
        out.push('final top='+menu.style.top+' h='+h);
      }
      document.title='RAF::'+out.join(' | ');
    });
    // Also: does a SECOND rAF callback exist from openAttachMenu's own call? irrelevant.
    return 'scheduled';
  })()`));
  await new Promise(z=>setTimeout(z,500));
  console.log(await ev('document.title'));
 });
 process.exit(0);
})().catch(e=>{console.log('ERR',e);process.exit(1)});
