import {readFileSync, writeFileSync} from 'node:fs';
import {spawnSync} from 'node:child_process';
import {test, expect, Page} from '@playwright/test';

const tenant=process.env.PLATFORM_TENANT || 'alpha';
if(!['alpha','beta'].includes(tenant))throw new Error('The fixture suite requires alpha or beta');
const otherTenant=tenant==='alpha'?'beta':'alpha';
const email=tenant==='alpha'?'alice@example.test':'bob@example.test';
const route=(path:string)=>path.replaceAll('/alpha/', '/'+tenant+'/');


async function get(page: Page, path: string, options: {headers?:Record<string,string>}={}) {
  const result=await page.evaluate(async ({path,options})=>{
    const response=await fetch(path,{headers:options.headers,credentials:'include'});
    return {status:response.status,body:await response.text()};
  },{path,options});
  return {status:()=>result.status,json:async()=>JSON.parse(result.body)};
}

async function rpc(page: Page,type:string,payload:unknown) {
  return page.evaluate(async ({type,payload,tenant}) => {
    const url=new URL('/'+tenant+'/rfe1-ws/v2?tenant='+tenant+'&windowId=files-'+crypto.randomUUID(),location.origin);
    url.protocol=location.protocol==='https:'?'wss:':'ws:';
    return await new Promise<any>((resolve,reject)=>{
      const ws=new WebSocket(url);
      const timer=setTimeout(()=>{ws.close();reject(new Error('File RPC timed out: '+type));},30000);
      ws.onmessage=event=>{
        const data=JSON.parse(event.data);
        if(data.type==='CONNECTION_READY')ws.send(JSON.stringify({event:'RPC',type,id:'file-rpc',payload}));
        if(data.id==='file-rpc'){clearTimeout(timer);ws.close();resolve(data);}
        if(data.type==='CONNECTION_REFUSED'){clearTimeout(timer);ws.close();reject(new Error('File RPC refused'));}
      };
      ws.onerror=()=>{clearTimeout(timer);reject(new Error('File RPC socket failed'));};
    });
  },{type,payload,tenant});
}

async function login(page: Page, path='/alpha/default?integration=1#resume') {
  if(tenant==='beta') {
    if(!process.env.FIXTURE_IDP_ORIGIN)throw new Error('FIXTURE_IDP_ORIGIN is required to select the beta identity');
    const selected=await page.context().request.get(process.env.FIXTURE_IDP_ORIGIN+'/select?email='+encodeURIComponent(email));
    expect(selected.status()).toBe(200);
  }
  await page.goto(route(path));
  await page.locator('a[href*="/auth/login/fixture"]').click();
  await expect(page).toHaveURL(new RegExp('/'+tenant+'/default'));
  await expect.poll(async () => (await get(page,'/api/auth/checkz')).status()).toBe(200);
}

test('real signed login, deep link and tenant boundary', async ({page}) => {
  const errors: string[]=[];
  page.on('pageerror', e => errors.push(e.message));
  await login(page);
  await expect(page).toHaveURL(new RegExp('/'+tenant+'/default[?]integration=1#resume$'));
  const auth=await (await get(page,'/api/auth/checkz')).json();
  expect(auth.email).toBe(email);
  const denied=await get(page,'/'+otherTenant+'/rfe1-ws/v2?tenant='+otherTenant+'&window=forged',{
    headers:{'X-Scitrera-User':'bob@example.test','X-Auth-Tenant-ID':'beta'}});
  expect(denied.status()).toBe(403);
  expect((await get(page,'/assets/missing.js')).status()).toBe(404);
  expect((await get(page,'/source.tar.gz')).status()).toBe(200);
  await expect(page.getByPlaceholder('Type a message... (Enter to Send)')).toBeVisible({timeout:30000});
  expect(errors).toEqual([]);
});

test('actual native socket sends readiness and serves a scoped profile', async ({page}) => {
  await login(page);
  const result=await page.evaluate(async ({tenant}) => {
    const url=new URL('/'+tenant+'/rfe1-ws/v2?tenant='+tenant+'&windowId=integration-native',location.origin);
    url.protocol=location.protocol==='https:'?'wss:':'ws:';
    return await new Promise<any>((resolve,reject) => {
      const ws=new WebSocket(url); const events:any[]=[];
      const timer=setTimeout(()=>{ws.close();reject(new Error('Native readiness/profile timeout'));},20000);
      ws.onmessage=event=>{
        const data=JSON.parse(event.data);events.push(data);
        if(data.type==='CONNECTION_READY') ws.send(JSON.stringify({event:'RPC',type:'GET_USER_PROFILE',id:'profile',payload:{}}));
        if(data.id==='profile'){clearTimeout(timer);ws.close();resolve({events,profile:data.payload});}
        if(data.type==='CONNECTION_REFUSED'){clearTimeout(timer);ws.close();reject(new Error('Native connection refused: '+JSON.stringify(data.payload)));}
      };
      ws.onerror=()=>{clearTimeout(timer);reject(new Error('Native connection failed'));};
    });
  },{tenant});
  expect(result.events[0].type).toBe('CONNECTION_READY');
  expect(result.profile.email).toBe(email);
});


