import test from 'node:test';import assert from 'node:assert/strict';import {normalizeJob,parentGaps,normalizeProfiles,normalizeBoards} from '../lib/normalize.js';
test('explicit lineage beats unrelated selection flags',()=>{
 const j=normalizeJob({id:'child',parent_id:'parent',parent_grid:2,job_type:'upscale',batch_size:1,full_command:'cat --raw',selected:false});
 assert.equal(j.kind,'upscale');assert.equal(j.parent_index,2);assert.equal(j.parent_id,'parent');assert.equal(j.flags.raw,true);
 assert.equal(parentGaps([j]).length,1);assert.equal(parentGaps([j,{job_id:'parent'}]).length,0);
});
test('unknown child kind is not invented',()=>assert.equal(normalizeJob({id:'child',parent_id:'parent',batch_size:4}).kind,'other'));
test('global profile counts only; no ratings records',()=>{
 const result=normalizeProfiles({profiles:[],counts:{mainV7:565}},{user_id:'me'},[]);
 assert.equal(result[0].ratings_count,565);assert.deepEqual(result[0].codes,[]);
});

test('moodboards separate references from generations and preserve multiple historical codes',()=>{
 const boards=normalizeBoards([{moodboard_id:'board',title:'Synthetic board',images:[{url:'https://cdn.midjourney.com/reference.png'}]}],[
  {job_id:'one',personalization_codes:[{id:'board',code:'old'}]},
  {job_id:'two',personalization_codes:[{id:'board',code:'new'},{id:'board',code:'new'}]},
  {job_id:'other',personalization_codes:[{id:'different',code:'elsewhere'}]},
 ]);
 assert.deepEqual(boards[0].generated_job_ids,['one','two']);
 assert.deepEqual(boards[0].codes,['old','new']);
 assert.equal(boards[0].images.length,1);
 assert.deepEqual(normalizeBoards([{moodboard_id:'empty',images:[]}],[])[0].generated_job_ids,[]);
});
