import test from 'node:test';
import assert from 'node:assert/strict';
import {parseCommand} from '../lib/parser.js';
test('all requested flags and aliases',()=>{
 const p=parseCommand('a cat --ar 3:2 --v 7 --s 100 --chaos 5 --w 20 --sref https://cdn.example/a.png::2 https://cdn.example/b.png --p abc def --profile xyz --niji 6 --raw --tile --iw 1.5');
 assert.equal(p.prompt,'a cat');
 assert.deepEqual(p.flags,{ar:'3:2',v:'7',stylize:'100',chaos:'5',weird:'20',sref:['https://cdn.example/a.png::2','https://cdn.example/b.png'],profile:['abc','def','xyz'],niji:'6',raw:true,tile:true,iw:'1.5'});
});
test('quoted flags, multi-prompts and weights remain verbatim',()=>{
 const text='"a sign --raw"::2 cat\'s cradle::0.5 --ar 2:3';
 const p=parseCommand(text);
 assert.equal(p.full_command,text); assert.equal(p.prompt,'"a sign --raw"::2 cat\'s cradle::0.5');
 assert.equal(p.flags.raw,false); assert.equal(p.flags.ar,'2:3');
});
test('unknown flags stripped from prompt but preserved',()=>{
 const p=parseCommand('portrait --seed 12 --no text watermark --v 6.1');
 assert.equal(p.prompt,'portrait'); assert.deepEqual(p.unknown_flags,{seed:['12'],no:['text watermark']});
});
test('repeated refs, quoted URL and default personalization',()=>{
 assert.deepEqual(parseCommand('cat --sref "https://example.com/a?q=two words" --sref random').flags.sref,['https://example.com/a?q=two words','random']);
 assert.equal(parseCommand('cat --p').flags.profile.length,0);
 assert.equal(parseCommand('cat --niji').flags.niji,true);
});
test('no flags and escaped quotes',()=>{
 assert.equal(parseCommand('cats::2 dogs::-1').prompt,'cats::2 dogs::-1');
 assert.equal(parseCommand('"say \\"hi\\" --raw" --tile').flags.raw,false);
});