test('chat streams through the allocated Sahara sandbox and survives reload', async ({page}) => {
  test.setTimeout(120000);
  const started:any[]=[];
  page.on('websocket',socket=>socket.on('framereceived',event=>{
    try {const data=JSON.parse(event.payload.toString());if(data.type==='CHAT_MSG_TASK_STARTED')started.push(data.payload);}catch{}
  }));
  await login(page,'/alpha/default');
  const input=page.getByPlaceholder('Type a message... (Enter to Send)');
  const marker='integration-marker-'+crypto.randomUUID().replaceAll('-','');
  const response='Synthetic platform response. '+marker;
  await input.fill('Return a synthetic integration response for '+marker);
  await input.press('Enter');
  await expect(page.getByText(response,{exact:true}).last()).toBeVisible({timeout:90000});
  if(process.env.GATEWAY_USAGE_RECORD){
    expect(started[0]?.taskId).toBeTruthy();
    writeFileSync(process.env.GATEWAY_USAGE_RECORD,JSON.stringify({tenant,email,task_id:started[0].taskId,marker}),{mode:0o600});
  }
  await page.reload();
  await expect(page.getByText(response,{exact:true}).last()).toBeVisible({timeout:30000});
});

test('authenticated upload and download preserve bytes and enforce tenant scope', async ({page})=>{
  test.setTimeout(240000);
  await login(page,'/alpha/default');
  const marker=crypto.randomUUID();
  const bytes='Synthetic file '+marker+'\nGreek: αβγ\n';
  const upload=await rpc(page,'FILE_UPLOAD_POST',{
    workspaceId:'default',fileName:'integration-'+marker+'.txt',contentType:'text/plain',method:'PUT',ingestFlags:{decompose:false},
  });
  expect(upload.type,upload.payload?.message).toBe('FILE_UPLOAD_POST');
  expect(upload.payload?.url).toContain('/storage/'+tenant+'/uploads/');
  const sent=await page.evaluate(async ({url,headers,bytes})=>{
    const result=await fetch(url,{method:'PUT',headers,body:new TextEncoder().encode(bytes),credentials:'include'});
    return result.status;
  },{url:upload.payload.url,headers:upload.payload.headers,bytes});
  expect(sent).toBe(200);
  const finalized=await rpc(page,'FILE_UPLOAD_COMPLETE',{workspace:'default',items:[{
    vfs_ref:upload.payload.key,content_type:'text/plain',
  }]});
  expect(finalized.payload?.finalized).toBe(1);
  const download=await rpc(page,'FILE_DOWNLOAD_GET',{workspaceId:'default',docId:upload.payload.key});
  expect(download.payload?.url).toContain('/storage/'+tenant+'/blob/');
  const received=await get(page,download.payload.url);
  expect(received.status()).toBe(200);
  const text=await page.evaluate(async url=>(await fetch(url,{credentials:'include'})).text(),download.payload.url);
  expect(text).toBe(bytes);
  const forged=await get(page,download.payload.url.replace('/storage/'+tenant+'/','/storage/'+otherTenant+'/'),{
    headers:{'X-Auth-Tenant-ID':'alpha','X-Scitrera-User':'alice@example.test'},
  });
  expect(forged.status()).toBe(403);
  expect((await get(page,'/storage/'+tenant+'/capabilities')).status()).toBe(404);
  await page.reload();
  expect((await get(page,download.payload.url)).status()).toBe(200);
  await expect.poll(async ()=>{
    const result=await rpc(page,'ADMIN_RPC_CALL',{op:'memorylayer.documents',args:{workspace_id:'default',limit:100}});
    expect(result.payload?.ok).toBe(true);
    return result.payload.result.items.find((item:any)=>item.filename==='integration-'+marker+'.txt')?.status;
  },{timeout:90000,intervals:[1000,2000,5000]}).toBe('completed');
  const digest=await page.evaluate(async bytes=>{
    const hash=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(bytes));
    return Array.from(new Uint8Array(hash)).map(x=>x.toString(16).padStart(2,'0')).join('');
  },bytes);
  const toolMarker='integration-marker-'+marker.replaceAll('-','');
  const input=page.getByPlaceholder('Type a message... (Enter to Send)');
  await input.fill('fixture-vfs '+toolMarker+' vfs_ref='+upload.payload.key+' sha256='+digest);
  await input.press('Enter');
  await expect(page.getByText('Verified file bytes '+toolMarker,{exact:true})).toBeVisible({timeout:90000});
  if(process.env.PERSISTENCE_RECORD)writeFileSync(process.env.PERSISTENCE_RECORD,JSON.stringify({
    vfsRef:upload.payload.key,bytes,digest,filename:'integration-'+marker+'.txt',
    threadURL:new URL(page.url()).pathname+new URL(page.url()).search,reply:'Verified file bytes '+toolMarker,
  }),{mode:0o600});

});

