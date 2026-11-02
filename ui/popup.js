const $=id=>document.getElementById(id);
const options=()=>({mode:$('mode').value,max:Number($('max').value)||20,since:$('since').value,images:$('images').checked,testSubset:$('testSubset').checked});
// Clearing needs no Midjourney tab; choosing opens the chooser on the Midjourney page.
async function clearSelection(){try{await chrome.storage.local.remove('selection');$('status').textContent='Selection cleared. New exports include everything in the scope again.';}catch(e){$('status').textContent=e.message;}}
for(const [command,label,place]of [['choose','Choose what to export','choice'],['clear','Clear selection','choice'],['scan','Scan','actions'],['export','Export','actions'],['pause','Pause','actions'],['resume','Resume','actions'],['new','New export','actions'],['folder','Open export folder','actions']]){
 const b=document.createElement('button');b.textContent=label;b.onclick=command==='clear'?clearSelection:async()=>{try{const r=await chrome.runtime.sendMessage({type:'ui-command',command,options:options()});$('status').textContent=r.error||(command==='choose'?'The chooser opens on the Midjourney page. It scans first when this scope has no scan yet.':'Command accepted. Detailed progress is also shown on the Midjourney page.');}catch(e){$('status').textContent=e.message;}};$(place).append(b);
}
async function render(){const {run,scan,selection}=await chrome.storage.local.get(['run','scan','selection']);$('selection').textContent=selectionLine(selection,run);$('status').textContent=run?JSON.stringify({status:run.status,jobs:run.jobs.length,completed_jobs:run.done_ids.length,files:Object.keys(run.done_files).length,folder:run.folder,failures:run.failures},null,2):scan?JSON.stringify(scan,null,2):'Open your Organize page before starting.';}
chrome.storage.onChanged.addListener(render);
async function initialize(){const {run}=await chrome.storage.local.get('run');if(run?.options && run.status!=='complete')for(const [key,value]of Object.entries(run.options)){const field=$(key);if(field){if(field.type==='checkbox')field.checked=!!value;else field.value=value;}}await render();}
initialize();
