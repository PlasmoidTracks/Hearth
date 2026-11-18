const {withPage}=require('./cdp');
(async()=>{
 await withPage(8955,'page.html', async(ev,cmd)=>{
  const b=JSON.parse(await ev('JSON.stringify(document.getElementById("attachMenuBtn").getBoundingClientRect())'));
  await cmd('Input.dispatchMouseEvent',{type:'mousePressed',x:b.left+10,y:b.top+10,button:'left',clickCount:1,buttons:1});
  await cmd('Input.dispatchMouseEvent',{type:'mouseReleased',x:b.left+10,y:b.top+10,button:'left',clickCount:1,buttons:0});
  await new Promise(z=>setTimeout(z,400));
  console.log(await ev(`(()=>{const m=document.getElementById('attachMenu');
    const mr=m.getBoundingClientRect();
    return JSON.stringify({inlineTop:m.style.top, inlineLeft:m.style.left,
      rect:[Math.round(mr.left),Math.round(mr.top),Math.round(mr.width),Math.round(mr.height)],
      offsetParent: m.offsetParent && (m.offsetParent.id || m.offsetParent.tagName),
      position:getComputedStyle(m).position});})()`));
 });
 process.exit(0);
})().catch(e=>{console.log('ERR',e);process.exit(1)});