test('POST logout revokes the cookie for checkz and new socket connections', async ({page})=>{
  await login(page,'/alpha/default');
  const status=await page.evaluate(async ()=>{
    const response=await fetch('/api/auth/auth/logout',{method:'POST',credentials:'include'});
    return response.status;
  });
  expect(status).toBeGreaterThanOrEqual(200);
  expect(status).toBeLessThan(400);
  // The app may navigate to sign-in when its session disappears. Keep the
  // post-logout transport probe in another page sharing the same cookie jar.
  const probe=await page.context().newPage();
  await probe.goto('/api/auth/checkz');
  expect((await get(probe,'/api/auth/checkz')).status()).toBe(401);
  const opened=await probe.evaluate(async ({tenant})=>{
    const url=new URL('/'+tenant+'/rfe1-ws/v2?tenant='+tenant+'&windowId=after-logout',location.origin);
    url.protocol=location.protocol==='https:'?'wss:':'ws:';
    return new Promise<boolean>((resolve,reject)=>{
      const ws=new WebSocket(url);
      const timer=setTimeout(()=>{ws.close();reject(new Error('Logout refusal timeout'));},10000);
      ws.onopen=()=>{clearTimeout(timer);ws.close();resolve(true);};
      ws.onerror=()=>{clearTimeout(timer);ws.close();resolve(false);};
    });
  },{tenant});
  expect(opened).toBe(false);
  await probe.close();
});

test('Sahara executes Python in the allocated code container', async ({page})=>{
  test.setTimeout(120000);
  await login(page,'/alpha/default');
  const marker='integration-marker-'+crypto.randomUUID().replaceAll('-','');
  const input=page.getByPlaceholder('Type a message... (Enter to Send)');
  await input.fill('fixture-python '+marker);
  await input.press('Enter');
  await expect(page.getByText('Verified code execution '+marker,{exact:true})).toBeVisible({timeout:90000});
});

test('tenant Admin lists ingested documents with delegated authority', async ({page})=>{
  test.setTimeout(120000);
  await login(page,'/alpha/default');
  const result=await rpc(page,'ADMIN_RPC_CALL',{op:'memorylayer.documents',args:{workspace_id:'default',limit:100}});
  expect(result.payload?.ok,JSON.stringify(result.payload)).toBe(true);
  expect(Array.isArray(result.payload.result.items)).toBe(true);
  expect(result.payload.result.items.every((item:any)=>item.workspace_id==='default')).toBe(true);
});


test('legacy Socket.IO completes authenticated connect and scoped RPC', async ({page})=>{
  await login(page);
  const result=await page.evaluate(async ({tenant})=>{
    const url=new URL('/'+tenant+'/rfe1-ws/?EIO=4&transport=websocket',location.origin);
    url.protocol=location.protocol==='https:'?'wss:':'ws:';
    return await new Promise<any>((resolve,reject)=>{
      const ws=new WebSocket(url);
      const windowId='integration-socketio-'+crypto.randomUUID();
      const timer=setTimeout(()=>{ws.close();reject(new Error('Socket.IO readiness/RPC timeout'));},20000);
      ws.onmessage=event=>{
        const packet=String(event.data);
        if(packet.startsWith('0'))ws.send('40'+JSON.stringify({tenant,windowId}));
        else if(packet==='2')ws.send('3');
        else if(packet.startsWith('40'))ws.send('42'+JSON.stringify(['RPC',{type:'GET_USER_PROFILE',id:'legacy-profile',payload:{},windowId}]));
        else if(packet.startsWith('42')){
          const [,data]=JSON.parse(packet.slice(2));
          if(data.id==='legacy-profile'){clearTimeout(timer);ws.close();resolve(data);}
        } else if(packet.startsWith('44')){
          clearTimeout(timer);ws.close();reject(new Error('Socket.IO connection refused'));
        }
      };
      ws.onerror=()=>{clearTimeout(timer);reject(new Error('Socket.IO upgrade failed'));};
    });
  },{tenant});
  expect(result.type).toBe('GET_USER_PROFILE');
  expect(result.payload.email).toBe(email);
});

