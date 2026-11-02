// In-page chooser for what an export includes. It lives in a closed shadow root and puts data into the page only as text.
(() => {
 const PAGE=120,CDN='https://cdn.midjourney.com',JOB_ID=/^[a-f0-9-]{36}$/i;
 const CSS=`
:host{all:initial}
*,*::before,*::after{box-sizing:border-box}
[hidden]{display:none!important}
.backdrop{position:fixed;inset:0;display:flex;padding:24px;background:rgba(2,6,23,.78);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:#f1f5f9;color-scheme:dark;text-align:left;letter-spacing:normal}
.dialog{flex:1;min-width:0;display:flex;flex-direction:column;background:#111827;border:1px solid #475569;border-radius:14px;box-shadow:0 24px 64px rgba(0,0,0,.55);overflow:hidden}
h2,h3,h4,p,ul{margin:0}
h2{font-size:18px;font-weight:700}
h3{font-size:17px;font-weight:700;overflow-wrap:anywhere}
h4{font-size:14px;font-weight:600}
.muted{color:#94a3b8}
.top{display:flex;align-items:flex-start;gap:16px;padding:16px 20px;border-bottom:1px solid #1e293b}
.heading{flex:1;min-width:0}
.heading .muted{margin-top:2px;font-size:13px}
.bar,.buttons,.confirm{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.note{padding:8px 20px;background:#1e293b;border-bottom:1px solid #334155;color:#fde68a;font-size:13px}
.body{flex:1;min-height:0;display:grid;grid-template-columns:minmax(220px,300px) minmax(0,1fr)}
.side{min-height:0;overflow:auto;overscroll-behavior:contain;padding:12px 8px;border-right:1px solid #1e293b}
.side-title{padding:0 10px 6px;color:#94a3b8;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.06em}
.groups{list-style:none;padding:0;display:grid;gap:2px}
.groups+.groups{margin-top:8px;padding-top:8px;border-top:1px solid #1e293b}
.group{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:8px}
.group:hover{background:#172033}
.group.current{background:#1e293b;box-shadow:inset 3px 0 0 #38bdf8}
.tick{display:flex}
.pick{flex:1;min-width:0;display:grid;gap:1px;padding:0;border:0;background:none;color:inherit;font:inherit;text-align:left;cursor:pointer}
.pick .name{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pick .muted{font-size:12px}
.side .empty{padding:4px 10px 10px;font-size:13px}
.main{min-height:0;overflow:auto;overscroll-behavior:contain;padding:18px 20px 28px}
.pane-head{margin-bottom:14px}
.pane-head .muted{margin-top:2px;font-size:13px}
.refs{margin-bottom:20px;padding:12px;border:1px solid #334155;border-radius:10px;background:#0f172a}
.refs.off .ref-grid{opacity:.4}
.option{display:flex;align-items:center;gap:10px;font-weight:600;cursor:pointer}
.ref-grid{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
.ref{display:flex;width:72px;height:72px;border-radius:6px;overflow:hidden;background:#1e293b}
.gens-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px}
.grow{flex:1}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px}
.card{position:relative;display:flex;flex-direction:column;border:1px solid #475569;border-radius:10px;background:#0f172a;overflow:hidden;cursor:pointer}
.card:hover{border-color:#94a3b8}
.card:focus-within{outline:2px solid #38bdf8;outline-offset:2px}
.card .box{position:absolute;top:6px;left:6px;z-index:1;display:flex;padding:4px;border-radius:6px;background:rgba(15,23,42,.85)}
.card.off{border-color:#1e293b;background:#0b1220}
.card.off .thumb,.card.off .text{opacity:.42}
.thumb{display:flex;aspect-ratio:1;background:#1e293b}
.thumb img,.ref img{display:block;width:100%;height:100%;object-fit:cover}
.text{display:grid;gap:4px;padding:8px 10px 10px}
.date{color:#94a3b8;font-size:12px}
.prompt{display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;overflow-wrap:anywhere;font-size:12.5px;line-height:1.35}
.ph{flex:1;display:flex;align-items:center;justify-content:center;padding:4px;background:repeating-linear-gradient(135deg,#1e293b 0 10px,#172033 10px 20px);color:#94a3b8;font-size:12px;text-align:center}
.main .empty{padding:20px 0;color:#94a3b8}
.more{display:block;margin:18px auto 0}
.bottom{display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:12px 20px;border-top:1px solid #1e293b;background:#0f172a}
.totals{flex:1;min-width:220px}
.summary{font-weight:600}
.totals .muted{margin-top:2px;font-size:12.5px}
button{font:inherit;font-size:13px;line-height:1.3;padding:6px 12px;border:1px solid #475569;border-radius:6px;background:transparent;color:#e2e8f0;cursor:pointer}
button:hover{background:#1e293b}
button:disabled{opacity:.45;cursor:default}
button.primary{border-color:#e2e8f0;background:#e2e8f0;color:#0f172a;font-weight:700}
button.primary:hover{background:#f8fafc}
button.danger{border-color:#fca5a5;background:#fca5a5;color:#450a0a;font-weight:700}
input[type=checkbox]{flex:none;width:18px;height:18px;margin:0;accent-color:#38bdf8;cursor:pointer}
input[type=checkbox]:disabled{cursor:default}
:focus-visible{outline:2px solid #38bdf8;outline-offset:2px}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
@media (max-width:760px){.backdrop{padding:0}.dialog{border-radius:0}.top{flex-wrap:wrap}.body{grid-template-columns:minmax(0,1fr);grid-template-rows:auto minmax(0,1fr)}.side{max-height:34vh;border-right:0;border-bottom:1px solid #1e293b}.grid{grid-template-columns:repeat(auto-fill,minmax(130px,1fr))}}
`;
 let current=null,pending=null;
 function h(tag,props={},...children){
  const el=document.createElement(tag);
  for(const [key,value]of Object.entries(props)){
   if(value===undefined||value===null||value===false)continue;
   if(key==='class')el.className=value;
   else if(key.startsWith('on'))el.addEventListener(key.slice(2),value);
   else if(key.startsWith('aria-')||key==='role')el.setAttribute(key,value);
   else el[key]=value;
  }
  el.append(...children.flat(Infinity).filter(child=>child!==undefined&&child!==null&&child!==false));
  return el;
 }
 const when=value=>{if(!value)return '';const d=new Date(value);return Number.isNaN(d.getTime())?'':d.toLocaleString(undefined,{day:'numeric',month:'short',year:'numeric'});};
 // When the list was read: the time today, with the date on another day.
 const asOf=value=>{const d=new Date(value);if(Number.isNaN(d.getTime()))return 'an unknown time';const time=d.toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit'});return d.toDateString()===new Date().toDateString()?time:`${when(value)}, ${time}`;};
 const shorten=text=>text.length>140?`${text.slice(0,139).trimEnd()}…`:text;
 const placeholder=text=>h('span',{class:'ph'},text);
 // The 384 px WebP name is the site's own thumbnail as far as observed; the originals are fallbacks.
 function jobThumb(job){
  if(job.video)return placeholder('Video');
  if(!JOB_ID.test(job.id))return placeholder('No preview');
  const sources=[`${CDN}/${job.id}/0_0_384_N.webp`,`${CDN}/${job.id}/0_0.jpeg`,`${CDN}/${job.id}/0_0.png`];
  const img=h('img',{alt:'',loading:'lazy',decoding:'async'});
  img.addEventListener('error',()=>{sources.shift();if(sources.length)img.src=sources[0];else img.replaceWith(placeholder('No preview'));});
  img.src=sources[0];return img;
 }
 function referenceThumb(url){
  let src=null;try{const parsed=new URL(url);if(parsed.origin===CDN)src=parsed.href;}catch{}
  if(!src)return placeholder('Not shown');
  const img=h('img',{alt:'',loading:'lazy',decoding:'async'});
  img.addEventListener('error',()=>img.replaceWith(placeholder('No preview')),{once:true});
  img.src=src;return img;
 }
 // A second open while the window is being built or shown gets the same answer. options: the export controls.
 function open(options={}){
  if(current){current.focus();return current.done;}
  if(!pending)pending=build(options).finally(()=>{pending=null;});
  return pending;
 }
 async function build(options){
  const stored=await chrome.storage.local.get(['scan_data','selection']);const scan=stored.scan_data;
  if(!scan)throw new Error('Scan first, then choose what to export');
  const choice=initialChoice(scan,stored.selection),initial=JSON.stringify(choice),groups=chooserGroups(scan),jobs=new Map(scan.jobs.map(job=>[job.id,job]));
  const returnFocus=document.activeElement;
  let focused=groups.find(group=>group.board!==null)||groups[0],shown=0,cards=[],refInput=null,refBox=null,groupCount=null,grid=null,more=null,finish;
  const done=new Promise(resolve=>{finish=resolve;});
  const host=document.createElement('mj-export-chooser');
  host.style.cssText='all:initial!important;position:fixed!important;inset:0!important;z-index:2147483647!important;display:block!important';
  const root=host.attachShadow({mode:'closed'});
  try{const sheet=new CSSStyleSheet();sheet.replaceSync(CSS);root.adoptedStyleSheets=[sheet];}catch{root.append(h('style',{},CSS));}
  const summary=h('p',{class:'summary','aria-live':'polite'});
  const saveButton=h('button',{type:'button',class:'primary',onclick:save},'Save selection');
  const cancelButton=h('button',{type:'button',onclick:cancel},'Cancel');
  const keepButton=h('button',{type:'button',onclick:()=>confirming(false)},'Keep choosing');
  const confirmBar=h('div',{class:'confirm',hidden:true},h('span',{},'Discard your changes?'),keepButton,h('button',{type:'button',class:'danger',onclick:()=>close(null)},'Discard changes'));
  const buttons=h('div',{class:'buttons'},cancelButton,saveButton);
  const main=h('div',{class:'main'});
  const rows=groups.map(group=>{
   const name=group.board===null?'Other generations':group.name||'Untitled moodboard';
   const input=h('input',{type:'checkbox',onchange:()=>change(()=>toggleGroup(choice,group))});
   const detail=group.board===null?countLabel(group.jobs.length,'generation'):`${countLabel(group.references.length,'reference')} · ${countLabel(group.jobs.length,'generation')}`;
   const button=h('button',{type:'button',class:'pick',title:name,onclick:()=>show(group)},h('span',{class:'name'},name),h('span',{class:'muted'},detail));
   const item=h('li',{class:'group'},h('label',{class:'tick'},input,h('span',{class:'sr'},group.board===null?'Export all other generations':`Export the whole moodboard ${name}`)),button);
   return {group,input,button,item};
  });
  const boardRows=rows.filter(row=>row.group.board!==null);
  const backdrop=h('div',{class:'backdrop'},h('div',{class:'dialog',role:'dialog','aria-modal':'true','aria-labelledby':'mjx-title'},
   h('header',{class:'top'},
    h('div',{class:'heading'},h('h2',{id:'mjx-title'},'Choose what to export'),h('p',{class:'muted'},`Your work as of ${asOf(scan.scanned_at)} · ${scopeLabel(scan.scope)}. Work you make later is not exported until you choose again.`)),
    h('div',{class:'bar'},
     h('button',{type:'button','aria-label':'Select all moodboards and generations',onclick:()=>change(()=>setEverything(choice,true))},'Select all'),
     h('button',{type:'button','aria-label':'Select no moodboards and no generations',onclick:()=>change(()=>setEverything(choice,false))},'Select none'))),
   options.testSubset?h('p',{class:'note'},'Small test is on: the export keeps only the first chosen moodboard and one profile.'):null,
   h('div',{class:'body'},
    h('nav',{class:'side','aria-label':'Moodboards'},h('h3',{class:'side-title'},'Your moodboards'),boardRows.length?h('ul',{class:'groups'},boardRows.map(row=>row.item)):h('p',{class:'muted empty'},'No moodboards in this scan.'),h('ul',{class:'groups'},rows.filter(row=>row.group.board===null).map(row=>row.item))),
    main),
   h('footer',{class:'bottom'},h('div',{class:'totals'},summary,h('p',{class:'muted'},'Your profiles are always exported: they are not part of this choice.')),confirmBar,buttons)));
  backdrop.addEventListener('mousedown',event=>{if(event.target===backdrop)event.preventDefault();});
  root.append(backdrop);
  function show(group){
   focused=group;
   for(const row of rows){row.item.classList.toggle('current',row.group===group);if(row.group===group)row.button.setAttribute('aria-current','true');else row.button.removeAttribute('aria-current');}
   renderPane();main.scrollTop=0;
  }
  function renderPane(){
   const group=focused,board=group.board!==null,name=board?group.name||'Untitled moodboard':'Other generations';
   cards=[];refInput=null;refBox=null;shown=0;
   const parts=[h('div',{class:'pane-head'},h('h3',{},name),h('p',{class:'muted'},board?`${countLabel(group.references.length,'reference image')} · ${countLabel(group.jobs.length,'generation')}`:`${countLabel(group.jobs.length,'generation')} not made with any of your moodboards`))];
   if(board){
    refInput=h('input',{type:'checkbox',onchange:()=>change(()=>{choice.moodboards[group.board]=refInput.checked;})});
    const label=group.references.length?`Include the moodboard and its ${countLabel(group.references.length,'reference image')}`:'Include the moodboard (it has no reference images)';
    refBox=h('section',{class:'refs'},h('label',{class:'option'},refInput,h('span',{},label)),group.references.length?h('div',{class:'ref-grid'},group.references.map(url=>h('span',{class:'ref'},referenceThumb(url)))):null);
    parts.push(refBox);
   }
   groupCount=h('span',{class:'muted'});
   parts.push(h('div',{class:'gens-head'},h('h4',{},board?'Generations made with this moodboard':'Generations'),groupCount,h('span',{class:'grow'}),
    h('button',{type:'button',disabled:!group.jobs.length,'aria-label':'Select all generations here',onclick:()=>change(()=>setJobs(choice,group.jobs,true))},'Select all'),
    h('button',{type:'button',disabled:!group.jobs.length,'aria-label':'Select no generations here',onclick:()=>change(()=>setJobs(choice,group.jobs,false))},'Select none')));
   grid=h('div',{class:'grid'});more=h('button',{type:'button',class:'more',hidden:true,onclick:showMore});
   parts.push(group.jobs.length?grid:h('p',{class:'empty'},board?'No generations in this scan were made with this moodboard.':'Every generation in this scan was made with one of your moodboards.'),more);
   main.replaceChildren(...parts);showMore();
  }
  // Only the focused group has cards, a page at a time, so only their thumbnails can load.
  function showMore(event){
   const ids=focused.jobs.slice(shown,shown+PAGE);shown+=ids.length;grid.append(...ids.map(card));
   const left=focused.jobs.length-shown;more.hidden=left<=0;more.textContent=`Show more (${left} not shown yet)`;
   refresh();if(event&&ids.length)cards[cards.length-ids.length].input.focus();
  }
  function card(id){
   const job=jobs.get(id)||{id,prompt:'',video:false};const prompt=job.prompt||'';
   const input=h('input',{type:'checkbox',onchange:()=>change(()=>setJobs(choice,[id],input.checked))});
   const el=h('label',{class:'card',title:prompt},h('span',{class:'box'},input),h('span',{class:'thumb'},jobThumb(job)),
    h('span',{class:'text'},h('span',{class:'date'},[when(job.created_at),job.video?'Video':''].filter(Boolean).join(' · ')||'No date'),h('span',{class:prompt?'prompt':'prompt muted'},prompt?shorten(prompt):'No prompt text')));
   cards.push({id,input,el});return el;
  }
  function refresh(){
   for(const row of rows){const state=groupState(choice,row.group);row.input.checked=state==='all';row.input.indeterminate=state==='some';row.input.disabled=row.group.board===null&&!row.group.jobs.length;}
   for(const item of cards){const on=choice.jobs[item.id]===true;item.input.checked=on;item.el.classList.toggle('off',!on);}
   if(refInput){refInput.checked=choice.moodboards[focused.board]===true;refBox.classList.toggle('off',!refInput.checked);}
   groupCount.textContent=focused.jobs.length?`${focused.jobs.filter(id=>choice.jobs[id]===true).length} of ${focused.jobs.length} chosen`:'';
   summary.textContent=summaryText(choiceCounts(choice));
  }
  function change(update){update();refresh();}
  function confirming(on){confirmBar.hidden=!on;buttons.hidden=on;(on?keepButton:cancelButton).focus();}
  function cancel(){if(!confirmBar.hidden)return confirming(false);if(JSON.stringify(choice)===initial)return close(null);confirming(true);}
  async function save(){
   saveButton.disabled=true;
   try{const selection=makeSelection(scan,choice);await chrome.storage.local.set({selection});close(selection);}
   catch(error){saveButton.disabled=false;summary.textContent=`Could not save: ${error.message}`;}
  }
  function close(result){
   for(const type of ['keydown','keyup','keypress'])window.removeEventListener(type,onKey,true);
   host.remove();current=null;if(returnFocus?.isConnected)returnFocus.focus?.();finish(result);
  }
  function trap(event){
   const items=[...root.querySelectorAll('button,input')].filter(el=>!el.disabled&&!el.closest('[hidden]')&&el.getClientRects().length);
   const index=items.indexOf(root.activeElement);if(!items.length)return;
   if(index===-1||(!event.shiftKey&&index===items.length-1)||(event.shiftKey&&index===0)){event.preventDefault();items[event.shiftKey?items.length-1:0].focus();}
  }
  // While open, keys belong to the chooser: the page's own shortcuts never see them.
  function onKey(event){
   if(event.type==='keydown'&&event.key==='Escape'){event.preventDefault();cancel();}
   else if(event.type==='keydown'&&event.key==='Tab')trap(event);
   event.stopPropagation();
  }
  for(const type of ['keydown','keyup','keypress'])window.addEventListener(type,onKey,true);
  document.documentElement.append(host);
  show(focused);
  const focusCurrent=()=>rows.find(row=>row.group===focused)?.button.focus();
  focusCurrent();
  current={focus:focusCurrent,done};
  return done;
 }
 globalThis.mjExportChooser={open};
})();
