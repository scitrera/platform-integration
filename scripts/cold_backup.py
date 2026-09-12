#!/usr/bin/env python3
"""Offline, same-version Compose backup into a private directory; no live snapshots."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
from compose import ROOT, command

CONFIG = ["compose.env", "images.env", "images.json", "oauth.env", "operators.json",
          "storage-credentials.json", "storage-tenants.json", "nginx.conf",
          "fixture.env", "fixtures.enabled", "alpha", "beta", "gateway", "sandbox-state"]

def output(args):
    return subprocess.check_output(args, text=True).strip()

def inspect_config():
    config=json.loads(output(command("config", "--format", "json")))
    project=config["name"]
    # Native allocations have their own lifecycle. Release through the public
    # tenant SDK before backup; never infer a tenant solely from container names.
    allocated=output(["docker","ps","-aq","--filter","label=scitrera.instance_id="+project])
    if allocated:
        raise SystemExit("Release this project's allocations through sandbox_release.py before backup/restore")
    running=output(["docker","ps","-q","--filter","label=com.docker.compose.project="+project])
    if running:
        raise SystemExit("Stop this Compose project first; offline backup/restore requires no running services")
    volumes={}
    for key,value in config.get("volumes",{}).items():
        name=value.get("name", project+"_"+key)
        data=json.loads(output(["docker","volume","inspect",name]))[0]
        labels=data.get("Labels") or {}
        if labels.get("com.docker.compose.project")!=project:
            raise SystemExit("Refusing volume without exact project ownership: "+name)
        volumes[key]=name
    return config,volumes

def digest(path):
    h=hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""):h.update(chunk)
    return h.hexdigest()

def helper(image,mounts,script):
    subprocess.run(["docker","run","--rm","--network","none",
                    *sum((["--mount",m] for m in mounts),[]),image,
                    "python","-c",script],check=True)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=["backup","verify"])
    parser.add_argument("--directory",type=Path,required=True)
    args=parser.parse_args()
    dest=args.directory.resolve()
    if args.action=="verify":
        manifest=json.loads((dest/"manifest.json").read_text())
        for name,record in manifest["archives"].items():
            if Path(name).name!=name:raise SystemExit("Invalid archive name")
            if digest(dest/name)!=record["sha256"]:raise SystemExit("Checksum mismatch: "+name)
        print("Verified archive checksums; restore requires the same component versions and a stopped project")
        return
    config,volumes=inspect_config()
    dest.mkdir(parents=True,exist_ok=False,mode=0o700)
    manifest={"format":1,"project":config["name"],"archives":{},
              "images":json.loads((ROOT/".local/images.json").read_text()),
              "scope":"offline named volumes and selected private configuration; allocations released"}
    image=manifest["images"]["BACKEND_IMAGE"]["id"]
    common=["type=bind,source="+str(dest)+",target=/backup"]
    uid,gid=os.getuid(),os.getgid()
    for key,name in sorted(volumes.items()):
        filename="volume-"+key+".tar.gz"
        script=("import os,tarfile; p="+repr("/backup/"+filename)+"; "
                "t=tarfile.open(p,'w:gz'); t.add('/data',arcname='.'); t.close(); "
                "os.chmod(p,0o600); os.chown(p,"+str(uid)+","+str(gid)+")")
        helper(image,[*common,"type=volume,source="+name+",target=/data,readonly"],script)
        manifest["archives"][filename]={"volume":key,"sha256":digest(dest/filename)}
    script=("import os,tarfile; p='/backup/config.tar.gz'; t=tarfile.open(p,'w:gz'); "
            "[t.add('/config/'+n,arcname=n) for n in "+repr(CONFIG)+" if os.path.exists('/config/'+n)]; "
            "t.close(); os.chmod(p,0o600); os.chown(p,"+str(uid)+","+str(gid)+")")
    helper(image,[*common,"type=bind,source="+str(ROOT/".local")+",target=/config,readonly"],script)
    manifest["archives"]["config.tar.gz"]={"configuration":True,"sha256":digest(dest/"config.tar.gz")}
    path=dest/"manifest.json"
    path.write_text(json.dumps(manifest,indent=2)+"\n")
    path.chmod(0o600)
    print("Created offline backup of",len(volumes),"volumes and private configuration at",dest)

if __name__=="__main__":main()