test('previously recorded file, document and chat survive recovery', async ({page})=>{
  test.skip(!process.env.PERSISTENCE_CHECK,'Select an existing private persistence record for a recovery drill');
  test.setTimeout(120000);
  const saved=JSON.parse(readFileSync(process.env.PERSISTENCE_CHECK!,'utf8'));
  await login(page,saved.threadURL);
  await expect(page.getByText(saved.reply,{exact:true})).toBeVisible({timeout:30000});
  const download=await rpc(page,'FILE_DOWNLOAD_GET',{workspaceId:'default',docId:saved.vfsRef});
  expect(download.type,download.payload?.message).toBe('FILE_DOWNLOAD_GET');
  const received=await page.evaluate(async url=>{
    const response=await fetch(url,{credentials:'include'});
    return {status:response.status,text:await response.text()};
  },download.payload.url);
  expect(received.status).toBe(200);
  expect(received.text).toBe(saved.bytes);
  const documents=await rpc(page,'ADMIN_RPC_CALL',{op:'memorylayer.documents',args:{workspace_id:'default',limit:100}});
  expect(documents.payload?.ok).toBe(true);
  expect(documents.payload.result.items.find((item:any)=>item.filename===saved.filename)?.status).toBe('completed');
  const marker='integration-marker-'+crypto.randomUUID().replaceAll('-','');
  const input=page.getByPlaceholder('Type a message... (Enter to Send)');
  await input.fill('fixture-vfs '+marker+' vfs_ref='+saved.vfsRef+' sha256='+saved.digest);
  await input.press('Enter');
  await expect(page.getByText('Verified file bytes '+marker,{exact:true})).toBeVisible({timeout:90000});
});

for(const decision of ['approve','deny'] as const) {
  test('real tool approval '+decision+' is scoped and controls the file write', async ({page})=>{
    test.setTimeout(120000);
    const sent:any[]=[];
    const received:any[]=[];
    page.on('websocket',socket=>{
      const capture=(items:any[],payload:string|Buffer)=>{
        try { const message=JSON.parse(payload.toString()); if(message.type)items.push(message); } catch {}
      };
      socket.on('framesent',event=>capture(sent,event.payload));
      socket.on('framereceived',event=>capture(received,event.payload));
    });
    await login(page,'/alpha/default');
    const marker='integration-marker-'+crypto.randomUUID().replaceAll('-','');
    const input=page.getByPlaceholder('Type a message... (Enter to Send)');
    await input.fill((decision==='deny'?'fixture-approval-deny ':'fixture-approval ')+marker);
    await input.press('Enter');
    try {
      await expect(page.getByText('Permission required',{exact:true})).toBeVisible({timeout:30000});
    } catch(error) {
      writeFileSync(process.env.PLATFORM_PROFILE==='kind'?'.local/helm/approval-wire.json':'.local/approval-wire.json',
        JSON.stringify({sent,received}),{mode:0o600});
      const stop=page.getByTitle('Stop the agent');
      if(await stop.isVisible())await stop.click();
      throw error;
    }
    const started=received.find(message=>message.type==='CHAT_MSG_TASK_STARTED')?.payload;
    expect(started?.taskId).toBeTruthy();
    await page.getByRole('button',{name:decision==='approve'?'Allow once':'Deny',exact:true}).click();
    await expect.poll(()=>sent.filter(message=>message.type==='CHAT_MSG_CONTROL').length).toBe(1);
    const control=sent.find(message=>message.type==='CHAT_MSG_CONTROL').payload;
    expect(control.workspace).toBe('default');
    expect(control.taskId).toBe(started.taskId);
    expect(control.threadId).toBe(started.threadId);
    expect(control.message.addr.task_id).toBe(started.taskId);
    const part=control.message.content[0];
    expect(part.kind).toBe(decision);
    expect(part.scope).toBe(decision==='approve'?'once':undefined);
    expect(part.request_id).toBeTruthy();
    await expect.poll(()=>received.some(message=>{
      const event=message.payload?.event;
      const parts=[event?.part,...(event?.message?.content || [])];
      return message.type==='CHAT_STREAM' && parts.some(candidate=>candidate?.type==='approval_request'
        && candidate.id===part.request_id && candidate.status===(decision==='approve'?'approved':'denied'));
    })).toBe(true);
    const reply=(decision==='approve'?'Verified approved file ':'Verified denied approval ')+marker;
    await expect(page.getByText(reply,{exact:true})).toBeVisible({timeout:60000});
    await page.reload();
    await expect(page.getByText(reply,{exact:true})).toBeVisible({timeout:30000});
  });
}


