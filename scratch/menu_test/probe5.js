const {withPage}=require('./cdp');
(async()=>{
 await withPage(8953,'page.html', async(ev,cmd)=>{
  // REAL trusted click on the button (like a user), then watch inline styles over time.
  const b=JSON.parse(await ev('JSON.stringify(document.getElementById("attachMenuBtn").getBoundingClientRect())'));
  await cmd('Input.dispatchMouseEvent',{type:'mousePressed',x:b.left+10,y:b.top+10,button:'left',clickCount:1,buttons:1});
  await cmd('Input.dispatchMouseEvent',{type:'mouseReleased',x:b.left+10,y:b.top+10,button:'left',clickCount:1,buttons:0});
  console.log('sync after click:', await ev(`JSON.stringify({top:document.getElementById('attachMenu').style.top,left:document.getElementById('attachMenu').style.left})`));
  await new Promise(z=>setTimeout(z,400));
  console.log('after 400ms:', await ev(`JSON.stringify({top:document.getElementById('attachMenu').style.top,left:document.getElementById('attachMenu').style.left})`));
 });
 process.exit(0);
})().catch(e=>{console.log('ERR',e);process.exit(1)});
