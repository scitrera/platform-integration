import {test, expect, type Page} from '@playwright/test';
import {randomUUID} from 'node:crypto';
import {spawnSync} from 'node:child_process';
import {writeFileSync} from 'node:fs';

test.skip(!process.env.WORK_PROFILE_ACCEPTANCE, 'Requires the explicit shared-worker fixture configuration');

async function login(page: Page, email: string) {
  const selected=await page.context().request.get(process.env.FIXTURE_IDP_ORIGIN+'/select?email='+encodeURIComponent(email));
  expect(selected.status()).toBe(200);
  await page.goto('/alpha/default');
  await page.locator('a[href*="/auth/login/fixture"]').click();
  await expect(page.getByPlaceholder('Type a message... (Enter to Send)')).toBeVisible({timeout:45000});
}

async function rpc(page: Page, type: string, payload: unknown) {
  return page.evaluate(async ({type,payload,window}) => {
    const url=new URL('/alpha/rfe1-ws/v2?tenant=alpha&windowId='+window, location.origin);
    url.protocol=location.protocol==='https:'?'wss:':'ws:';
    return new Promise<any>((resolve,reject) => {
      const socket=new WebSocket(url);
      const timer=setTimeout(()=>{socket.close();reject(new Error('RPC timeout: '+type));},30000);
      socket.onmessage=event=>{
        const message=JSON.parse(event.data);
        if(message.type==='CONNECTION_READY')socket.send(JSON.stringify({event:'RPC',id:'profile-rpc',type,payload}));
        if(message.id==='profile-rpc'){clearTimeout(timer);socket.close();resolve(message.payload);}
        if(message.type==='CONNECTION_REFUSED'){clearTimeout(timer);socket.close();reject(new Error('RPC refused'));}
      };
      socket.onerror=()=>{clearTimeout(timer);reject(new Error('RPC failed'));};
    });
  },{type,payload,window:'profile-'+randomUUID()});
}

async function newConversation(page: Page) {
  const select=page.getByLabel('Work profile for new conversation');
  await expect(select).toBeVisible();
  await select.selectOption('document-review');
  await page.getByRole('button',{name:'New Thread',exact:true}).click();
  await expect(page.getByLabel('Conversation work profile')).toContainText('Document Review',{timeout:45000});
  await expect.poll(()=>new URL(page.url()).searchParams.get('thread')).toBeTruthy();
  return new URL(page.url()).searchParams.get('thread')!;
}

function workspaceGrant(action: string) {
  const result=spawnSync('python3',['tests/integration/work_profile_acl.py',action,
    '--project',process.env.WORK_PROFILE_PROJECT!],{encoding:'utf8',timeout:65000});
  if(result.status!==0)throw new Error('Fixture workspace permissions failed: '+action);
}

function member(action: string) {
  const result=spawnSync('python3',['tests/integration/auth_operator.py',action,
    '--origin',process.env.AUTH_OPERATOR_ORIGIN!,'--operators',process.env.AUTH_OPERATORS_FILE!,
    '--tenant','alpha','--email','denied@example.test'],{encoding:'utf8',timeout:30000});
  if(result.status!==0)throw new Error('Fixture membership operation failed: '+action);
}