test('Sahara invokes the browser tool only in the initiating window', async ({page,context})=>{
  test.setTimeout(120000);
  const calls:any[]=[];
  const notices:any[]=[];
  const results:any[]=[];
  page.on('websocket',socket=>{
    socket.on('framereceived',event=>{
      try {const data=JSON.parse(event.payload.toString());if(data.type==='AGENT_TOOL_CALL')calls.push(data.payload);if(['RPX','AGENT_TOOL_CATALOG','CHAT_MSG_TASK_STARTED','CHAT_MSG_TASK_DONE','CHAT_GET_ACTIVE_TASKS'].includes(data.type))notices.push(data);}catch{}
    });
    socket.on('framesent',event=>{
      try {const data=JSON.parse(event.payload.toString());if(data.type==='AGENT_TOOL_RESULT')results.push(data.payload);}catch{}
    });
  });
  await login(page,'/alpha/default');
  const observer=await context.newPage();
  const otherCalls:any[]=[];
  let observerCatalog=false;
  observer.on('websocket',socket=>{
    socket.on('framereceived',event=>{
      try {const data=JSON.parse(event.payload.toString());if(data.type==='AGENT_TOOL_CALL')otherCalls.push(data.payload);if(data.type==='AGENT_TOOL_CATALOG' && data.payload?.published)observerCatalog=true;}catch{}
    });
  });
  await observer.goto(route('/alpha/default'));
  await expect.poll(()=>observerCatalog,{timeout:15000}).toBe(true);
  await expect.poll(()=>notices.some(data=>data.type==='AGENT_TOOL_CATALOG' && data.payload?.published),{timeout:15000}).toBe(true);
  const marker='integration-marker-'+crypto.randomUUID().replaceAll('-','');
  const input=page.getByPlaceholder('Type a message... (Enter to Send)');
  await input.fill('fixture-browser-tool frontend_set_chat_state fullscreen '+marker);
  await input.press('Enter');
  try {
    await expect(page.getByText('Permission required',{exact:true})).toBeVisible({timeout:15000});
  } catch(error) {
    writeFileSync(process.env.PLATFORM_PROFILE==='kind'?'.local/helm/browser-tool-notices.json':'.local/browser-tool-notices.json',
      JSON.stringify({notices,calls,results,otherCalls}),{mode:0o600});
    throw error;
  }
  try {
    await page.getByRole('button',{name:'Allow once',exact:true}).click({timeout:15000});
    await expect(page.getByText('Verified browser tool '+marker,{exact:true})).toBeVisible({timeout:60000});
  } catch(error) {
    writeFileSync(process.env.PLATFORM_PROFILE==='kind'?'.local/helm/browser-tool-notices.json':'.local/browser-tool-notices.json',
      JSON.stringify({notices,calls,results,otherCalls}),{mode:0o600});
    const stop=page.getByTitle('Stop the agent');
    if(await stop.isVisible())await stop.click();
    throw error;
  }
  expect(calls).toHaveLength(1);
  expect(calls[0].toolName).toBe('frontend_set_chat_state');
  expect(calls[0].args).toEqual({state:'fullscreen'});
  expect(results.find(item=>item.callId===calls[0].callId)?.result).toBe('fullscreen');
  expect(otherCalls).toEqual([]);
});


test('cancelling a live stream stops the addressed task and allows the next turn', async ({page})=>{
  test.setTimeout(120000);
  const sent:any[]=[];
  const received:any[]=[];
  page.on('websocket',socket=>{
    socket.on('framesent',event=>{try {sent.push(JSON.parse(event.payload.toString()));}catch{}});
    socket.on('framereceived',event=>{try {received.push(JSON.parse(event.payload.toString()));}catch{}});
  });
  await login(page,'/alpha/default');
  const marker='integration-marker-'+crypto.randomUUID().replaceAll('-','');
  const input=page.getByPlaceholder('Type a message... (Enter to Send)');
  await input.fill('fixture-slow-stream '+marker);
  await input.press('Enter');
  await expect(page.getByText(new RegExp('^Streaming fixture '+marker))).toBeVisible({timeout:30000});
  const started=received.find(message=>message.type==='CHAT_MSG_TASK_STARTED')?.payload;
  expect(started?.taskId).toBeTruthy();
  await page.getByTitle('Stop the agent').click();
  await expect.poll(()=>sent.some(message=>message.type==='CHAT_MSG_CANCEL')).toBe(true);
  const cancel=sent.find(message=>message.type==='CHAT_MSG_CANCEL').payload;
  expect(cancel.taskId).toBe(started.taskId);
  expect(cancel).toEqual({taskId:started.taskId});
  await expect.poll(()=>received.find(message=>message.type==='CHAT_MSG_TASK_DONE'
    && message.payload.taskId===started.taskId)?.payload.status,{timeout:15000}).toBe('cancelled');
  expect(received.some(message=>JSON.stringify(message).includes('COMPLETE '+marker))).toBe(false);
  const next='integration-marker-'+crypto.randomUUID().replaceAll('-','');
  await input.fill('Return a synthetic integration response for '+next);
  await input.press('Enter');
  await expect(page.getByText('Synthetic platform response. '+next,{exact:true})).toBeVisible({timeout:30000});
});

