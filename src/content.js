(() => {
 const pending=new Map();let panel,active=false,paused=false,scheduler,run,saveTail=Promise.resolve();
 const allowedPage=()=>/^\/(organize|imagine|personalize|moodboards)(\/|$)/.test(location.pathname);
 const rpc=(op,args={})=>new Promise((resolve,reject)=>{const id=crypto.randomUUID();const timeout=setTimeout(()=>{pending.delete(id);reject(new Error('Page read timed out'));},90000);pending.set(id,result=>{clearTimeout(timeout);resolve(result);});window.postMessage({source:'mj-export-request',id,op,args},location.origin);});
 const worker=async message=>{const r=await chrome.runtime.sendMessage(message);if(r?.error)throw new Error(r.error);return r;};
 window.addEventListener('message',event=>{
  if(event.source!==window||event.origin!==location.origin)return;
  if(event.data?.source==='mj-export-response'){const fn=pending.get(event.data.id);if(fn){pending.delete(event.data.id);fn(event.data.result);}}
  if(event.data?.source==='mj-export-schema'&&allowedPage())worker({type:'schema',record:event.data.record}).then(render).catch(()=>{});
 });
 const save=()=>{const task=saveTail.then(()=>chrome.storage.local.set({run:JSON.parse(JSON.stringify(run))}));saveTail=task.catch(()=>{});return task;};
 function opts(){return {mode:panel.querySelector('[name=mode]').value,max:Math.max(1,Number(panel.querySelector('[name=max]').value)||20),since:panel.querySelector('[name=since]').value,images:panel.querySelector('[name=images]').checked,testSubset:panel.querySelector('[name=testSubset]').checked};}
 function status(text){if(panel)panel.querySelector('[data-status]').textContent=text;}
 function verifyPage(){if(!allowedPage())throw new Error('Keep your own Organize page open while exporting');if(paused)throw new Error('Run paused');}
 async function api(op,args){verifyPage();const r=await scheduler.api(()=>rpc(op,args));if(r.error)throw new Error(r.error);return r.data;}
 function failure(kind,id,reason){if(!run.failures.some(f=>f.kind===kind&&f.id===id&&f.reason===reason))run.failures.push({kind,id,reason});}
 async function collect(options){
  const jobs=[];let cursor=null,checkpoint=null;const seen=new Set(),cursors=new Set();
  do{
   verifyPage();status(`Reading own jobs: ${jobs.length}`);
   const size=options.mode==='max'?Math.min(100,Math.max(1,options.max-jobs.length)):100;
   const page=await api('jobs',{size,cursor,checkpoint});
   for(const raw of page.data){
    if(seen.has(raw.id))continue;seen.add(raw.id);
    if(options.mode==='since'&&new Date(raw.enqueue_time)<new Date(options.since))continue;
    if(options.mode==='max'&&jobs.length>=options.max)break;
    jobs.push(normalizeJob(raw));
   }
   cursor=page.cursor;checkpoint=page.checkpoint;
   if(run){run.cursor=cursor;run.checkpoint=checkpoint;run.discovered_ids=[...seen];await save();}
   if(options.mode==='max'&&jobs.length>=options.max)break;
   if(cursor&&cursors.has(cursor))throw new Error('Pagination cursor repeated; stopped to avoid looping');
   if(cursor)cursors.add(cursor);
  }while(cursor);
  return {jobs,exhausted:!cursor};
 }
 async function getData(options){
  const identity=await api('identity');
  if(!identity?.username_v2||!identity.user_id)throw new Error('Current account identity is unavailable');
  if(run&&run.identity.user_id!==identity.user_id)throw new Error('Account changed: start a new export explicitly');
  const result=await collect(options);
  const boards=normalizeBoards(await api('moodboards'),result.jobs);
  const profiles=normalizeProfiles(await api('profiles'),identity,result.jobs);
  return {identity,...result,boards,profiles};
 }
 function validateOptions(options){if(options.mode==='max'&&(!Number.isSafeInteger(options.max)||options.max<1))throw new Error('Maximum jobs must be a positive integer');if(options.mode==='since'&&(!options.since||!Number.isFinite(Date.parse(options.since))))throw new Error('Choose a valid since date');if(!['max','since','all'].includes(options.mode))throw new Error('Invalid scope');}
 async function scan(options){
  if(active)throw new Error('A run is already active in this tab');validateOptions(options);active=true;try{await worker({type:'acquire'});}catch(error){active=false;throw error;}paused=false;scheduler=new ReadScheduler();const savedRun=run;run=null;
  try{const data=await getData(options);const available={moodboards:data.boards.length,profiles:data.profiles.length};if(options.testSubset){data.boards=data.boards.slice(0,1);data.profiles=data.profiles.slice(0,1);}const images=data.jobs.reduce((n,j)=>n+j.image_urls.length,0)+data.boards.reduce((n,b)=>n+b.images.length,0);const summary={scope:options,available,jobs:data.jobs.length,moodboards:data.boards.length,profiles:data.profiles.length,feed_exhausted:data.exhausted,estimated_bytes:images*1024*1024,estimate_basis:'Rough estimate: 1 MiB per image, not measured; no images fetched'};await chrome.storage.local.set({scan:summary});status(JSON.stringify(summary,null,2));}
  finally{active=false;run=savedRun;await worker({type:'release'});}
 }
 async function image(url,file){
  verifyPage();const prior=run.done_files[file];
  if(prior&&(await worker({type:'check-file',id:prior.id})).complete)return {file:prior.file||file,url:prior.source_url||url};
  if(!url.startsWith('https://cdn.midjourney.com/')){failure('image',file,'Source is outside the two permitted hosts; not downloaded');return null;}
  let source=url,actualFile=file;
  const suffix=new URL(url).pathname.match(/\.(png|jpe?g|webp)$/i)?.[1];if(suffix)actualFile=file.replace(/\.(jpeg|png|webp)$/,'.'+suffix.toLowerCase());
  const response=await scheduler.attempt(async()=>{
   const permitted=await rpc('authorize-image',{url:source});if(permitted.status!==200)return permitted;
   let result=await worker({type:'cdn-file',url:source,filename:`${run.folder}/${actualFile}`});
   if(result.status===404&&/\/0_\d\.jpeg$/.test(source)){
    source=source.replace(/\.jpeg$/,'.png');actualFile=file.replace(/\.jpeg$/,'.png');
    const allowed=await rpc('authorize-image',{url:source});if(allowed.status!==200)return allowed;
    result=await worker({type:'cdn-file',url:source,filename:`${run.folder}/${actualFile}`});
   }
   if(result.status===0)throw new Error(result.error_detail||'Download failed');return result;
  });
  run.done_files[file]={...response,file:actualFile,source_url:source};run.lastDownloadId=response.id;await save();return {file:actualFile,url:source};
 }
 async function writeMetadata(){
  if(!run)return;
  const likes=run.jobs.filter(j=>j.image_likes?.length).map(j=>({job_id:j.job_id,image_likes:j.image_likes}));
  const metadata={extension_version:chrome.runtime.getManifest().version,export_time:new Date().toISOString(),username:run.identity.username_v2,scope:run.options,status:run.status,counts:{jobs:run.jobs.length,moodboards:run.boards.length,profiles:run.profiles.length,likes:likes.length,files:Object.keys(run.done_files).length,completed_jobs:run.done_ids.length,download_bytes:Object.values(run.done_files).reduce((sum,f)=>sum+(f.bytes||0),0)},failures:run.failures,parent_gaps:parentGaps(run.jobs),feed_exhausted:run.exhausted,limitations:['Likes cover the selected own jobs only.','Profile codes are available only when present in exported job references or profile records.','Missing version and creation fields are null; image dimensions are API metadata.']};
  const files={'jobs.jsonl':run.jobs,'moodboards.jsonl':run.boards,'profiles.jsonl':run.profiles,'likes.jsonl':likes};
  for(const [name,rows]of Object.entries(files)){const result=await worker({type:'text-file',filename:`${run.folder}/${name}`,text:rows.map(r=>JSON.stringify(r)).join('\n')+(rows.length?'\n':'')});run.lastDownloadId=result.id;}
  const result=await worker({type:'text-file',filename:`${run.folder}/export_meta.json`,text:JSON.stringify(metadata,null,2)+'\n'});run.lastDownloadId=result.id;run.local_folder=result.filename.replace(/[\\/][^\\/]+$/,'');await save();
  status(JSON.stringify({...metadata,folder:run.folder,local_folder:run.local_folder},null,2));
 }
 async function start(options,newRun=false){
  if(active)throw new Error('A run is already active in this tab');validateOptions(options);active=true;try{await worker({type:'acquire'});}catch(error){active=false;throw error;}paused=false;scheduler=new ReadScheduler();let writable=false;
  try{
   const stored=(await chrome.storage.local.get('run')).run;
   run=newRun?null:stored;
   if(run){
    if(run.options.mode!==options.mode||run.options.max!==options.max||run.options.since!==options.since||run.options.testSubset!==options.testSubset)throw new Error('Scope differs from saved progress. Use New export to start a separate run.');
    run.options.images=options.images;run.failures=[];
   }
   const identity=await api('identity');
   if(run&&run.identity.user_id!==identity.user_id)throw new Error('Account differs from saved export. Use New export.');
   if(!run){const name=identity.username_v2.replace(/[^a-zA-Z0-9_.-]/g,'_');const date=new Date().toISOString().slice(0,10).replaceAll('-','');run={identity,folder:`mj_export_${name}_${date}${newRun?'_'+Date.now():''}`,options,done_ids:[],done_files:{},jobs:[],boards:[],profiles:[],failures:[],cursor:null,checkpoint:null};}
   writable=true;run.status='reading';await save();
   const data=await getData(options);
   // Revalidate ownership on every explicit resume before using saved download progress.
   const existing=new Map(run.jobs.map(j=>[j.job_id,j]));
   run.jobs=data.jobs.map(j=>({...j,image_files:existing.get(j.job_id)?.image_files||[]}));
   run.boards=options.testSubset?data.boards.slice(0,1):data.boards;
   run.profiles=options.testSubset?data.profiles.slice(0,1):data.profiles;
   if(options.images)run.done_ids=run.done_ids.filter(id=>run.jobs.find(j=>j.job_id===id)?.image_files.length>0);
   run.exhausted=data.exhausted;run.status='exporting';await save();
   for(const job of run.jobs){
    verifyPage();status(`Exporting ${run.done_ids.length}/${run.jobs.length} jobs; ${Object.keys(run.done_files).length} files`);
    if(job.video){failure('job',job.job_id,'Video media export is not implemented');continue;}
    if(options.images){
      const results=await mapImages(job.image_urls,async(url,i)=>image(url,`images/${job.job_id}_${i}.jpeg`),()=>paused||scheduler.stopped);
      job.image_files=results.filter(Boolean).map(r=>r.file);results.forEach((r,i)=>{if(r)job.image_urls[i]=r.url;});
      if(job.image_files.length!==job.image_urls.length)throw new Error('Not all job images completed');
    }
    if(!run.done_ids.includes(job.job_id))run.done_ids.push(job.job_id);await save();
   }
   if(options.images)for(const board of run.boards){
    const results=await mapImages(board.images,async(img,i)=>image(img.source_url,`images/moodboard_${board.id}_${i}.jpeg`),()=>paused||scheduler.stopped);
    results.forEach((r,i)=>{if(r)board.images[i].file=r.file;});await save();verifyPage();
   }
   run.status=run.failures.length?'completed_with_failures':'complete';await save();await writeMetadata();
  }catch(error){
   if(run&&writable){run.status=paused?'paused':'stopped';failure('run','export',error.message);await save();await writeMetadata().catch(e=>status(`Export stopped: ${error.message}. Metadata save failed: ${e.message}`));}
   else status(error.message);
  }finally{active=false;await worker({type:'release'});}
 }
 async function command(name,options){
  if(name==='pause'){paused=true;scheduler?.stop();status('Pausing after current downloads finish');return;}
  if(name==='folder')return worker({type:'open-folder'});
  if(name==='scan')return scan(options);
  if(name==='resume'){const saved=(await chrome.storage.local.get('run')).run;if(!saved)throw new Error('No saved export to resume');return start(saved.options);}
  if(name==='export'||name==='new')return start(options,name==='new');
 }
 chrome.runtime.onMessage.addListener((m,sender,respond)=>{
  if(m.type==='run-state'){respond({active});return;}
  if(m.type==='command'){
   if(active&&!['pause','folder'].includes(m.command)){respond({error:'Run already active'});return;}
   command(m.command,m.options||opts()).catch(e=>status(e.message));respond({ok:true});
  }
 });
 async function render(){
  if(!allowedPage()||!document.body)return;
  if(!panel){
   panel=document.createElement('details');panel.id='mj-export-panel';
   panel.style.cssText='position:fixed;bottom:12px;right:12px;z-index:2147483647;width:440px;max-height:80vh;overflow:auto;background:#111827;color:#f8fafc;padding:14px;border:1px solid #64748b;border-radius:12px;font:13px system-ui;box-shadow:0 8px 30px #0008';
   panel.innerHTML='<summary style="cursor:pointer;font-weight:700">MJ Export · Local export</summary><p>Your own work only. Keep this tab open.</p><label>Scope <select name="mode"><option value="max">Max N jobs</option><option value="since">Since date</option><option value="all">All jobs</option></select></label> <label>N <input name="max" type="number" min="1" value="20" style="width:70px"></label><br><label>Since <input type="date" name="since"></label><p><label><input type="checkbox" name="images"> Include images</label><br><label><input type="checkbox" name="testSubset" checked> Small test: one board and one profile</label></p><div data-actions></div><pre data-status style="white-space:pre-wrap;max-height:280px;overflow:auto">Ready. Start with metadata only.</pre><details><summary>API field names</summary><pre data-schema style="white-space:pre-wrap"></pre></details>';
   for(const [name,label]of [['scan','Scan'],['export','Export / resume'],['pause','Pause'],['new','New export'],['folder','Open export folder']]){const b=document.createElement('button');b.type='button';b.textContent=label;b.style.cssText='padding:7px;margin:3px;color:#111;background:#e2e8f0;border-radius:5px;border:0';b.onclick=()=>command(name,opts()).catch(e=>status(e.message));panel.querySelector('[data-actions]').append(b);}
   document.body.append(panel);
   const saved=(await chrome.storage.local.get('run')).run;
   if(saved?.options){for(const [key,value]of Object.entries(saved.options)){const field=panel.querySelector(`[name="${key}"]`);if(field){if(field.type==='checkbox')field.checked=!!value;else field.value=value;}}status(`Saved export: ${saved.status}. Export / resume continues these settings; New export starts a different scope.`);}
  }
  const {schemas={}}=await chrome.storage.local.get('schemas');panel.querySelector('[data-schema]').textContent=JSON.stringify(schemas,null,2);
 }
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',render,{once:true});else render();
})();
