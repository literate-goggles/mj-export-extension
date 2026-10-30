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
     if(!retry || n===this.maxAttempts-1){this.stop();throw new Error(`Read stopped after HTTP ${result.status}`);}
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