test('provider failure completes the task as failed and a later request recovers', async ({page})=>{
  test.setTimeout(120000);
  const received:any[]=[];
  page.on('websocket',socket=>socket.on('framereceived',event=>{
    try {received.push(JSON.parse(event.payload.toString()));}catch{}
  }));
  await login(page,'/alpha/default');
  const marker='integration-marker-'+crypto.randomUUID().replaceAll('-','');
  const input=page.getByPlaceholder('Type a message... (Enter to Send)');
  await input.fill('fixture-provider-error '+marker);
  await input.press('Enter');
  await expect.poll(()=>received.find(message=>message.type==='CHAT_MSG_TASK_STARTED')?.payload.taskId).toBeTruthy();
  const task=received.find(message=>message.type==='CHAT_MSG_TASK_STARTED').payload.taskId;
  await expect.poll(()=>received.find(message=>message.type==='CHAT_MSG_TASK_DONE'
    && message.payload.taskId===task)?.payload.status,{timeout:90000}).toBe('failed');
  await expect(page.getByTitle('Stop the agent')).not.toBeVisible();
  const next='integration-marker-'+crypto.randomUUID().replaceAll('-','');
  await input.fill('Return a synthetic integration response for '+next);
  await input.press('Enter');
  await expect(page.getByText('Synthetic platform response. '+next,{exact:true})).toBeVisible({timeout:30000});
});


test('Sahara presents generated bytes as a durable scoped file artifact', async ({page})=>{
  test.setTimeout(120000);
  const parts:any[]=[];
  page.on('websocket',socket=>socket.on('framereceived',event=>{
    try {
      const data=JSON.parse(event.payload.toString());
      if(data.type==='CHAT_STREAM') {
        const item=data.payload?.event;
        for(const part of [item?.part,...(item?.message?.content || [])])if(part && ['file','tool_result'].includes(part.type))parts.push(part);
      }
    } catch {}
  }));
  await login(page,'/alpha/default');
  const marker='integration-marker-'+crypto.randomUUID().replaceAll('-','');
  const filename='artifact-'+marker+'.txt';
  const bytes='Synthetic generated artifact '+marker+'\nGreek: αβγ\n';
  const input=page.getByPlaceholder('Type a message... (Enter to Send)');
  await input.fill('fixture-artifact '+marker);
  await input.press('Enter');
  const reply='Verified generated artifact '+marker;
  await expect(page.getByText(new RegExp('^(Verified generated artifact|Artifact presentation failed) '+marker+'$'))).toBeVisible({timeout:90000});
  if(!await page.getByText(reply,{exact:true}).isVisible())writeFileSync(
    process.env.PLATFORM_PROFILE==='kind'?'.local/helm/artifact-results.json':'.local/artifact-results.json',
    JSON.stringify(parts),{mode:0o600});
  await expect(page.getByText(reply,{exact:true})).toBeVisible();
  const part=parts.find(item=>item.file_name===filename);
  expect(part?.vfs_ref).toMatch(/^vfs_[a-zA-Z0-9]+$/);
  expect(part.mime).toBe('text/plain');
  expect(part.size_bytes).toBe(Buffer.byteLength(bytes));
  await expect(page.getByText(filename,{exact:true}).first()).toBeVisible();
  const verifyDownload=async()=>{
    const result=await rpc(page,'FILE_DOWNLOAD_GET',{workspaceId:'default',docId:part.vfs_ref});
    expect(result.type).toBe('FILE_DOWNLOAD_GET');
    const actual=await page.evaluate(async url=>{
      const response=await fetch(url,{credentials:'include'});
      return {status:response.status,bytes:await response.text()};
    },result.payload.url);
    expect(actual).toEqual({status:200,bytes});
  };
  await verifyDownload();
  const denied=await rpc(page,'FILE_DOWNLOAD_GET',{workspaceId:'ungranted-fixture-workspace',docId:part.vfs_ref});
  expect(denied.type).toBe('RPX');
  expect(denied.payload?.url).toBeUndefined();
  await page.reload();
  await expect(page.getByText(reply,{exact:true})).toBeVisible({timeout:30000});
  await expect(page.getByText(filename,{exact:true}).first()).toBeVisible();
  await verifyDownload();
});


