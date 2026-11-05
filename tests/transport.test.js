import test from 'node:test';import assert from 'node:assert/strict';
import {ReadScheduler,mapImages,safeRelativePath,isShortLink,savedName} from '../lib/transport.js';
test('serial API calls are spaced at least one second',async()=>{
 let clock=0;const times=[];const s=new ReadScheduler({now:()=>clock,sleep:async ms=>{clock+=ms;}});
 await Promise.all([1,2,3].map(()=>s.api(async()=>{times.push(clock);return{status:200};})));
 assert.deepEqual(times,[0,1000,2000]);
});
test('retry backs off and stops after repeated failures',async()=>{
 let clock=0,calls=0;const s=new ReadScheduler({now:()=>clock,sleep:async ms=>{clock+=ms;}});
 await assert.rejects(s.api(async()=>{calls++;return {status:429};}),/HTTP 429/);
 assert.equal(calls,3);assert.equal(clock,6000);assert.equal(s.stopped,true);
 await assert.rejects(s.api(async()=>({status:200})),/paused/);
});
test('challenge stops immediately',async()=>{
 const s=new ReadScheduler({sleep:async()=>{}});let calls=0;
 await assert.rejects(s.api(async()=>{calls++;return {status:403,challenge:true};}),/challenge/);assert.equal(calls,1);
});
test('no more than two images in flight',async()=>{
 let active=0,max=0;
 await mapImages([1,2,3,4,5],async()=>{active++;max=Math.max(max,active);await new Promise(r=>setTimeout(r,5));active--;});
 assert.equal(max,2);
});
test('reject path traversal and absolute paths',()=>{
 for(const p of ['../secret','/secret','a/../../b','a\\b','a//b','a\u0000b'])assert.throws(()=>safeRelativePath(p));
 assert.equal(safeRelativePath('images/job_0.png'),'images/job_0.png');
});
test('a failed worker drains the other in-flight download before returning',async()=>{
 let drained=false;
 await assert.rejects(mapImages([0,1],async i=>{if(i===0)throw new Error('failed');await new Promise(r=>setTimeout(r,10));drained=true;}),/failed/);
 assert.equal(drained,true);
});

test('network exceptions stop the shared scheduler',async()=>{
 const scheduler=new ReadScheduler({sleep:async()=>{}});
 await assert.rejects(scheduler.attempt(async()=>{throw new Error('network failure');}),/network failure/);
 assert.equal(scheduler.stopped,true);
});

test('a failure of one picture is retried as usual, then returned without stopping the run',async()=>{
 let clock=0,calls=0;const s=new ReadScheduler({now:()=>clock,sleep:async ms=>{clock+=ms;}});
 assert.deepEqual(await s.attempt(async()=>{calls++;return {status:503,item:true};}),{status:503,item:true});
 assert.deepEqual([calls,clock,s.stopped],[3,6000,false]);
 for(const status of [404,0]){calls=0;assert.equal((await s.attempt(async()=>{calls++;return {status,item:true};})).status,status);assert.equal(calls,1);}
 assert.equal(s.stopped,false);
 await assert.rejects(s.attempt(async()=>({status:403,challenge:true,item:true})),/challenge/);assert.equal(s.stopped,true);
 const t=new ReadScheduler({sleep:async()=>{}});await assert.rejects(t.attempt(async()=>({status:404})),/HTTP 404/);assert.equal(t.stopped,true);
});
test('only a plain https://s.mj.run/<code> is a short link',()=>{
 for(const url of ['https://s.mj.run/AbCdEfGhIjK','https://s.mj.run/a-_9'])assert.equal(isShortLink(url),true,url);
 for(const url of ['http://s.mj.run/AbCdEfGhIjK','https://s.mj.run.example.invalid/AbCdEfGhIjK','https://s.mj.run/AbCdEfGhIjK?x=1','https://s.mj.run/AbCdEfGhIjK#x','https://s.mj.run/a/b','https://s.mj.run/AbCdEfGhIjK/','https://user@s.mj.run/AbCdEfGhIjK','https://s.mj.run:443/AbCdEfGhIjK','https://S.MJ.RUN/AbCdEfGhIjK','https://s.mj.run/','https://s.mj.run/AbCdEfGhIjK\n',`https://s.mj.run/${'a'.repeat(65)}`,'https://s.mj.run/%41b',null,42])assert.equal(isShortLink(url),false,String(url));
});
test('the saved name is the requested one in the requested folder, whatever suffix Chrome chose',()=>{
 const want='mj_export_x_20261105/images/moodboard_b_0.jpeg';
 assert.equal(savedName(want,want),'moodboard_b_0.jpeg');
 assert.equal(savedName(want,'/home/u/Downloads/mj_export_x_20261105/images/moodboard_b_0.png'),'moodboard_b_0.png');
 assert.equal(savedName(want,'C:\\Users\\u\\Downloads\\mj_export_x_20261105\\images\\moodboard_b_0.webp'),'moodboard_b_0.webp');
 for(const path of ['/home/u/Downloads/AbCdEfGhIjK.png','/d/mj_export_x_20261105/images/moodboard_b_0 (1).png','/d/mj_export_x_20261105/images/moodboard_b_0.png.crdownload','/d/other/images/moodboard_b_0.png','/d/xmj_export_x_20261105/images/moodboard_b_0.png','/d/mj_export_x_20261105/images/moodboard_b_01.png','/d/mj_export_x_20261105/images/moodboard_b_0.','',undefined])assert.equal(savedName(want,path),null,String(path));
});
