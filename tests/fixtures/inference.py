"""Deterministic inference only; never evidence for a real model provider."""
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import math
import time
import uuid
import re

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def reply(self,data,status=200):
        raw=json.dumps(data).encode()
        self.send_response(status);self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    def do_GET(self):
        if self.path in ['/health','/healthz','/readyz']:return self.reply({'fixture':True})
        if self.path=='/v1/models':return self.reply({'object':'list','data':[{'id':'fixture-model','object':'model'}]})
        self.reply({'error':'not_found'},404)
    def do_POST(self):
        length=int(self.headers.get('Content-Length','0'))
        if length>4*1024*1024:return self.reply({'error':'too_large'},413)
        data=json.loads(self.rfile.read(length))
        if self.path in ['/v1/embeddings/images','/v1/embeddings/multi']:
            values=data.get('images',data.get('input',[]))
            vectors=[]
            for index,value in enumerate(values):
                digest=hashlib.sha256(str(value).encode()).digest()
                vector=[(digest[j%32]-127.5)/128 for j in range(128)]
                norm=math.sqrt(sum(x*x for x in vector))
                vectors.append({'index':index,'vectors':[[x/norm for x in vector]],'num_vectors':1})
            return self.reply({'data':vectors,'model':'fixture-multivector'})
        if self.path=='/v1/score':
            query=data['query_vectors']
            scores=[]
            for index,document in enumerate(data['document_vectors']):
                score=sum(max(sum(x*y for x,y in zip(q,d)) for d in document) for q in query)
                scores.append({'index':index,'score':score})
            return self.reply({'scores':scores})
        if self.path=='/v1/embeddings':
            inputs=data.get('input',[])
            if isinstance(inputs,str):inputs=[inputs]
            dim=int(data.get('dimensions') or 1536)
            vectors=[]
            for i,text in enumerate(inputs):
                seed=hashlib.sha256(str(text).encode()).digest()
                vector=[(seed[j%len(seed)]-127.5)/128 for j in range(dim)]
                scale=math.sqrt(sum(x*x for x in vector))
                vectors.append({'object':'embedding','index':i,'embedding':[x/scale for x in vector]})
            return self.reply({'object':'list','data':vectors,'model':'fixture-embedding','usage':{'prompt_tokens':1,'total_tokens':1}})
        if self.path=='/v1/chat/completions':
            marker=re.findall(r'integration-marker-[a-f0-9]+',json.dumps(data.get('messages',[])))
            response='Synthetic platform response.'+(' '+marker[-1] if marker else '')
            if data.get('model')=='fixture-memorylayer':
                response=('Synthetic fixture transcript for document ingestion.' if 'image_url' in json.dumps(data.get('messages',[])) else '[]')
            messages=data.get('messages',[])
            user_index=next((i for i in range(len(messages)-1,-1,-1) if messages[i].get('role')=='user'),-1)
            python_turn=user_index>=0 and 'fixture-python' in str(messages[user_index].get('content',''))
            tool_results=[str(m.get('content','')) for m in messages[user_index+1:] if m.get('role')=='tool']
            tool_call=None
            if python_turn and marker:
                expected='fixture-code:'+marker[-1]
                if not tool_results:
                    tool_call={'id':'call_'+uuid.uuid4().hex,'type':'function','function':{
                        'name':'python','arguments':json.dumps({'code':'print('+repr(expected)+')'})}}
                elif any(expected in output for output in tool_results):
                    response='Verified code execution '+marker[-1]
                else:
                    response='Code execution did not return the expected fixture marker.'
                    print('fixture tool failure: '+json.dumps(tool_results)[:2000],flush=True)
            user_text=str(messages[user_index].get('content','')) if user_index>=0 else ''
            credential_check=re.search(r'fixture-credential-check ([a-f0-9]{64})',user_text)
            if credential_check:
                authorization=self.headers.get('Authorization','')
                actual=hashlib.sha256(authorization.removeprefix('Bearer ').encode()).hexdigest()
                if not authorization.startswith('Bearer ') or actual!=credential_check.group(1):
                    return self.reply({'error':{'message':'Synthetic provider credential rejected',
                                               'type':'authentication_error','code':'invalid_api_key'}},401)
            slow_stream='fixture-slow-stream' in user_text and bool(marker)
            if 'fixture-provider-error' in user_text and marker:
                return self.reply({'error':{'message':'Synthetic inference outage '+marker[-1],
                                           'type':'fixture_unavailable','code':'fixture_unavailable'}},503)
            if slow_stream:
                response='Streaming fixture '+marker[-1]+' '+('more '*150)+'COMPLETE '+marker[-1]
            if 'fixture-vfs' in user_text and marker:
                reference=re.search(r'vfs_ref=(vfs_[a-zA-Z0-9]+)',user_text)
                digest=re.search(r'sha256=([a-f0-9]{64})',user_text)
                function=None
                if reference and digest:
                    if not tool_results:
                        function={'name':'vfs_get','arguments':json.dumps({'vfs_ref':reference.group(1),'workspace':'default'})}
                    elif len(tool_results)==1:
                        try:
                            file=json.loads(tool_results[0])['files'][0]
                            if file.get('error'):print('fixture VFS failure: '+re.sub(r'https?://\S+','[endpoint]',str(file['error']))[:700],flush=True)
                            path=file['path']
                            function={'name':'python','arguments':json.dumps({'code':
                                'from pathlib import Path; import hashlib; print(hashlib.sha256(Path('+repr(path)+').read_bytes()).hexdigest())'})}
                        except (ValueError,KeyError,IndexError,TypeError) as error:
                            print('fixture VFS parse failure: '+type(error).__name__,flush=True)
                            response='Fixture VFS materialization failed.'
                    elif digest.group(1) in tool_results[-1]:
                        response='Verified file bytes '+marker[-1]
                    else:
                        response='Fixture VFS byte digest did not match.'
                    if function:
                        tool_call={'id':'call_'+uuid.uuid4().hex,'type':'function','function':function}
            if 'fixture-approval' in user_text and marker:
                filename='approval-'+marker[-1]+'.txt'
                expected='approved-fixture:'+marker[-1]
                denied='fixture-approval-deny' in user_text
                function=None
                if not tool_results:
                    function={'name':'apply_patch','arguments':json.dumps({'patch':
                        '*** Begin Patch\n*** Add File: '+filename+'\n+'+expected+'\n*** End Patch\n'})}
                elif len(tool_results)==1:
                    function={'name':'read_file','arguments':json.dumps({'path':filename})}
                elif denied:
                    refused=any(word in tool_results[0].lower() for word in ['denied','not approved','requires approval','requires_approval','approval required'])
                    absent=any(word in tool_results[-1].lower() for word in ['no such file','not found','not_found'])
                    response=('Verified denied approval ' if refused and absent else 'Approval denial verification failed ')+marker[-1]
                else:
                    response=('Verified approved file ' if expected in tool_results[-1] else 'Approved file verification failed ')+marker[-1]
                if function:
                    tool_call={'id':'call_'+uuid.uuid4().hex,'type':'function','function':function}
            if 'fixture-artifact' in user_text and marker:
                filename='artifact-'+marker[-1]+'.txt'
                expected='Synthetic generated artifact '+marker[-1]+'\nGreek: αβγ\n'
                function=None
                if not tool_results:
                    function={'name':'write_file','arguments':json.dumps({'path':filename,'content':expected})}
                elif len(tool_results)==1:
                    function={'name':'present_artifact','arguments':json.dumps({'paths':[filename]})}
                else:
                    response=('Verified generated artifact ' if 'presented' in tool_results[-1] and filename in tool_results[-1]
                              else 'Artifact presentation failed ')+marker[-1]
                if function:
                    tool_call={'id':'call_'+uuid.uuid4().hex,'type':'function','function':function}
            if 'fixture-browser-tool' in user_text and marker:
                name='frontend_set_chat_state'
                names=[item.get('function',{}).get('name') for item in data.get('tools',[])]
                if not tool_results and name in names:
                    tool_call={'id':'call_'+uuid.uuid4().hex,'type':'function','function':{
                        'name':name,'arguments':json.dumps({'state':'fullscreen'})}}
                elif tool_results:
                    response=('Verified browser tool ' if 'fullscreen' in tool_results[-1] else 'Browser tool failed ')+marker[-1]
                else:
                    response='Browser tool was not advertised '+marker[-1]
            base={'id':'fixture-'+uuid.uuid4().hex,'object':'chat.completion','created':int(time.time()),'model':data.get('model','fixture-model')}
            usage={'prompt_tokens':10,'completion_tokens':4,'total_tokens':14}
            usage_event={**base,'object':'chat.completion.chunk','choices':[],'usage':usage}
            include_usage=bool(data.get('stream_options',{}).get('include_usage'))
            if tool_call:
                if not data.get('stream'):
                    return self.reply({**base,'choices':[{'index':0,'message':{'role':'assistant','content':None,'tool_calls':[tool_call]},'finish_reason':'tool_calls'}],'usage':usage})
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
                chunk={**base,'object':'chat.completion.chunk','choices':[{'index':0,'delta':{'role':'assistant','tool_calls':[{'index':0,**tool_call}]},'finish_reason':None}]}
                done={**base,'object':'chat.completion.chunk','choices':[{'index':0,'delta':{},'finish_reason':'tool_calls'}]}
                try:
                    for event in [chunk,done]+([usage_event] if include_usage else []):
                        self.wfile.write(('data: '+json.dumps(event)+'\n\n').encode());self.wfile.flush()
                    self.wfile.write(b'data: [DONE]\n\n');self.wfile.flush()
                except (BrokenPipeError,ConnectionResetError):pass
                return
            if not data.get('stream'):
                return self.reply({**base,'choices':[{'index':0,'message':{'role':'assistant','content':response},'finish_reason':'stop'}],
                                  'usage':{'prompt_tokens':10,'completion_tokens':4,'total_tokens':14}})
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Cache-Control','no-store');self.end_headers()
            try:
                deltas=([({'role':'assistant'},None),({'content':'Streaming fixture '+marker[-1]+' '},None)]
                    +[({'content':'more '},None)]*150+[({'content':'COMPLETE '+marker[-1]},None),({},'stop')]
                    if slow_stream else [({'role':'assistant'},None),({'content':response[:10]},None),({'content':response[10:]},None),({},'stop')])
                for delta,finish in deltas:
                    event={**base,'object':'chat.completion.chunk','choices':[{'index':0,'delta':delta,'finish_reason':finish}]}
                    self.wfile.write(('data: '+json.dumps(event)+'\n\n').encode());self.wfile.flush()
                    time.sleep(0.2 if slow_stream else 0.05)
                if include_usage:
                    self.wfile.write(('data: '+json.dumps(usage_event)+'\n\n').encode());self.wfile.flush()
                self.wfile.write(b'data: [DONE]\n\n');self.wfile.flush()
                if slow_stream:print('fixture slow stream completed '+marker[-1],flush=True)
            except (BrokenPipeError,ConnectionResetError):
                if slow_stream:print('fixture slow stream disconnected '+marker[-1],flush=True)
            return
        self.reply({'error':'unsupported_fixture_operation'},400)

ThreadingHTTPServer(('0.0.0.0',8080),Handler).serve_forever()
