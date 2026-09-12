#!/usr/bin/env python3
"""Run local-fixture reader ACL checks in Compose or the disposable Kind cluster."""
import argparse
import json
from pathlib import Path
import subprocess
import uuid
from compose import command

ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile",choices=["compose","kind"],required=True)
    parser.add_argument("--tenant",choices=["alpha","beta"],default="alpha")
    args=parser.parse_args()
    tenant=args.tenant
    if args.profile=="compose":
        for role,extra in [("management",["--seed"]),("model-catalog",[])]:
            subprocess.run(command("run","--rm","--no-deps",
                "-v",str(ROOT/"tests/integration")+":/checks:ro",
                "-v",str(ROOT/".local"/tenant/"tls"/role)+":/run/tls:ro",
                "--entrypoint","python","acl-"+tenant,"/checks/catalog_acl.py",*extra),check=True)
        return
    k=["kubectl","--kubeconfig",str(ROOT/".local/cluster/kubeconfig"),"--context","kind-platform-integration"]
    images=json.loads((ROOT/".local/images.json").read_text())
    suffix=uuid.uuid4().hex[:8]
    for stage,namespace,secret,target,extra,component in [
        ("seed","tenant-"+tenant,"aether-client-management",tenant+"-aether:50051",["--seed"],"catalog-check"),
        ("reader","platform-shared","aether-sparkroute-creds-"+tenant,
         tenant+"-aether.tenant-"+tenant+".svc.cluster.local:50051",[],"gateway")]:
        name="catalog-acl-"+stage+"-"+suffix
        config={"apiVersion":"v1","kind":"ConfigMap","metadata":{"name":name,"namespace":namespace,
            "labels":{"platform.scitrera.io/test":"catalog-acl"}},
            "data":{"catalog_acl.py":(ROOT/"tests/integration/catalog_acl.py").read_text()}}
        labels={"app.kubernetes.io/component":component,"platform.scitrera.io/trust":"service",
                "platform.scitrera.io/test":"catalog-acl"}
        pod={"automountServiceAccountToken":False,"restartPolicy":"Never",
             "serviceAccountName":tenant+"-runtime" if stage=="seed" else "shared-runtime",
             "securityContext":{"runAsNonRoot":True,"runAsUser":1000,"runAsGroup":1000,"fsGroup":1000,
                                "seccompProfile":{"type":"RuntimeDefault"}},
             "containers":[{"name":"check","image":images["BACKEND_IMAGE"]["tag"],
                "command":["python","/checks/catalog_acl.py",*extra],
                "env":[{"name":"AETHER_GATEWAY","value":target}],
                "securityContext":{"allowPrivilegeEscalation":False,"readOnlyRootFilesystem":True,
                                   "capabilities":{"drop":["ALL"]}},
                "resources":{"requests":{"cpu":"50m","memory":"64Mi"},"limits":{"cpu":"1","memory":"256Mi"}},
                "volumeMounts":[{"name":"check","mountPath":"/checks","readOnly":True},
                                {"name":"tls","mountPath":"/run/tls","readOnly":True}]}],
             "volumes":[{"name":"check","configMap":{"name":name}},{"name":"tls","secret":{"secretName":secret}}]}
        job={"apiVersion":"batch/v1","kind":"Job","metadata":{"name":name,"namespace":namespace,"labels":labels},
             "spec":{"backoffLimit":0,"activeDeadlineSeconds":90,"template":{"metadata":{"labels":labels},"spec":pod}}}
        for document in [config,job]:
            subprocess.run(k+["apply","-f","-"],input=json.dumps(document),text=True,check=True,stdout=subprocess.DEVNULL)
        done=subprocess.run(k+["-n",namespace,"wait","--for=condition=complete","job/"+name,"--timeout=105s"])
        subprocess.run(k+["-n",namespace,"logs","job/"+name],check=True)
        done.check_returncode()

if __name__=="__main__":main()