test('two users share a work-profile worker with separate conversations, controls and history', async ({browser})=>{
  test.setTimeout(300000);
  member('grant-member');
  const a=await browser.newContext(), b=await browser.newContext();
  const alice=await a.newPage(), memberPage=await b.newPage();
  alice.setDefaultTimeout(15000); memberPage.setDefaultTimeout(15000);
  const started:any[]=[];
  for(const [page,user] of [[alice,'alice@example.test'],[memberPage,'denied@example.test']] as const) {
    page.on('websocket',socket=>socket.on('framereceived',event=>{
      try {const data=JSON.parse(event.payload.toString());if(data.type==='CHAT_MSG_TASK_STARTED')started.push({...data.payload,user});}catch{}
    }));
  }
  try {
    workspaceGrant('grant');
    await Promise.all([login(alice,'alice@example.test'),login(memberPage,'denied@example.test')]);
    console.log('Both fixture users signed in');
    const [threadA,threadB]=await Promise.all([newConversation(alice),newConversation(memberPage)]);
    expect(threadA).not.toBe(threadB);
    console.log('Both profile conversations created');
    const markers=['integration-marker-'+randomUUID().replaceAll('-',''),'integration-marker-'+randomUUID().replaceAll('-','')];
    await Promise.all([alice,memberPage].map(async (page,index)=>{
      const input=page.getByPlaceholder('Type a message... (Enter to Send)');
      await input.fill('fixture-work-profile-policy '+markers[index]); await input.press('Enter');
      await expect(page.getByText('Verified work profile policy '+markers[index],{exact:true}).last()).toBeVisible({timeout:120000});
    }));
    console.log('Both shared-worker replies received');
    for(const [page,index] of [[alice,0],[memberPage,1]] as const) {
      await page.reload();
      await expect(page.getByLabel('Conversation work profile')).toContainText('Document Review',{timeout:45000});
      await expect(page.getByText('Verified work profile policy '+markers[index],{exact:true}).last()).toBeVisible({timeout:30000});
      await expect(page.getByText('Verified work profile policy '+markers[1-index],{exact:true})).toHaveCount(0);
    }
    // A custom application repeating the selected profile cannot move an existing conversation.
    const beforeMismatch=started.length;
    await rpc(alice,'CHAT_MESSAGE',{workspace:'default',threadId:threadA,workProfile:'draft',text:'must not run'});
    expect(started.length).toBe(beforeMismatch);
    const listed=await rpc(alice,'CT_LIST',{workspaceId:'default'});
    expect(listed.find((item:any)=>item.id===threadA)?.workProfile).toBe('document-review');

    const marker='integration-marker-'+randomUUID().replaceAll('-','');
    const before=started.length;
    const input=alice.getByPlaceholder('Type a message... (Enter to Send)');
    await input.fill('fixture-slow-stream '+marker); await input.press('Enter');
    await expect.poll(()=>started.length).toBeGreaterThan(before);
    const aliceTask=started.filter(task=>task.user==='alice@example.test').at(-1);
    const denied=await rpc(memberPage,'CHAT_MSG_CANCEL',{taskId:aliceTask.taskId});
    expect(denied).toMatchObject({ok:false,error:'task unavailable'});
    await expect(alice.getByText(new RegExp('COMPLETE '+marker))).toBeVisible({timeout:60000});
    // Delegated browser tools execute on the originating user's browser.
    const toolMarker='integration-marker-'+randomUUID().replaceAll('-','');
    const otherInput=memberPage.getByPlaceholder('Type a message... (Enter to Send)');
    await otherInput.fill('fixture-browser-tool '+toolMarker); await otherInput.press('Enter');
    await expect(memberPage.getByText('Permission required',{exact:true})).toBeVisible({timeout:30000});
    await expect(alice.getByText('Permission required',{exact:true})).toHaveCount(0);
    await memberPage.getByRole('button',{name:'Allow once',exact:true}).click();
    await expect(memberPage.getByText('Verified browser tool '+toolMarker,{exact:true}).last()).toBeVisible({timeout:60000});

    const project=process.env.WORK_PROFILE_PROJECT!;
    const inspect=spawnSync('docker',['ps','--filter','label=scitrera.instance_id='+project,
      '--filter','label=scitrera.tenant_id=alpha','--format','{{.Names}}'],{encoding:'utf8'});
    expect(inspect.status).toBe(0);
    const names=inspect.stdout.trim().split('\n').filter(Boolean);
    const sandboxes=names.filter(name=>name.startsWith('scitrera-sandbox-'));
    const workers=JSON.parse(spawnSync('docker',['inspect',...sandboxes],{encoding:'utf8'}).stdout)
      .filter((item:any)=>item.Config.Env.includes('SAHARA_WORK_PROFILE=document-review'));
    expect(workers).toHaveLength(1);
    const sandboxID=workers[0].Config.Labels['scitrera.sandbox_id'];
    const paired=spawnSync('docker',['ps','--filter','label=scitrera.instance_id='+project,
      '--filter','label=scitrera.sandbox_id='+sandboxID,'--format','{{.Names}}'],{encoding:'utf8'});
    expect(paired.stdout.trim().split('\n')).toHaveLength(2);
    if(process.env.WORK_PROFILE_RECORD)writeFileSync(process.env.WORK_PROFILE_RECORD,
      JSON.stringify({tenant:'alpha',sandboxID,threads:[threadA,threadB],users:['alice@example.test','denied@example.test'],started,
        checks:['selected-profile','shared-worker','separate-history','cross-user-cancel-denied','delegated-tool','user-scoped-approval','shared-runtime-policy','two-containers']}),{mode:0o600});
  } finally {
    await Promise.all([a.close(),b.close()]);
    try { workspaceGrant('revoke'); } finally { member('remove-member'); }
  }
});
