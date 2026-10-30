const urls=new Set();
chrome.runtime.onMessage.addListener((m,sender,respond)=>{
 if(sender.id!==chrome.runtime.id||m.target!=='offscreen')return;
 if(m.type==='blob'){const url=URL.createObjectURL(new Blob([m.text],{type:'application/octet-stream'}));urls.add(url);respond({url});}
 if(m.type==='revoke'&&urls.has(m.url)){URL.revokeObjectURL(m.url);urls.delete(m.url);respond({ok:true});}
});
