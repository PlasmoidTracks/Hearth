const {withPage}=require('./cdp');
(async()=>{
 await withPage(8941,'page.html', async(ev,cmd,logs)=>{
  const R=[];
  // Real user path: trusted mouse click on the 📎 button.
  let r=JSON.parse(await ev('JSON.stringify(document.getElementById("attachMenuBtn").getBoundingClientRect())'));
  await cmd('Input.dispatchMouseEvent',{type:'mousePressed',x:r.left+10,y:r.top+10,button:'left',clickCount:1,buttons:1});
  await cmd('Input.dispatchMouseEvent',{type:'mouseReleased',x:r.left+10,y:r.top+10,button:'left',clickCount:1,buttons:0});
  await new Promise(z=>setTimeout(z,300));
  R.push(['opens on button click', await ev('!document.getElementById("attachMenu").classList.contains("hidden")')]);
  const mr=JSON.parse(await ev('JSON.stringify(document.getElementById("attachMenu").getBoundingClientRect())'));
  const cs=JSON.parse(await ev('JSON.stringify(getComputedStyle(document.getElementById("attachMenu")).visibility)'));
  R.push(['visible rect w>0 h>0', mr.width>50 && mr.height>30]);
  R.push(['visibility', await ev('getComputedStyle(document.getElementById("attachMenu")).visibility')]);
  R.push(['above button', mr.bottom <= r.top+2]);
  R.push(['items rendered', await ev('Array.from(document.querySelectorAll("[data-am]")).map(b=>b.getAttribute("data-am")).join(",")')]);
  // proj item hidden when no project linked (it's in the list only if visible()) — S.proj unset here, so expect files,camera only.
  // Click "files" item via real mouse: menu closes and fileInput.click() fires.
  await ev('window.__pickerFired=false; HTMLInputElement.prototype.click=function(){ if(this.id==="fileInput") window.__pickerFired=true; };');
  r=JSON.parse(await ev('JSON.stringify(document.querySelector("[data-am=\\"files\\"]").getBoundingClientRect())'));
  await cmd('Input.dispatchMouseEvent',{type:'mousePressed',x:r.left+20,y:r.top+8,button:'left',clickCount:1,buttons:1});
  await cmd('Input.dispatchMouseEvent',{type:'mouseReleased',x:r.left+20,y:r.top+8,button:'left',clickCount:1,buttons:0});
  await new Promise(z=>setTimeout(z,300));
  R.push(['files item closes menu', await ev('document.getElementById("attachMenu").classList.contains("hidden")')]);
  R.push(['file picker fired', await ev('window.__pickerFired')]);
  // Project linked -> checkable item appears and toggles WITHOUT closing.
  await ev('S.proj={linked:true}; openAttachMenu();');
  await new Promise(z=>setTimeout(z,200));
  R.push(['proj item visible when linked', await ev('!!document.querySelector("[data-am=\\"proj_into\\"]")')]);
  r=JSON.parse(await ev('JSON.stringify(document.querySelector("[data-am=\\"proj_into\\"]").getBoundingClientRect())'));
  await cmd('Input.dispatchMouseEvent',{type:'mousePressed',x:r.left+20,y:r.top+8,button:'left',clickCount:1,buttons:1});
  await cmd('Input.dispatchMouseEvent',{type:'mouseReleased',x:r.left+20,y:r.top+8,button:'left',clickCount:1,buttons:0});
  await new Promise(z=>setTimeout(z,300));
  R.push(['toggle sets uploadToProject', await ev('S.uploadToProject===true')]);
  R.push(['toggle keeps menu open', await ev('!document.getElementById("attachMenu").classList.contains("hidden")')]);
  R.push(['checkmark rendered', await ev('document.getElementById("attachMenu").innerHTML.includes("\\u2713")')]);
  // Escape closes.
  await cmd('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape'});
  await new Promise(z=>setTimeout(z,200));
  R.push(['escape closes', await ev('document.getElementById("attachMenu").classList.contains("hidden")')]);
  // Outside real click closes.
  await cmd('Input.dispatchMouseEvent',{type:'mousePressed',x:r.left+10,y:r.top+10,button:'left',clickCount:1,buttons:1}); // reopen via button? r is item rect now; use button rect
  await new Promise(z=>setTimeout(z,200));
  const b=JSON.parse(await ev('JSON.stringify(document.getElementById("attachMenuBtn").getBoundingClientRect())'));
  if(await ev('document.getElementById("attachMenu").classList.contains("hidden")')){
    await cmd('Input.dispatchMouseEvent',{type:'mousePressed',x:b.left+10,y:b.top+10,button:'left',clickCount:1,buttons:1});
    await cmd('Input.dispatchMouseEvent',{type:'mouseReleased',x:b.left+10,y:b.top+10,button:'left',clickCount:1,buttons:0});
    await new Promise(z=>setTimeout(z,250));
  }
  R.push(['open before outside-click (sanity)', await ev('!document.getElementById("attachMenu").classList.contains("hidden")')]);
  await cmd('Input.dispatchMouseEvent',{type:'mousePressed',x:40,y:60,button:'left',clickCount:1,buttons:1});
  await cmd('Input.dispatchMouseEvent',{type:'mouseReleased',x:40,y:60,button:'left',clickCount:1,buttons:0});
  await new Promise(z=>setTimeout(z,250));
  R.push(['outside click closes', await ev('document.getElementById("attachMenu").classList.contains("hidden")')]);
  console.log(R.map(([k,v])=>(v?'PASS':'FAIL')+'  '+k+(typeof v==='boolean'?'':' ['+v+']')).join('\n'));
  console.log('CONSOLE:',JSON.stringify(logs));
 });
 process.exit(0);
})().catch(e=>{console.log('HARNESS ERR',e);process.exit(1)});
