#!/usr/bin/env python3
"""Run the disposable Compose profile with public component bootstrap commands."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from compose import command

ROOT=Path(__file__).resolve().parents[1]

def run(args):
    subprocess.run(args,cwd=ROOT,check=True)

def wait_ready(timeout=600):
    config=json.loads(subprocess.check_output(command("config","--format","json"),cwd=ROOT,text=True))
    services=config["services"]
    jobs={name for name,service in services.items()
          if service.get("labels",{}).get("platform.scitrera.io/lifecycle")=="job"}
    deadline=time.monotonic()+timeout
    while True:
        raw=subprocess.check_output(command("ps","--all","--format","json"),cwd=ROOT,text=True).strip()
        rows=json.loads(raw) if raw.startswith("[") else [json.loads(line) for line in raw.splitlines()]
        by_service={row["Service"]:row for row in rows}
        waiting=[]
        for name in services:
            row=by_service.get(name)
            if row is None:
                waiting.append(name)
                continue
            state=row["State"]
            if state in ("dead","exited"):
                if name in jobs and state=="exited" and int(row.get("ExitCode",0))==0:
                    continue
                raise RuntimeError(name+" stopped unexpectedly; inspect its private Compose logs")
            if row.get("Health")=="unhealthy":
                raise RuntimeError(name+" failed its health check; inspect its private Compose logs")
            if name in jobs or state!="running" or row.get("Health") not in ("","healthy",None):
                waiting.append(name)
        if not waiting:return
        if time.monotonic()>deadline:raise RuntimeError("Readiness deadline exceeded: "+", ".join(sorted(waiting)))
        time.sleep(1)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=["up","stop","start","down"])
    parser.add_argument("--fixtures",action="store_true",help="Select synthetic OAuth and inference")
    args=parser.parse_args()
    if args.action in ("stop","start","down"):
        if args.action in ("stop","down"):
            config=json.loads(subprocess.check_output(command("config","--format","json"),cwd=ROOT,text=True))
            allocated=subprocess.check_output(["docker","ps","-aq","--filter",
                "label=scitrera.instance_id="+config["name"]],text=True).strip()
            if allocated:
                parser.error("Release this project's allocated sandboxes through sandbox_release.py before "+args.action)
        run(command(args.action))
        if args.action=="start":wait_ready()
        return
    if not (ROOT/".local/images.json").is_file():
        parser.error("Build or supply selected images first; see docs/compose.md")
    if args.fixtures:
        for script,extra in [("configure.py",[]),("fixtures.py",[]),("gateway_configure.py",["--fixtures","--extend-existing"])]:
            run([sys.executable,str(ROOT/"scripts"/script),*extra])
    if not (ROOT/".local/compose.env").is_file():
        parser.error("Configuration is missing; run configure.py and supply required provider inputs")
    images=json.loads((ROOT/".local/images.json").read_text())
    operator=ROOT/".local/operators.json"
    if not operator.exists():
        run(["docker","run","--rm","--network","none","--user",str(os.getuid())+":"+str(os.getgid()),
             "-v",str(ROOT/".local")+":/out",images["AUTH_IMAGE"]["tag"],
             "bootstrap","--token-file","/out/operators.json","--operator","operator"])
    run(command("config","--quiet"))
    run(command("up","-d","auth"))
    run([sys.executable,str(ROOT/"scripts/auth_setup.py")])
    run(command("up","-d"))
    wait_ready()
    print("Compose services and dependency jobs are ready. External OAuth/provider acceptance remains separate.")

if __name__=="__main__":main()
