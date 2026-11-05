import {safeRelativePath,isShortLink,savedName} from '../lib/transport.js';
let updates=Promise.resolve(),offscreenPromise;
const serialized=fn=>{const task=updates.then(fn);updates=task.catch(()=>{});return task;};
async function offscreen(){
 if(!offscreenPromise)offscreenPromise=(async()=>{if(!await chrome.offscreen.hasDocument())await chrome.offscreen.createDocument({url:'ui/offscreen.html',reasons:['BLOBS'],justification:'Create local JSONL download files without remote code'});})().catch(e=>{offscreenPromise=null;throw e;});
 return offscreenPromise;
}
// Where a short link may lead: the picture types the viewer shows (not SVG, which can carry scripts).
const PICTURE_TYPES=new Set(['image/jpeg','image/jpg','image/pjpeg','image/png','image/webp','image/gif','image/avif']);
// Chrome's verdicts that need nobody's decision; any other one ends a short link's download.
const CALM=new Set(['safe','accepted','allowlistedByPolicy','deepScannedSafe','asyncScanning','asyncLocalPasswordScanning']);
const SHORT_LINK_WAIT_MS=120000,POLL_MS=2000;
// A short link's picture that is not kept: the export reports it and goes on. retry: answered as a server failure, which is retried first.
class PictureFailure extends Error{constructor(message,retry=false){super(message);this.retry=retry;}}
class Interrupted extends Error{constructor(code,at){super(`Download interrupted: ${code}`);this.code=code;this.at=at;}}
const at=d=>{try{return d?.finalUrl?` at ${new URL(d.finalUrl).host}`:'';}catch{return '';}};
// Why a short link's download is not kept, as the failure to answer with; null while nothing is wrong. types: every type Chrome reported for it. Once
// Chrome has named the file it reports the saved suffix's type (image/jpeg for a .jpeg it kept), so the server's own type may show only early on.
function refused(d,types){
 const final=String(d.finalUrl||''),other=[...types].find(type=>!PICTURE_TYPES.has(type));
 if((final||d.state==='complete')&&!final.startsWith('https://'))return new PictureFailure(`Short link led to a non-HTTPS address${at(d)}; not kept`);
 // Refused as soon as it shows, before the file is complete (Chrome may open a completed file of a type the user chose to always open). A server's error
 // page shows here as its type, as Chrome reports its error code only once it has named the file, so this refusal is retried like a server error.
 if(other!==undefined||d.state==='complete'&&!types.size)return new PictureFailure(`Short link led to ${other===undefined?'a response with no type':other.length>100?other.slice(0,100)+'…':other}${at(d)}, not a JPEG, PNG, WebP, GIF or AVIF picture; not kept`,true); // a server's type, shortened: the export keeps the reason
 if(d.danger&&!CALM.has(d.danger))return new PictureFailure(`Chrome flagged the short link's download as dangerous (${d.danger})${at(d)}; cancelled`);
 return null;
}
// Ends a short link's download that is not kept: cancelled while it runs, its file removed once complete, its entry erased (which also deletes the partial file Chrome keeps to resume an interrupted one).
async function discard(id){
 await chrome.downloads.cancel(id).catch(()=>{});
 const [d]=await chrome.downloads.search({id}).catch(()=>[]);
 if(d?.state==='complete')await chrome.downloads.removeFile(id).catch(()=>{});
 await chrome.downloads.erase({id}).catch(()=>{});
}
// picture: an image, which must be saved under the requested name in the export folder, though Chrome may change the suffix.
async function download(url,filename,picture=false){
 safeRelativePath(filename);const short=isShortLink(url);
 if(!url.startsWith(`blob:chrome-extension://${chrome.runtime.id}/`)&&!url.startsWith('https://cdn.midjourney.com/')&&!short)throw new Error('Only local export blobs, the Midjourney image CDN or Midjourney short links may be downloaded');
 return await new Promise((resolve,reject)=>{
  let id=null,ended=false,last=null,timer=null,poll=null;const types=new Set(),early=[];
  const note=mime=>{const type=String(mime||'').split(';')[0].trim().toLowerCase();if(type)types.add(type);};
  const end=()=>{ended=true;clearTimeout(timer);clearInterval(poll);chrome.downloads.onChanged.removeListener(onChange);};
  const fail=error=>{if(!ended){end();reject(error);}};
  const drop=error=>{if(!ended){end();(id===null?Promise.resolve():discard(id)).finally(()=>reject(error));}};
  const check=async()=>{
   if(ended||id===null)return;const [d]=await chrome.downloads.search({id});if(ended||!d)return;last=d;note(d.mime);
   if(d.state==='interrupted'){const error=new Interrupted(d.error||'unknown',at(d));return short?drop(error):fail(error);}
   const problem=short&&refused(d,types);if(problem)return drop(problem);
   if(d.state!=='complete')return;
   if(picture&&!savedName(filename,d.filename))return fail(new Error(`Chrome saved ${filename} as ${String(d.filename||'').split(/[\\/]/).pop()||'nothing'}, not under that name in the export folder; another extension may be renaming downloads, or another name or folder was chosen in Chrome's save dialog (setting "Ask where to save each file before downloading")`));
   const u=short&&new URL(d.finalUrl);end(); // read before the wait ends, so that a throw still answers
   resolve({id,bytes:d.fileSize,filename:d.filename,...(short?{final_url:u.origin+u.pathname}:{})});
  };
  // A change of type carries the one before it; changes that come before Chrome answers with the id are kept until then.
  const onChange=delta=>{if(id===null){if(short)early.push(delta);return;}if(delta.id!==id)return;note(delta.mime?.previous);note(delta.mime?.current);if(short||delta.state)check().catch(fail);};chrome.downloads.onChanged.addListener(onChange);
  // A short link may lead anywhere, so it gets a time limit, from before Chrome creates the download (which waits for the response to begin).
  if(short)timer=setTimeout(()=>drop(new PictureFailure(`Short link download did not finish within 2 minutes${at(last)}; cancelled`)),SHORT_LINK_WAIT_MS);
  chrome.downloads.download({url,filename,saveAs:false,conflictAction:'overwrite'}).then(started=>{
   id=started;if(ended){discard(started);return;}
   for(const delta of early.splice(0))if(delta.id===id){note(delta.mime?.previous);note(delta.mime?.current);}
   if(short)poll=setInterval(()=>check().catch(fail),POLL_MS); // polling also keeps this worker awake while it waits
   check().catch(fail);
  },error=>fail(/^[A-Z_]+$/.test(error?.message)?new Interrupted(error.message,''):error)); // Chrome's error code when it could not start the download, mapped as an interruption
 });
}
chrome.runtime.onMessage.addListener((message,sender,respond)=>{
 if(message.target==='offscreen')return;
 const ownTab=sender.url?.startsWith('https://www.midjourney.com/');
 const extension=sender.id===chrome.runtime.id&&sender.url?.startsWith(`chrome-extension://${chrome.runtime.id}/`);
 if(!ownTab&&!extension)return;
 (async()=>{
  if(message.type==='acquire')return serialized(async()=>{const {lease}=await chrome.storage.session.get('lease');if(lease&&lease!==sender.tab.id){const s=await chrome.tabs.sendMessage(lease,{type:'run-state'}).catch(()=>null);if(s?.choosing)throw new Error('Finish choosing first: Save or Cancel the open choice in your other Midjourney tab.');if(s?.active)throw new Error('Another MJ Export tab is already running');}await chrome.storage.session.set({lease:sender.tab.id});return {ok:true};});
  if(message.type==='release'){const {lease}=await chrome.storage.session.get('lease');if(lease===sender.tab.id)await chrome.storage.session.remove('lease');return {ok:true};}
  if(message.type==='schema')return serialized(async()=>{const {schemas={}}=await chrome.storage.local.get('schemas');schemas[message.record.path]=message.record;await chrome.storage.local.set({schemas});return {ok:true};});
  if(message.type==='text-file'){
   await offscreen();const {url}=await chrome.runtime.sendMessage({target:'offscreen',type:'blob',text:message.text});
   try{return await download(url,message.filename);}finally{await chrome.runtime.sendMessage({target:'offscreen',type:'revoke',url});}
  }
  if(message.type==='cdn-file'){
   try{return {status:200,...await download(message.url,message.filename,true)};}
   catch(e){
    const reason=e.message,code=e instanceof Interrupted?e.code:'',status=/FORBIDDEN|UNAUTHORIZED/.test(code)?403:/SERVER_FAILED|NETWORK/.test(code)?503:/BAD_CONTENT/.test(code)?404:0; // from Chrome's code only: other messages can name the folder and the saved file
    if(e instanceof PictureFailure)return {status:e.retry?503:0,item:true,error_detail:reason};
    // A short link that no longer works, or whose server refuses it, fails that picture alone; local and user errors still stop the run.
    if(e instanceof Interrupted&&isShortLink(message.url)&&/^(SERVER_|NETWORK_|FILE_(VIRUS_INFECTED|BLOCKED|SECURITY_CHECK_FAILED|TOO_LARGE)$)/.test(e.code))return {status,item:true,error_detail:`Short link could not be downloaded: Chrome reported ${e.code}${e.at}`};
    return {status,error_detail:reason};
   }
  }
  if(message.type==='check-file'){const [d]=await chrome.downloads.search({id:message.id});return {complete:d?.state==='complete'&&d.exists,bytes:d?.fileSize,filename:d?.filename};}
  if(message.type==='open-folder'){const {run}=await chrome.storage.local.get('run');if(run?.lastDownloadId)await chrome.downloads.show(run.lastDownloadId);else await chrome.downloads.showDefaultFolder();return {ok:true};}
  if(message.type==='ui-command'){
   const tabs=await chrome.tabs.query({url:'https://www.midjourney.com/*'});
   const selected=tabs.find(t=>t.active&&/^https:\/\/www.midjourney.com\/(organize|imagine|personalize|moodboards)(\/|$)/.test(t.url))||tabs.find(t=>/^https:\/\/www.midjourney.com\/(organize|imagine|personalize|moodboards)(\/|$)/.test(t.url));
   if(!selected)throw new Error('Open your Midjourney Organize page first');
   if(message.command==='choose'){await chrome.tabs.update(selected.id,{active:true}).catch(()=>{});await chrome.windows.update(selected.windowId,{focused:true}).catch(()=>{});}
   return chrome.tabs.sendMessage(selected.id,{type:'command',command:message.command,options:message.options});
  }
  throw new Error('Unsupported extension action');
 })().then(respond,e=>respond({error:e.message}));return true;
});
