(() => {
  const ownPage=()=>/^\/(organize|imagine|personalize|moodboards)(\/|$)/.test(location.pathname);
  const nativeFetch=window.fetch.bind(window);
  const paths=new Set(['/api/imagine','/api/moodboards','/api/personalized-profiles','/api/profile']);
  let ownId=null,last=0,queue=Promise.resolve();
  const allowedImages=new Set();
  const jobKeys=['id','enqueue_time','full_command','width','height','batch_size','job_type','event_type','parent_id','parent_grid','parent_index','image_likes','personalization_codes','video_segments'];
  const pick=(obj,keys)=>Object.fromEntries(keys.filter(k=>Object.hasOwn(obj,k)).map(k=>[k,obj[k]]));
  function project(path,data){
    if(path==='/api/imagine'){
      if(!Array.isArray(data.data))throw new Error('Unexpected jobs response');
      return {cursor:data.cursor??null,checkpoint:data.checkpoint??null,data:data.data.map(j=>{
        if(!/^[a-f0-9-]{36}$/i.test(j.id))throw new Error('Unexpected job ID');
        for(let i=0;i<Math.min(4,j.batch_size||1);i++)for(const ext of ['jpeg','png','webp'])allowedImages.add(`https://cdn.midjourney.com/${j.id}/0_${i}.${ext}`);
        return pick(j,jobKeys);
      })};
    }
    if(path==='/api/profile'){
      if(data.user_id!==ownId)throw new Error('Account identity mismatch');
      return pick(data,['user_id','username_v2']);
    }
    if(path==='/api/moodboards'){
      if(!Array.isArray(data))throw new Error('Unexpected moodboards response');
      return data.filter(b=>b.user_id===ownId).map(b=>{
        const images=(b.images||[]).map(i=>{try{if(new URL(i.url).origin==='https://cdn.midjourney.com'||/^https:\/\/s\.mj\.run\/[A-Za-z0-9_-]{1,64}$/.test(i.url))allowedImages.add(i.url);}catch{}return pick(i,['url','width','height','created']);}); // CDN pictures, and the Midjourney short links of older boards (the pattern of isShortLink in lib/transport.js)
        return {...pick(b,['moodboard_id','title','created','user_id','version','code']),images};
      });
    }
    if(path==='/api/personalized-profiles')return {profiles:(data.profiles||[]).filter(p=>p.user_id===ownId).map(p=>pick(p,['id','created','majorVersion','rankingCount','service','title','type','user_id','code'])),counts:pick(data.counts||{},['main','mainV7','niji','nijiV7','profileCounts'])};
  }
  function schema(x,d=0){
    if(d>6)return '…';if(x===null)return 'null';
    if(Array.isArray(x))return {length:x.length,samples:x.slice(0,3).map(v=>schema(v,d+1))};
    if(typeof x==='object')return Object.fromEntries(Object.entries(x).slice(0,60).filter(([k])=>!/(token|cookie|authorization|secret|email)/i.test(k)).map(([k,v])=>[k,schema(v,d+1)]));
    return typeof x;
  }
  window.fetch=async function(input,init){
    const result=await nativeFetch(input,init);
    try{
      const url=new URL(typeof input==='string'?input:input instanceof URL?input.href:input.url,location.origin);
      if(ownPage()&&url.origin===location.origin&&paths.has(url.pathname)){
        if(url.pathname==='/api/imagine'&&url.searchParams.get('user_id'))ownId=url.searchParams.get('user_id');
        result.clone().json().then(data=>window.postMessage({source:'mj-export-schema',record:{path:url.pathname,method:init?.method||'GET',params:[...url.searchParams.keys()],status:result.status,shape:schema(data)}},location.origin)).catch(()=>{});
      }
    }catch{}
    return result;
  };
  const sleep=ms=>new Promise(r=>setTimeout(r,ms));
  function challenge(){return /verify (you are|you're) human|unusual traffic|account (has been |is )?(suspended|banned)|captcha/i.test(document.body?.innerText||'');}
  async function api(path,params={}){
    const work=queue.then(async()=>{
      if(!ownPage()||!ownId)throw new Error('Open your Organize page and reload it to identify the current account');
      if(challenge())return {status:403,challenge:true};
      await sleep(Math.max(0,1000-(Date.now()-last)));last=Date.now();
      const url=new URL(path,location.origin);for(const [k,v]of Object.entries(params))if(v!==null&&v!==undefined&&v!=='')url.searchParams.set(k,String(v));
      const response=await nativeFetch(url,{method:'GET',credentials:'same-origin',headers:{'x-csrf-protection':'1'},redirect:'error'});
      const status=response.status;
      if(!response.ok)return {status,retryAfterMs:Number(response.headers.get('retry-after'))*1000};
      if(!response.headers.get('content-type')?.includes('json'))return {status:403,challenge:true};
      return {status,data:project(path,await response.json())};
    });queue=work.catch(()=>{});return work;
  }
  window.addEventListener('message',async event=>{
    if(event.source!==window||event.origin!==location.origin||event.data?.source!=='mj-export-request')return;
    const {id,op,args={}}=event.data;let result;
    try{
      if(op==='identity')result=await api('/api/profile',{user_id:ownId});
      else if(op==='jobs')result=await api('/api/imagine',{user_id:ownId,page_size:Math.max(1,Math.min(100,Number(args.size)||20)),cursor:args.cursor,checkpoint:args.checkpoint});
      else if(op==='moodboards')result=await api('/api/moodboards');
      else if(op==='profiles')result=await api('/api/personalized-profiles');
      else if(op==='authorize-image'){if(challenge())result={status:403,challenge:true};else if(allowedImages.has(args.url))result={status:200};else throw new Error('Image not in this account’s observed export records');}
      else throw new Error('Unsupported read operation');
    }catch(e){result={status:0,error:e.message};}
    window.postMessage({source:'mj-export-response',id,result},location.origin);
  });
})();