test('a signed-in fixture identity without membership cannot enter a tenant', async ({page})=>{
  if(!process.env.FIXTURE_IDP_ORIGIN)throw new Error('FIXTURE_IDP_ORIGIN is required for the no-membership identity');
  const selected=await page.context().request.get(process.env.FIXTURE_IDP_ORIGIN+'/select?email=denied%40example.test');
  expect(selected.status()).toBe(200);
  const ready:any[]=[];
  page.on('websocket',socket=>socket.on('framereceived',event=>{
    try {const data=JSON.parse(event.payload.toString());if(data.type==='CONNECTION_READY')ready.push(data);}catch{}
  }));
  await page.goto(route('/alpha/default'));
  await page.locator('a[href*="/auth/login/fixture"]').click();
  await expect.poll(async ()=>(await get(page,'/api/auth/checkz')).status()).toBe(200);
  const auth=await (await get(page,'/api/auth/checkz')).json();
  expect(auth.email).toBe('denied@example.test');
  await expect(page.getByRole('heading',{name:'No workspaces available',exact:true})).toBeVisible();
  await expect(page.getByPlaceholder('Type a message... (Enter to Send)')).toHaveCount(0);
  for(const slug of ['alpha','beta']){
    expect((await get(page,'/'+slug+'/rfe1-ws/v2?tenant='+slug+'&windowId=denied')).status()).toBe(401);
    expect((await get(page,'/storage/'+slug+'/blob/nonexistent')).status()).toBe(401);
  }
  expect(ready).toEqual([]);
});


function operatorAction(action:string, identity:string){
  const result=spawnSync('python3',['tests/integration/auth_operator.py',action,
    '--origin',process.env.AUTH_OPERATOR_ORIGIN!,'--operators',process.env.AUTH_OPERATORS_FILE!,
    '--tenant',tenant,'--email',identity],{encoding:'utf8',timeout:30000});
  if(result.status!==0){
    writeFileSync(process.env.PLATFORM_PROFILE==='kind'?'.local/helm/auth-operator-test.log':'.local/auth-operator-test.log',
      result.stdout+'\n'+result.stderr,{mode:0o600});
    throw new Error('Synthetic fixture operator action failed: '+action);
  }
}

test('tenant membership alone cannot authorize embedded Admin operations', async ({page})=>{
  test.skip(!process.env.AUTH_OPERATOR_ORIGIN || !process.env.AUTH_OPERATORS_FILE,'Requires private fixture operator configuration paths');
  test.setTimeout(90000);
  operatorAction('grant-member','denied@example.test');
  try{
    const selected=await page.context().request.get(process.env.FIXTURE_IDP_ORIGIN+'/select?email=denied%40example.test');
    expect(selected.status()).toBe(200);
    await page.goto(route('/alpha/default'));
    await page.locator('a[href*="/auth/login/fixture"]').click();
    await expect.poll(async ()=>(await get(page,'/api/auth/checkz')).status()).toBe(200);
    // Operator enrollment and tenant-side identity caches converge separately.
    // Establish a positive native read before testing the admin denial.
    await expect.poll(async ()=>{
      try{return (await rpc(page,'GET_USER_PROFILE',{})).payload?.email;}
      catch{return undefined;}
    },{timeout:45000,intervals:[250,500,1000]}).toBe('denied@example.test');
    for(const op of ['memorylayer.documents','aether.list_acl_rules']){
      const result=await rpc(page,'ADMIN_RPC_CALL',{op,args:{workspace_id:'default',limit:1}});
      expect(result.payload).toEqual({ok:false,error:'ERR_PERMISSION_DENIED'});
    }
  }finally{
    operatorAction('remove-member','denied@example.test');
  }
});

