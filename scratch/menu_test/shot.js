const {withPage}=require('./cdp');
const fs=require('fs');
(async()=>{
 await withPage(8957,'page_final.html', async(ev,cmd)=>{
  // Open the menu with a project linked so all three items show.
  await ev('S.proj={linked:true}; S.uploadToProject=true; openAttachMenu();');
  await new Promise(z=>setTimeout(z,400));
  const shot = await cmd('Page.captureScreenshot',{format:'png'});
  if(shot.result && shot.result.data){ fs.writeFileSync('menu_open.png', Buffer.from(shot.result.data,'base64')); console.log('screenshot saved'); }
  else console.log('no screenshot', JSON.stringify(shot).slice(0,200));
 });
 process.exit(0);
})().catch(e=>{console.log('ERR',e);process.exit(1)});
