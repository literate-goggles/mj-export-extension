import test from 'node:test';import assert from 'node:assert/strict';
import {ReadScheduler,mapImages,safeRelativePath} from '../lib/transport.js';
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
