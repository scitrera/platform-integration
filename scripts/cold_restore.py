#!/usr/bin/env python3
"""Restore an offline Compose backup into a new project and empty .local directory."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
from cold_backup import ROOT, CONFIG, digest, helper, output
from compose import command
from compose_tenants import validate_tenants

def validate_archive(path,config=False):
    with tarfile.open(path) as archive:
        allowed = set(CONFIG) | {'alpha', 'beta'}
        if config:
            definitions = [m for m in archive.getmembers() if m.name == 'tenants.json']
            if definitions:
                if len(definitions) != 1 or not definitions[0].isfile():
                    raise SystemExit('Invalid tenant definitions in configuration archive')
                tenants = validate_tenants(json.load(archive.extractfile(definitions[0])))
                allowed = set(CONFIG) | {tenant['slug'] for tenant in tenants}
        for member in archive:
            parts=Path(member.name).parts
            if Path(member.name).is_absolute() or ".." in parts:
                raise SystemExit("Unsafe archive path")
            if not (member.isfile() or member.isdir()):
                raise SystemExit("This restore profile refuses links, devices and special files")
            if config and parts and parts[0] not in allowed:
                raise SystemExit("Configuration archive contains an unrecognized entry")

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory",type=Path,required=True)
    parser.add_argument("--project",required=True,help="A new, unused Compose project name")
    args=parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,62}",args.project):parser.error("Invalid project name")
    backup=args.directory.resolve()
    manifest=json.loads((backup/"manifest.json").read_text())
    if manifest.get("format")!=1:parser.error("Unsupported backup format")
    if args.project==manifest["project"]:parser.error("Use a new project; existing resources are never overwritten")
    local=ROOT/".local"
    if local.exists() and any(local.iterdir()):parser.error("Restore requires an empty .local in a fresh checkout")
    for kind in ["container","volume","network"]:
        ids=output(["docker",kind,"ls","-q","--filter","label=com.docker.compose.project="+args.project])
        if ids:parser.error("Target project already owns Docker resources")
    for name,record in manifest["archives"].items():
        if Path(name).name!=name:parser.error("Invalid archive filename")
        if digest(backup/name)!=record["sha256"]:parser.error("Archive checksum mismatch: "+name)
        validate_archive(backup/name,config=record.get("configuration",False))
    image=manifest["images"]["BACKEND_IMAGE"]["id"]
    output(["docker","image","inspect",image])
    local.mkdir(mode=0o700,exist_ok=True)
    common=["type=bind,source="+str(backup)+",target=/backup,readonly"]
    # Archives were fully checked before creating the first destination. Preserve
    # numeric owners (PostgreSQL/Valkey/Aether differ) and file modes.
    helper(image,[*common,"type=bind,source="+str(local)+",target=/restore"],
           "import tarfile; t=tarfile.open('/backup/config.tar.gz'); t.extractall('/restore',numeric_owner=True,filter='fully_trusted')")
    envfile=local/"compose.env"
    env=dict(line.split("=",1) for line in envfile.read_text().splitlines() if line and not line.startswith("#"))
    env["COMPOSE_PROJECT_NAME"]=args.project
    env["SANDBOX_HOST_ROOT"]=str(local/"sandbox-state")
    envfile.write_text("".join(k+"="+v+"\n" for k,v in env.items()))
    envfile.chmod(0o600)
    config=json.loads(output(command("config","--format","json")))
    expected=set(config["volumes"])
    supplied={v["volume"] for v in manifest["archives"].values() if "volume" in v}
    if supplied!=expected:parser.error("Backup volume set does not match this installation version")
    for filename,record in manifest["archives"].items():
        if "volume" not in record:continue
        key=record["volume"];name=config["volumes"][key]["name"]
        if not name.startswith(args.project+"_"):parser.error("Volume name must belong to the new project")
        if subprocess.run(["docker","volume","inspect",name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0:
            parser.error("Target volume already exists: "+name)
    for filename,record in manifest["archives"].items():
        if "volume" not in record:continue
        key=record["volume"];name=config["volumes"][key]["name"]
        output(["docker","volume","create","--label","com.docker.compose.project="+args.project,
                "--label","com.docker.compose.volume="+key,name])
        helper(image,[*common,"type=volume,source="+name+",target=/restore"],
               "import tarfile; t=tarfile.open("+repr("/backup/"+filename)+"); t.extractall('/restore',numeric_owner=True,filter='fully_trusted')")
    print("Restored",len(expected),"volumes into new project",args.project)
    print("Original volumes are intact. Keep the original stopped while reusing its ports; run dev.py up --fixtures for a fixture backup.")

if __name__=="__main__":main()
