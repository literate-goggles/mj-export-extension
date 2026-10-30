import {safeRelativePath} from '../lib/transport.js';
let updates=Promise.resolve(),offscreenPromise;
const serialized=fn=>{const task=updates.then(fn);updates=task.catch(()=>{});return task;};
async function offscreen(){
 if(!offscreenPromise)offscreenPromise=(async()=>{if(!await chrome.offscreen.hasDocument())await chrome.offscreen.createDocument({url:'ui/offscreen.html',reasons:['BLOBS'],justification:'Create local JSONL download files without remote code'});})().catch(e=>{offscreenPromise=null;throw e;});
 return offscreenPromise;
}
async function download(url,filename){
 safeRelativePath(filename);
 if(!url.startsWith(`blob:chrome-extension://${chrome.runtime.id}/`)&&!url.startsWith('https://cdn.midjourney.com/'))throw new Error('Only local export blobs or the permitted image CDN may be downloaded');
 const id=await chrome.downloads.download({url,filename,saveAs:false,conflictAction:'overwrite'});
 return await new Promise((resolve,reject)=>{
  const end=async()=>{const [d]=await chrome.downloads.search({id});if(d?.state==='complete'){chrome.downloads.onChanged.removeListener(onChange);resolve({id,bytes:d.fileSize,filename:d.filename});}else if(d?.state==='interrupted'){chrome.downloads.onChanged.removeListener(onChange);reject(new Error(`Download interrupted: ${d.error||'unknown'}`));}};
  const onChange=d=>{if(d.id===id&&d.state)end().catch(reject);};chrome.downloads.onChanged.addListener(onChange);end().catch(reject);
 });
}
chrome.runtime.onMessage.addListener((message,sender,respond)=>{
 if(message.target==='offscreen')return;
 const ownTab=sender.url?.startsWith('https://www.midjourney.com/');
 const extension=sender.id===chrome.runtime.id&&sender.url?.startsWith(`chrome-extension://${chrome.runtime.id}/`);
 if(!ownTab&&!extension)return;
 (async()=>{
  if(message.type==='acquire')return serialized(async()=>{const {lease}=await chrome.storage.session.get('lease');if(lease&&lease!==sender.tab.id){try{const s=await chrome.tabs.sendMessage(lease,{type:'run-state'});if(s.active)throw new Error('Another MJ Export tab is already running');}catch(e){if(e.message.includes('already running'))throw e;}}await chrome.storage.session.set({lease:sender.tab.id});return {ok:true};});
  if(message.type==='release'){const {lease}=await chrome.storage.session.get('lease');if(lease===sender.tab.id)await chrome.storage.session.remove('lease');return {ok:true};}
  if(message.type==='schema')return serialized(async()=>{const {schemas={}}=await chrome.storage.local.get('schemas');schemas[message.record.path]=message.record;await chrome.storage.local.set({schemas});return {ok:true};});
  if(message.type==='text-file'){
   await offscreen();const {url}=await chrome.runtime.sendMessage({target:'offscreen',type:'blob',text:message.text});
   try{return await download(url,message.filename);}finally{await chrome.runtime.sendMessage({target:'offscreen',type:'revoke',url});}
  }
  if(message.type==='cdn-file'){try{const result=await download(message.url,message.filename);return {status:200,...result};}catch(e){const reason=e.message;return {status:/FORBIDDEN|UNAUTHORIZED/.test(reason)?403:/SERVER_FAILED|NETWORK/.test(reason)?503:/BAD_CONTENT/.test(reason)?404:0,error_detail:reason};}}
  if(message.type==='check-file'){const [d]=await chrome.downloads.search({id:message.id});return {complete:d?.state==='complete'&&d.exists,bytes:d?.fileSize};}
  if(message.type==='open-folder'){const {run}=await chrome.storage.local.get('run');if(run?.lastDownloadId)await chrome.downloads.show(run.lastDownloadId);else await chrome.downloads.showDefaultFolder();return {ok:true};}
  if(message.type==='ui-command'){
   const tabs=await chrome.tabs.query({url:'https://www.midjourney.com/*'});
   const selected=tabs.find(t=>t.active&&/^https:\/\/www.midjourney.com\/(organize|imagine|personalize|moodboards)(\/|$)/.test(t.url))||tabs.find(t=>/^https:\/\/www.midjourney.com\/(organize|imagine|personalize|moodboards)(\/|$)/.test(t.url));
   if(!selected)throw new Error('Open your Midjourney Organize page first');
   return chrome.tabs.sendMessage(selected.id,{type:'command',command:message.command,options:message.options});
  }
  throw new Error('Unsupported extension action');
 })().then(respond,e=>respond({error:e.message}));return true;
});
