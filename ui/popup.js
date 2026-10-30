const $=id=>document.getElementById(id);
const options=()=>({mode:$('mode').value,max:Number($('max').value)||20,since:$('since').value,images:$('images').checked,testSubset:$('testSubset').checked});
for(const [command,label]of [['scan','Scan'],['export','Export'],['pause','Pause'],['resume','Resume'],['new','New export'],['folder','Open export folder']]){
 const b=document.createElement('button');b.textContent=label;b.onclick=async()=>{try{const r=await chrome.runtime.sendMessage({type:'ui-command',command,options:options()});$('status').textContent=r.error||'Command accepted. Detailed progress is also shown on the Midjourney page.';}catch(e){$('status').textContent=e.message;}};$('actions').append(b);
}
async function render(){const {run,scan}=await chrome.storage.local.get(['run','scan']);$('status').textContent=run?JSON.stringify({status:run.status,jobs:run.jobs.length,completed_jobs:run.done_ids.length,files:Object.keys(run.done_files).length,folder:run.folder,failures:run.failures},null,2):scan?JSON.stringify(scan,null,2):'Open your Organize page before starting.';}
chrome.storage.onChanged.addListener(render);
async function initialize(){const {run}=await chrome.storage.local.get('run');if(run?.options && run.status!=='complete')for(const [key,value]of Object.entries(run.options)){const field=$(key);if(field){if(field.type==='checkbox')field.checked=!!value;else field.value=value;}}await render();}
initialize();
