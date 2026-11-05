// A run owns one scheduler. It must be explicitly started by the popup.
export class ReadScheduler {
 constructor({sleep=ms=>new Promise(r=>setTimeout(r,ms)),now=()=>Date.now(),maxAttempts=3}={}) {this.sleep=sleep;this.now=now;this.maxAttempts=maxAttempts;this.last=-Infinity;this.stopped=false;this.tail=Promise.resolve();}
 stop(){this.stopped=true;}
 api(operation){const task=this.tail.then(()=>this.attempt(operation,true));this.tail=task.catch(()=>{});return task;}
 async attempt(operation,isAPI=false){
   for(let n=0;n<this.maxAttempts;n++){
     if(this.stopped)throw new Error('Run paused');
     if(isAPI){await this.sleep(Math.max(0,1000-(this.now()-this.last)));this.last=this.now();}
     if(this.stopped)throw new Error('Run paused');
     let result;
     try { result=await operation(); } catch (error) { this.stop(); throw error; }
     if(result.error){this.stop();throw new Error(result.error);}
     if(result.status>=200&&result.status<300)return result;
     if(result.challenge || result.status===401){this.stop();throw new Error(result.challenge?'Site challenge or warning: stopped':'Session expired: sign in on Midjourney');}
     const retry=[403,429].includes(result.status)||result.status>=500;
     if(!retry || n===this.maxAttempts-1){if(result.item)return result;this.stop();throw new Error(`Read stopped after HTTP ${result.status}`);} // item: a failure of that one picture (a short link's), which the caller reports; the run goes on
     await this.sleep(Math.max(2000*2**n,Math.min(120000,Number(result.retryAfterMs)||0)));
   }
 }
}
export async function mapImages(items, operation, shouldStop=()=>false) {
 let next=0;const results=new Array(items.length);
 const settled=await Promise.allSettled(Array.from({length:Math.min(2,items.length)},async()=>{
  while(next<items.length&&!shouldStop()){const index=next++;results[index]=await operation(items[index],index);}
 }));
 const failure=settled.find(r=>r.status==='rejected');if(failure)throw failure.reason;
 return results;
}
export function safeRelativePath(path) {
 if(typeof path!=='string'||!path||path.length>220||path.startsWith('/')||path.split('/').some(p=>!p||p==='.'||p==='..')||/[\\\x00-\x1f<>:"|?*]/.test(path))throw new Error('Invalid export path');
 return path;
}
// A Midjourney short link, as moodboards made before about May 2026 store their pictures: https://s.mj.run/ and one plain code.
// src/page-bridge.js repeats the pattern (it is not bundled); tests/page-bridge.test.js checks that the two agree.
export function isShortLink(url) {
 return typeof url==='string'&&/^https:\/\/s\.mj\.run\/[A-Za-z0-9_-]{1,64}$/.test(url);
}
// The name Chrome saved a requested picture under, or null when that is not the requested name in the requested folder.
// Chrome may change the suffix to the type the server sent. saved: Chrome's absolute path (any separator), or a relative one.
export function savedName(requested, saved) {
 const parts=String(requested).split('/'),want=parts.pop(),dot=want.lastIndexOf('.'),stem=dot>0?want.slice(0,dot):want;
 const path=String(saved||'').replaceAll('\\','/'),name=path.slice(path.lastIndexOf('/')+1),tail=[...parts,name].join('/');
 return name.startsWith(stem+'.')&&/^[A-Za-z0-9]{1,8}$/.test(name.slice(stem.length+1))&&(path===tail||path.endsWith('/'+tail))?name:null;
}