test('operator revocation refuses new sessions and preserves already accepted sockets', async ({page})=>{
  test.skip(!process.env.AUTH_OPERATOR_ORIGIN || !process.env.AUTH_OPERATORS_FILE,'Requires private fixture operator configuration paths');
  test.setTimeout(90000);
  await login(page,'/alpha/default');
  const probe=await page.context().newPage();
  await probe.goto('/assets/missing.js');
  await probe.evaluate(async tenant=>{
    const url=new URL('/'+tenant+'/rfe1-ws/v2?tenant='+tenant+'&windowId=revoke-'+crypto.randomUUID(),location.origin);
    url.protocol=location.protocol==='https:'?'wss:':'ws:';
    await new Promise<void>((resolve,reject)=>{
      const ws=new WebSocket(url);(window as any).__acceptanceSessionSocket=ws;
      const timer=setTimeout(()=>reject(new Error('Initial socket readiness timed out')),20000);
      ws.onmessage=event=>{if(JSON.parse(event.data).type==='CONNECTION_READY'){clearTimeout(timer);resolve();}};
      ws.onerror=()=>{clearTimeout(timer);reject(new Error('Initial socket failed'));};
    });
  },tenant);
  operatorAction('revoke-sessions',email);
  await expect.poll(async ()=>(await get(probe,'/api/auth/checkz')).status()).toBe(401);
  expect((await get(probe,'/'+tenant+'/rfe1-ws/v2?tenant='+tenant+'&windowId=revoked-new')).status()).toBe(401);
  const accepted=await probe.evaluate(async ()=>{
    const ws=(window as any).__acceptanceSessionSocket as WebSocket;
    return await new Promise<any>((resolve,reject)=>{
      const timer=setTimeout(()=>reject(new Error('Accepted socket profile timed out')),10000);
      ws.onmessage=event=>{
        const data=JSON.parse(event.data);
        if(data.id==='revoked-existing'){clearTimeout(timer);ws.close();resolve(data);}
      };
      ws.send(JSON.stringify({event:'RPC',type:'GET_USER_PROFILE',id:'revoked-existing',payload:{}}));
    });
  });
  expect(accepted.payload.email).toBe(email);
  await probe.close();
  await page.goto(route('/alpha/default'));
  await expect(page.locator('a[href*="/auth/login/fixture"]')).toBeVisible();
});


test('a short-lived fixture session expires at the installed public boundary', async ({page})=>{
  test.skip(process.env.FIXTURE_SESSION_EXPIRY!=='1','Run through session_expiry.py to restore the fixture auth workload');
  await login(page,'/alpha/default');
  // The controller sets a real three-second server-side session lifetime.
  // Wait beyond that lifetime without replacing or editing the signed cookie.
  await page.waitForTimeout(4500);
  expect((await get(page,'/api/auth/checkz')).status()).toBe(401);
  expect((await get(page,'/'+tenant+'/rfe1-ws/v2?tenant='+tenant+'&windowId=expired')).status()).toBe(401);
  await page.reload();
  await expect(page.locator('a[href*="/auth/login/fixture"]')).toBeVisible();
});


function workerAction(action:string){
  const profile=process.env.PLATFORM_PROFILE==='kind'?'kind':'compose';
  const args=['tests/integration/worker_signal.py',action,'--profile',profile,'--sandbox',process.env.WORKER_SANDBOX_ID!];
  if(profile==='kind')args.push('--kubeconfig',process.env.WORKER_KUBECONFIG!,'--context',process.env.WORKER_CONTEXT!,'--pod',process.env.WORKER_POD!);
  else args.push('--project',process.env.WORKER_PROJECT!);
  const result=spawnSync('python3',args,{encoding:'utf8',timeout:20000});
  if(result.status!==0)throw new Error('Selected worker signal failed: '+action);
}

test('an unavailable worker resumes the queued turn across browser reconnect', async ({page})=>{
  test.skip(!process.env.WORKER_SANDBOX_ID,'Requires the explicitly selected disposable allocation');
  test.setTimeout(90000);
  await login(page,'/alpha/default');
  const marker='integration-marker-'+crypto.randomUUID().replaceAll('-','');
  const expected='Synthetic platform response. '+marker;
  const url=page.url();
  let resumed=false;
  try{
    workerAction('pause');
    const input=page.getByPlaceholder('Type a message... (Enter to Send)');
    await input.fill('Return a synthetic integration response for '+marker);
    await input.press('Enter');
    await page.waitForTimeout(1500);
    await expect(page.getByText(expected,{exact:true})).toHaveCount(0);
    // Navigating away closes the real browser transport while the worker
    // is unavailable. The next page must recover through replay/history.
    await page.goto('about:blank');
    workerAction('resume');resumed=true;
    await page.goto(url);
    await expect(page.getByText(expected,{exact:true})).toBeVisible({timeout:60000});
    await expect(page.getByText(expected,{exact:true})).toHaveCount(1);
  }finally{
    if(!resumed)workerAction('resume');
  }
});
