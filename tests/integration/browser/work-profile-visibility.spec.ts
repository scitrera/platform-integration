import {test, expect, type Page} from '@playwright/test';
import {randomUUID} from 'node:crypto';
import {spawnSync} from 'node:child_process';

test.skip(!process.env.WORK_PROFILE_ACCEPTANCE, 'Requires the explicit work-profile fixture');

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


function chooser(enabled: boolean) {
  const result=spawnSync('python3',['tests/integration/work_profile_ui.py',
    '--project',process.env.WORK_PROFILE_PROJECT!,'--enabled',String(enabled)],
    {encoding:'utf8',timeout:65000});
  if(result.status!==0)throw new Error('Fixture chooser configuration failed');
}

test('tenant opt-in controls the chooser without disabling application profile selection', async ({page})=>{
  test.setTimeout(120000);
  try {
    chooser(false);
    await login(page,'alice@example.test');
    await expect(page.getByRole('button',{name:'New Thread',exact:true})).toBeVisible();
    const available=await rpc(page,'CHAT_WORK_PROFILES',{workspaceId:'default'});
    expect(available.profiles.some((profile:any)=>profile.id==='document-review')).toBe(true);
    await expect(page.getByLabel('Work profile for new conversation')).toHaveCount(0);

    const created=await rpc(page,'CT_ADD',{workspaceId:'default',name:'Application selected profile',workProfile:'document-review'});
    expect(created.workProfile).toBe('document-review');
    await page.goto('/alpha/default?thread='+encodeURIComponent(created.id));
    await expect(page.getByLabel('Conversation work profile')).toContainText('Document Review');
    await expect(page.getByLabel('Work profile for new conversation')).toHaveCount(0);

    chooser(true);
    await page.reload();
    await expect(page.getByLabel('Work profile for new conversation')).toBeVisible();
    await expect(page.getByLabel('Conversation work profile')).toContainText('Document Review');
  } finally {
    chooser(true);
  }
});
