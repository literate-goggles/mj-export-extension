import {parseCommand} from './parser.js';
export function normalizeJob(j) {
 const parsed=parseCommand(j.full_command||'');
 const type=`${j.job_type||''} ${j.event_type||''}`.toLowerCase();
 let kind='other';
 for(const [rx,k]of [[/zoom/,'zoom'],[/pan/,'pan'],[/remix/,'remix'],[/upscale|upsample/,'upscale'],[/variation|vary/,'variation']])if(rx.test(type)){kind=k;break;}
 if(kind==='other'&&!j.parent_id&&j.batch_size>1)kind='grid';
 const index=Number.isInteger(j.parent_index)?j.parent_index:Number.isInteger(j.parent_grid)?j.parent_grid:null;
 const count=Number.isInteger(j.batch_size)&&j.batch_size>0?Math.min(j.batch_size,4):1;
 return {job_id:j.id,created_at:j.enqueue_time||null,...parsed,parent_id:j.parent_id||null,parent_index:index,kind,image_files:[],image_urls:Array.from({length:count},(_,i)=>`https://cdn.midjourney.com/${j.id}/0_${i}.jpeg`),width:j.width??null,height:j.height??null,job_type:j.job_type??null,event_type:j.event_type??null,parent_grid:j.parent_grid??null,personalization_codes:j.personalization_codes||[],image_likes:j.image_likes||[],video:!!j.video_segments};
}
export function normalizeBoards(boards,jobs){
 const references = new Map();
 for (const job of jobs) {
  for (const reference of job.personalization_codes || []) {
   if (!reference.id) continue;
   if (!references.has(reference.id)) references.set(reference.id, { codes: new Set(), jobs: new Set() });
   const entry = references.get(reference.id);
   if (reference.code) entry.codes.add(reference.code);
   entry.jobs.add(job.job_id);
  }
 }
 return boards.map(board => {
  const entry = references.get(board.moodboard_id);
  const codes = [...new Set([...(board.code ? [board.code] : []), ...(entry?.codes || [])])];
  return {
   id: board.moodboard_id, code: codes[0] || null, codes,
   name: board.title, version: board.version ?? null, created_at: board.created ?? null,
   generated_job_ids: [...(entry?.jobs || [])],
   images: (board.images || []).map(image => ({
    source_url: image.url, file: null, width: image.width ?? null, height: image.height ?? null,
   })),
  };
 });
}
export function normalizeProfiles(data,identity,jobs){
 const codes=new Map();for(const j of jobs)for(const p of j.personalization_codes||[])if(p.id&&p.code){if(!codes.has(p.id))codes.set(p.id,new Set());codes.get(p.id).add(p.code);}
 const profiles=data.profiles.map(p=>({id:p.id,codes:[...new Set([...(codes.get(p.id)||[]),...(p.code?[p.code]:[])])],name:p.title,version:p.majorVersion,created_at:p.created,ratings_count:p.rankingCount,service:p.service,type:p.type}));
 for(const [key,version,service,label]of [['main','6','main','Global V6 Profile'],['mainV7','7','main','Global V7 Profile'],['niji','6','niji','Global Niji V6 Profile'],['nijiV7','7','niji','Global Niji V7 Profile']]){
  if(typeof data.counts?.[key]!=='number')continue;
  const id=`${identity.user_id}_${version}_${service}`;
  if(!profiles.some(p=>p.id===id))profiles.push({id,codes:[...(codes.get(id)||[])],name:label,version,ratings_count:data.counts[key],service,type:'global'});
 }
 return profiles;
}
export function parentGaps(jobs){const ids=new Set(jobs.map(j=>j.job_id));return jobs.filter(j=>j.parent_id&&!ids.has(j.parent_id)).map(j=>({job_id:j.job_id,parent_id:j.parent_id,reason:'Parent is outside the selected subset or unavailable in the own feed'}));}
