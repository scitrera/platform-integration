#!/usr/bin/env python3
"""Exercise disposable installed gateway publication, credentials and ledger accounting."""
# SPDX-License-Identifier: AGPL-3.0-only
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import copy
from datetime import datetime,timedelta,timezone
import hashlib
import json
from pathlib import Path
import secrets
import subprocess
import time
import uuid
from compose import command

ROOT=Path(__file__).resolve().parents[1]

class Acceptance:
    def __init__(self,args):
        self.args=args
        if not (ROOT/".local/fixtures.enabled").is_file():
            raise RuntimeError("Only an enabled local fixture installation is supported")
        self.run_id=uuid.uuid4().hex
        self.model="fixture-acceptance-"+self.run_id
        self.directory=ROOT/".local/gateway-acceptance"/self.run_id
        self.directory.mkdir(parents=True,mode=0o700)
        self.images=json.loads((ROOT/".local/images.json").read_text())
        self.jobs=[]
        self.project=args.project
        self.kube=[]
        self.credential_path=None
        self.role_original=None
        self.role_extended=None
        self.role_granted=False
        self.secret_name="gateway-acceptance-"+self.run_id
        self.secret_created=False
        self.catalog_published=False
        self.winner="initial.json"
        if args.profile=="compose":
            env=dict(line.split("=",1) for line in (ROOT/".local/compose.env").read_text().splitlines() if "=" in line)
            if not self.project or not self.project.startswith("platform-smoke-") or env.get("COMPOSE_PROJECT_NAME")!=self.project:
                raise RuntimeError("Supply the exact disposable Compose smoke project")
            self.execute=command("exec","-T","platform-alpha")
            self.db=["docker","exec","-i",self.project+"-sparkroute-postgres-1","psql","-U","sparkroute","-d","sparkroute","-At"]
        else:
            if not args.kubeconfig or args.context!="kind-platform-integration":
                raise RuntimeError("Supply the explicit disposable Kind kubeconfig and context")
            self.kube=["kubectl","--kubeconfig",str(args.kubeconfig.resolve()),"--context",args.context]
            self.execute=self.kube+["-n","tenant-alpha","exec","-i","deployment/alpha-platform","--"]
            self.db=self.kube+["-n","platform-shared","exec","-i","shared-gateway-db-1","-c","postgres","--","psql","-U","postgres","-d","sparkroute","-At"]

    @staticmethod
    def run(argv,*,data=None,check=True,timeout=180):
        result=subprocess.run(argv,input=data,text=True,capture_output=True,timeout=timeout)
        if check and result.returncode:
            raise RuntimeError("Acceptance subprocess failed: "+argv[0]+" (exit "+str(result.returncode)+")")
        return result

    def private_log(self,name,result):
        path=self.directory/name
        path.write_text(result.stdout+"\n"+result.stderr)
        path.chmod(0o600)

    def apply(self,document):
        self.run(self.kube+["apply","-f","-"],data=json.dumps(document))

    def job(self,args,*,python=False,label,probe=False):
        name="gateway-check-"+self.run_id[:8]+"-"+label+"-"+uuid.uuid4().hex[:6]
        self.jobs.append(name)
        labels={"platform.scitrera.io/trust":"service","app.kubernetes.io/component":"model-catalog",
                "platform.scitrera.io/test":"gateway-acceptance","platform.scitrera.io/test-run":self.run_id}
        container={"name":"check","image":self.images["BACKEND_IMAGE" if python else "SPARKROUTE_IMAGE"]["tag"],
            "command":(["python"] if python else ["/sparkroute"])+args,
            "env":[{"name":"AETHER_GATEWAY","value":"alpha-aether:50051"}],
            "securityContext":{"allowPrivilegeEscalation":False,"readOnlyRootFilesystem":True,"capabilities":{"drop":["ALL"]}},
            "resources":{"requests":{"cpu":"50m","memory":"64Mi"},"limits":{"cpu":"1","memory":"256Mi"}},
            "volumeMounts":[{"name":"tls","mountPath":"/run/tls","readOnly":True},
                            {"name":"records","mountPath":"/acceptance","readOnly":True}]}
        if probe:
            container["env"]=[
                {"name":"LLM_GATEWAY_BASE_URL","value":"http://shared-gateway.platform-shared.svc:8080/v1"},
                {"name":"LLM_GATEWAY_BEARER_TOKEN_FILE","value":"/run/gateway/token"}]
            container["volumeMounts"]=[{"name":"caller","mountPath":"/run/gateway","readOnly":True}]
        volumes=([{"name":"caller","secret":{"secretName":"gateway-token"}}] if probe else [
            {"name":"tls","secret":{"secretName":"aether-client-management"}},
            {"name":"records","configMap":{"name":self.secret_name}}])
        self.apply({"apiVersion":"batch/v1","kind":"Job","metadata":{"name":name,"namespace":"tenant-alpha","labels":labels},
            "spec":{"backoffLimit":0,"activeDeadlineSeconds":65,"template":{"metadata":{"labels":labels},"spec":{
                "serviceAccountName":"alpha-runtime","automountServiceAccountToken":False,"restartPolicy":"Never",
                "securityContext":{"runAsNonRoot":True,"runAsUser":1000,"runAsGroup":1000,"fsGroup":1000,
                                   "seccompProfile":{"type":"RuntimeDefault"}},
                "containers":[container],"volumes":volumes}}}})
        deadline=time.monotonic()+90
        while time.monotonic()<deadline:
            state=json.loads(self.run(self.kube+["-n","tenant-alpha","get","job",name,"-o","json"]).stdout)["status"]
            if state.get("succeeded") or state.get("failed"):
                result=self.run(self.kube+["-n","tenant-alpha","logs","job/"+name],check=False)
                self.private_log(label+"-"+name+".log",result)
                if probe:
                    assert state.get("succeeded"),"Gateway probe failed; inspect the private Job log"
                    return json.loads(result.stdout)
                return 0 if state.get("succeeded") else 1
            time.sleep(0.3)
        raise RuntimeError("Acceptance Job timed out: "+name)

    def publish(self,file,expected=None):
        target="aether-alpha" if self.args.profile=="compose" else "alpha-aether"
        args=["catalog","publish","--address",target+":50051","--server-name",target,
            "--implementation","scitrera-management-plane","--specifier","acceptance-"+uuid.uuid4().hex,
            "--ca","/run/tls/ca.crt","--cert","/run/tls/tls.crt","--key","/run/tls/tls.key",
            "--record","/acceptance/"+file]
        if expected:args+=["--expected","/acceptance/"+expected]
        if self.kube:return self.job(args,label=file.split(".")[0])
        result=self.run(command("run","--rm","--no-deps","-v",str(self.directory)+":/acceptance:ro",
            "models-alpha-sahara-default",*args),check=False)
        self.private_log("publish-"+uuid.uuid4().hex+".log",result)
        return result.returncode

    def check_catalog(self,remove=False):
        args=["/acceptance/catalog_record.py","--model",self.model,"--expected","/acceptance/"+self.winner]
        if remove:args.append("--remove")
        if self.kube:
            assert self.job(args,python=True,label="read")==0,"Exact catalog read failed"
        else:
            result=self.run(command("run","--rm","--no-deps","-v",str(self.directory)+":/acceptance:ro",
                "-v",str(ROOT/".local/alpha/tls/management")+":/run/tls:ro",
                "--entrypoint","python","acl-alpha",*args),check=False)
            self.private_log("catalog-read.log",result)
            assert result.returncode==0,"Exact catalog read failed"

    def credential(self,value):
        if self.kube:
            document={"apiVersion":"v1","kind":"Secret","metadata":{"name":self.secret_name,"namespace":"platform-shared"},
                      "type":"Opaque","data":{"api-key":base64.b64encode(value.encode()).decode()}}
            if not self.secret_created:
                self.run(self.kube+["create","-f","-"],data=json.dumps(document))
                self.secret_created=True
            else:
                self.run(self.kube+["-n","platform-shared","patch","secret",self.secret_name,"--type=json","--patch-file=/dev/stdin"],
                    data=json.dumps([{"op":"replace","path":"/data/api-key","value":document["data"]["api-key"]}]))
        else:
            temp=self.credential_path.with_suffix(".new")
            temp.write_text(value)
            temp.chmod(0o600)
            temp.replace(self.credential_path)

    def setup(self):
        record=json.loads((ROOT/".local/alpha/model-catalog/sahara-default.json").read_text())
        if record["providers"][0]["base_url"]!="http://inference:8080/v1":
            raise RuntimeError("Acceptance requires the local deterministic inference fixture")
        record["model"]=self.model
        record["virtual_model"]["name"]=self.model
        record["valid_until"]=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat().replace("+00:00","Z")
        if self.kube:
            # A temporary policy grants only these operator probe pods access
            # to the installed gateway; ordinary platform pods keep their policy.
            self.apply({"apiVersion":"networking.k8s.io/v1","kind":"NetworkPolicy",
                "metadata":{"name":self.secret_name,"namespace":"tenant-alpha"},
                "spec":{"podSelector":{"matchLabels":{"platform.scitrera.io/test-run":self.run_id}},
                    "policyTypes":["Egress"],"egress":[{"to":[{"namespaceSelector":{"matchLabels":{
                        "kubernetes.io/metadata.name":"platform-shared"}},"podSelector":{"matchLabels":{
                        "app.kubernetes.io/component":"gateway"}}}],"ports":[{"protocol":"TCP","port":8080}]}]}})
            ref="k8s://platform-shared/"+self.secret_name+"#api-key"
            role=json.loads(self.run(self.kube+["-n","platform-shared","get","role","shared-catalog-credentials","-o","json"]).stdout)
            self.role_original=role["rules"][0]["resourceNames"]
            self.role_extended=self.role_original+[self.secret_name]
            self.run(self.kube+["-n","platform-shared","patch","role","shared-catalog-credentials","--type=json","--patch-file=/dev/stdin"],
                data=json.dumps([{"op":"test","path":"/rules/0/resourceNames","value":self.role_original},
                    {"op":"replace","path":"/rules/0/resourceNames","value":self.role_extended}]))
            self.role_granted=True
        else:
            path=ROOT/".local/gateway"/(self.secret_name+".key")
            if path.exists():raise RuntimeError("Acceptance credential already exists")
            self.credential_path=path
            ref="file:///run/gateway/"+path.name
        if self.kube:
            record["providers"][0]["base_url"]="http://inference.platform-shared.svc.cluster.local:8080/v1"
        record["providers"][0]["auth"]={"type":"bearer","credential":ref}
        for name in ["initial","left","right"]:
            item=copy.deepcopy(record)
            item["deployments"][0]["model"]="fixture-acceptance-"+name
            (self.directory/(name+".json")).write_text(json.dumps(item,sort_keys=True)+"\n")
        (self.directory/"catalog_record.py").write_text((ROOT/"tests/integration/catalog_record.py").read_text())
        # These files contain public synthetic records, references and code, never credentials.
        # Compose's publisher runs as the generated local UID.
        if self.kube:
            self.apply({"apiVersion":"v1","kind":"ConfigMap","metadata":{"name":self.secret_name,"namespace":"tenant-alpha"},
                "data":{p.name:p.read_text() for p in self.directory.iterdir() if p.suffix in [".json",".py"]}})
        self.key_before=secrets.token_urlsafe(32)
        self.key_after=secrets.token_urlsafe(32)
        self.credential(self.key_before)

    def request(self,key,success):
        task=str(uuid.uuid4())
        digest=hashlib.sha256(key.encode()).hexdigest()
        # The existing trusted backend process already mounts its gateway caller
        # token. The provider key stays only in the gateway credential source.
        program="""import json,os,urllib.request,urllib.error
from pathlib import Path
body=json.dumps(%s).encode()
headers=%s
headers['Authorization']='Bearer '+Path(os.environ['LLM_GATEWAY_BEARER_TOKEN_FILE']).read_text().strip()
request=urllib.request.Request(os.environ['LLM_GATEWAY_BASE_URL']+'/chat/completions',data=body,headers=headers)
try:
 response=urllib.request.urlopen(request,timeout=40)
 print(json.dumps({'status':response.status,'body':json.loads(response.read())}))
except urllib.error.HTTPError as error:
 print(json.dumps({'status':error.code}))
""" % (repr({"model":self.model,"messages":[{"role":"user","content":"fixture-credential-check "+digest}]}),
       repr({"Content-Type":"application/json","X-Scitrera-Tenant":"alpha","X-Scitrera-User":"user:alice@example.test","X-Scitrera-Source":"sahara","X-Scitrera-Workspace":"default","X-Scitrera-Thread-Id":"_default","X-Scitrera-Task-Id":task}))
        if self.kube:
            result=self.job(["-c",program],python=True,label="request",probe=True)
        else:
            result=self.run(self.execute+["python","-"],data=program,check=False)
            self.private_log("gateway-request-"+task+".log",result)
            assert result.returncode==0,"Gateway request process failed; inspect the private request log"
            result=json.loads(result.stdout)
        if success:
            assert result["status"]==200,"Installed gateway request failed"
            assert result["body"]["usage"]=={"prompt_tokens":10,"completion_tokens":4,"total_tokens":14}
        else:
            assert result["status"]==401,"Old provider credential was not refused"
        return task

    def ledger(self,task,model):
        uuid.UUID(task)
        fields=["tenant_id","principal_id","attribution_json","http_status","input_tokens","output_tokens","total_tokens","stream"]
        sql="SELECT json_build_object("+",".join("'"+name+"',"+name for name in fields)+") FROM llm_requests WHERE attribution_json->>'sparkroute.task_id'='"+task+"' AND requested_model='"+model+"' AND completed_at IS NOT NULL;"
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            result=self.run(self.db,data=sql)
            rows=[json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
            if rows:break
            time.sleep(0.2)
        assert len(rows)==1,"Expected one exact task/model ledger row"
        row=rows[0]
        assert row["tenant_id"]=="alpha" and row["principal_id"]=="platform-alpha"
        attrs=row["attribution_json"]
        for key,value in {"user":"user:alice@example.test","source":"sahara","tenant":"alpha",
                          "workspace":"default","thread_id":"_default","task_id":task}.items():
            assert attrs.get("sparkroute."+key)==value,"Incorrect attribution: "+key
        assert row["http_status"]==200
        assert [row[k] for k in ["input_tokens","output_tokens","total_tokens"]]==[10,4,14],"Usage not captured"
        return row

    def cleanup(self):
        errors=[]
        def attempt(label,operation):
            try:operation()
            except Exception as error:errors.append(label+": "+type(error).__name__)
        if self.catalog_published:
            attempt("catalog removal",lambda:self.check_catalog(remove=True))
        if self.credential_path:
            attempt("credential removal",lambda:self.credential_path.unlink(missing_ok=True))
            attempt("partial credential removal",lambda:self.credential_path.with_suffix(".new").unlink(missing_ok=True))
        if self.kube:
            if self.role_granted:
                def remove_grant():
                    # Preserve unrelated operator edits; remove only this run's grant.
                    for _ in range(3):
                        role=json.loads(self.run(self.kube+["-n","platform-shared","get","role","shared-catalog-credentials","-o","json"]).stdout)
                        names=role["rules"][0]["resourceNames"]
                        if self.secret_name not in names:return
                        result=self.run(self.kube+["-n","platform-shared","patch","role","shared-catalog-credentials",
                            "--type=json","--patch-file=/dev/stdin"],check=False,
                            data=json.dumps([{"op":"test","path":"/rules/0/resourceNames","value":names},
                                {"op":"replace","path":"/rules/0/resourceNames","value":[n for n in names if n!=self.secret_name]}]))
                        if result.returncode==0:return
                    raise RuntimeError("Concurrent Role edits prevented scoped cleanup")
                attempt("credential grant removal",remove_grant)
            if self.secret_created:
                attempt("Secret removal",lambda:self.run(self.kube+["-n","platform-shared","delete","secret",self.secret_name,"--ignore-not-found"]))
            if self.jobs:
                attempt("Job removal",lambda:self.run(self.kube+["-n","tenant-alpha","delete","job",*self.jobs,"--wait=false","--ignore-not-found"]))
            for kind in ["configmap","networkpolicy"]:
                attempt(kind+" removal",lambda kind=kind:self.run(self.kube+["-n","tenant-alpha","delete",kind,self.secret_name,"--ignore-not-found"]))
        if errors:raise RuntimeError("Acceptance cleanup incomplete: "+"; ".join(errors))

    def main(self):
        report={}
        if self.args.usage_only:
            if not self.args.usage_record:raise RuntimeError("--usage-only requires --usage-record")
            item=json.loads(self.args.usage_record.read_text())
            assert item["tenant"]=="alpha" and item["email"]=="alice@example.test"
            assert self.ledger(item["task_id"],"sahara-default")["stream"]
            report["browser_sahara_stream_attribution_and_usage"]=True
            (self.directory/"report.json").write_text(json.dumps(report,indent=2)+"\n")
            print(json.dumps(report))
            return
        try:
            self.setup()
            with ThreadPoolExecutor(max_workers=2) as pool:
                result=list(pool.map(self.publish,["initial.json","initial.json"]))
            self.catalog_published=0 in result
            assert result==[0,0],"Concurrent identical publication failed"
            self.check_catalog()
            report["concurrent_idempotent_create"]=True
            with ThreadPoolExecutor(max_workers=2) as pool:
                result=list(pool.map(lambda file:self.publish(file,"initial.json"),["left.json","right.json"]))
            assert sorted(result)==[0,1],"Expected exactly one CAS winner"
            self.winner=["left.json","right.json"][result.index(0)]
            self.check_catalog()
            assert self.publish("initial.json","initial.json")!=0,"Stale publisher overwrote the winner"
            assert self.publish(self.winner)==0,"Identical winner retry failed"
            self.check_catalog()
            report["concurrent_cas_and_stale_refusal"]=True
            before=self.request(self.key_before,True)
            self.credential(self.key_after)
            after=self.request(self.key_after,True)
            self.request(self.key_before,False)
            self.ledger(before,self.model)
            self.ledger(after,self.model)
            report["provider_credential_rotation"]=True
            report["authenticated_gateway_request_attribution_and_usage"]=True
            if self.args.usage_record:
                item=json.loads(self.args.usage_record.read_text())
                assert item["tenant"]=="alpha" and item["email"]=="alice@example.test"
                assert self.ledger(item["task_id"],"sahara-default")["stream"]
                report["browser_sahara_stream_attribution_and_usage"]=True
        finally:
            self.cleanup()
        (self.directory/"report.json").write_text(json.dumps(report,indent=2)+"\n")
        print(json.dumps(report,sort_keys=True))
        print("Private evidence directory:",self.directory)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile",choices=["compose","kind"],required=True)
    parser.add_argument("--project")
    parser.add_argument("--kubeconfig",type=Path)
    parser.add_argument("--context")
    parser.add_argument("--usage-record",type=Path)
    parser.add_argument("--usage-only",action="store_true",help="Read the actual browser turn's ledger without rotating or publishing")
    args=parser.parse_args()
    Acceptance(args).main()

if __name__=="__main__":main()
