#!/usr/bin/env python3
"""Build selected component snapshots locally and record image IDs; never publish."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess

ROOT=Path(__file__).resolve().parents[1]
LOCAL=ROOT/'.local'
NGINX='nginx:1.28.2-alpine@sha256:5b4900b042ccfa8b0a73df622c3a60f2322faeb2be800cbee5aa7b44d241649e'
RECIPES={
    'AUTH_IMAGE':('auth-go','Dockerfile'),
    'AETHER_IMAGE':('aether','server/Dockerfile'),
    'BACKEND_IMAGE':('platform-backend','backend/Dockerfile'),
    'SAHARA_IMAGE':('platform-backend','agent-harness/sahara/Dockerfile'),
    'SIDECAR_IMAGE':('platform-backend','sandbox/images/sandbox-sidecar2/Dockerfile'),
    'PROVIDER_IMAGE':('platform-backend','sandbox-provider/Dockerfile'),
    'CODE_IMAGE':('platform-backend','sandbox/sandboxes/sahara-code-sidecar/Dockerfile'),
    'CODE_BASE_IMAGE':('platform-backend','sandbox/sandboxes/sahara-code-sidecar/Dockerfile_base'),
    'TOOLS_IMAGE':('platform-backend','tools-wss/Dockerfile'),
    'SKILLS_IMAGE':('platform-backend','backend/Dockerfile.skills-seeder'),
    'ORCHESTRATOR_IMAGE':('platform-backend','orchestrators/Dockerfile.k8s'),
    'MEMORYLAYER_IMAGE':('memorylayer-enterprise','docker/Dockerfile.enterprise'),
    'CONNECTORS_IMAGE':('memorylayer-enterprise','docker/Dockerfile.data-connectors'),
    'ML_POSTGRES_IMAGE':('memorylayer-enterprise','postgres-container/Dockerfile'),
    'ML_CNPG_IMAGE':('memorylayer-enterprise','postgres-container/Dockerfile.cnpg'),
    'EDGE_IMAGE':('memorylayer-storage','docker/Dockerfile.blobgw-edge'),
    'BLOBGW_IMAGE':('memorylayer-storage','docker/Dockerfile.blobgw'),
}

def run(args, **kwargs):
    return subprocess.run(args,check=True,**kwargs)

def output(args,**kwargs):
    return subprocess.check_output(args,text=True,**kwargs).strip()

def snapshot(name,path,allow_dirty,extra_files=()):
    revision=output(['git','rev-parse','HEAD'],cwd=path)
    status=output(['git','status','--porcelain','--untracked-files=no'],cwd=path)
    if status and not allow_dirty:
        raise SystemExit(f'{name} has tracked changes; review and pass --allow-dirty explicitly')
    digest=hashlib.sha256()
    files=[]
    for rel in sorted(set(output(['git','ls-files','-z'],cwd=path).split('\0')) | set(extra_files)):
        if Path(rel).is_absolute() or '..' in Path(rel).parts:
            raise SystemExit('Source paths must stay inside their component')
        source=path/rel
        if source.is_symlink():
            raise SystemExit(f'Symlink must be reviewed before build: {name}/{rel}')
        if source.is_file():
            data=source.read_bytes()
            digest.update(rel.encode()+b'\0'+hashlib.sha256(data).digest())
            files.append((rel,data,source.stat().st_mode & 0o777))
    content_hash=digest.hexdigest()
    target=LOCAL/'sources'/f'{name}-{content_hash[:16]}'
    if not target.exists():
        target.mkdir(parents=True)
        for rel,data,mode in files:
            dest=target/rel
            dest.parent.mkdir(parents=True,exist_ok=True)
            dest.write_bytes(data)
            dest.chmod(mode)
    return target, {'revision':revision,'dirty':bool(status),'tracked_content_sha256':content_hash,'included_untracked':list(extra_files)}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources',type=Path,help='JSON object mapping component names to explicit local Git directories')
    parser.add_argument('--allow-dirty',action='store_true')
    parser.add_argument('--include-untracked',type=Path,help='Reviewed JSON allowlist mapping component names to new source files')
    parser.add_argument('--select',nargs='+',default=[])
    parser.add_argument('--use-image',action='append',default=[],metavar='KEY=TAG',
                        help='Record an operator-supplied image; verifies its ID, not its source provenance')
    args=parser.parse_args()
    LOCAL.mkdir(exist_ok=True)
    record_path=LOCAL/'images.json'
    records=json.loads(record_path.read_text()) if record_path.exists() else {}
    sources=json.loads(args.sources.read_text()) if args.sources else {}
    extras=json.loads(args.include_untracked.read_text()) if args.include_untracked else {}
    selected=args.select
    snapshots={}
    for key in selected:
        name='platform-frontend' if key=='WEB_IMAGE' else RECIPES[key][0]
        if name not in sources: parser.error(f'missing explicit source path for {name}')
        if name not in snapshots:
            snapshots[name]=snapshot(name,Path(sources[name]).resolve(),args.allow_dirty,extras.get(name,[]))
        source,provenance=snapshots[name]
        revision=provenance['revision']+('-dirty' if provenance['dirty'] else '')
        tag='platform-integration/'+key.lower().replace('_image','')+':'+provenance['tracked_content_sha256'][:16]
        built_at=datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
        if key=='WEB_IMAGE':
            env=os.environ.copy()
            for item in list(env):
                if item.startswith('VITE_'): env.pop(item)
            env['BUILD_REVISION']=revision
            env['BUILD_TIMESTAMP']=built_at
            for folder in ['vendor/messaging-spec/typescript','web']:
                run(['npm','ci','--prefix',str(source/folder)],env=env)
                run(['npm','run','build','--prefix',str(source/folder)],env=env)
            run(['python3','scripts/artifacts.py','web'],cwd=source,env=env)
            run(['docker','build','--build-arg','NGINX_IMAGE='+NGINX,'-f',str(ROOT/'images/web/Dockerfile'),
                 '-t',tag,str(source/'web/dist')])
        else:
            dependency_args=['--build-arg','BUILD_TIMESTAMP='+built_at] if key=='BACKEND_IMAGE' else []
            if key=='SAHARA_IMAGE':
                # The installed web profile consumes current-window live tools.
                dependency_args=['--build-arg','SAHARA_BRIDGE_TOOLS_AUTODISCOVER=true']
            if key=='CODE_IMAGE':
                if 'CODE_BASE_IMAGE' not in records:parser.error('Build or supply CODE_BASE_IMAGE before CODE_IMAGE')
                dependency_args=['--build-arg','SANDBOX_BASE_IMAGE='+records['CODE_BASE_IMAGE']['tag']]
            if key in {'CODE_IMAGE','CODE_BASE_IMAGE'}:
                dependency_args += ['--build-arg','SCITRERA_GIT_COMMIT='+revision]
            if key=='SIDECAR_IMAGE':
                if 'AETHER_IMAGE' not in records:parser.error('Build or supply AETHER_IMAGE before SIDECAR_IMAGE')
                dependency_args=['--build-arg','AETHER_IMAGE='+records['AETHER_IMAGE']['tag']]
            run(['docker','build',*dependency_args,'--label','org.opencontainers.image.revision='+revision,
                 '--build-arg','SOURCE_REVISION='+revision,'--build-arg','REVISION='+revision,
                 '-f',str(source/RECIPES[key][1]),'-t',tag,str(source)])
        if key=='MEMORYLAYER_IMAGE':
            # A fresh enterprise wrapper can still embed an obsolete core client.
            run(['python3', str(ROOT/'tests/acceptance/memorylayer_document_client.py'), '--image', tag])
        if key=='ML_CNPG_IMAGE':
            # CNPG validates the PostgreSQL version in the image tag.
            version=output(['docker','run','--rm','--network','none','--entrypoint','postgres',tag,'--version'])
            match=re.match(r'postgres [(]PostgreSQL[)] ([0-9]+[.][0-9]+)',version)
            if not match:raise RuntimeError('Cannot determine the CNPG operand PostgreSQL version')
            versioned=tag.rsplit(':',1)[0]+':'+match.group(1)+'-'+provenance['tracked_content_sha256'][:16]
            run(['docker','tag',tag,versioned])
            tag=versioned
        records[key]={'tag':tag,'provenance':provenance,'built_at':built_at}
        if key=='SAHARA_IMAGE':records[key]['build_args']={'SAHARA_BRIDGE_TOOLS_AUTODISCOVER':'true'}
        if key=='SIDECAR_IMAGE':records[key]['dependency_images']={'aether':output(['docker','image','inspect','--format','{{.Id}}',records['AETHER_IMAGE']['tag']])}
        if key=='CODE_IMAGE':records[key]['dependency_images']={'code_base':output(['docker','image','inspect','--format','{{.Id}}',records['CODE_BASE_IMAGE']['tag']])}
        if key=='CODE_BASE_IMAGE':
            # The component's second-stage Dockerfile has this documented base contract.
            run(['docker','tag',tag,'platform-backend/sahara-code-sidecar-base:dev'])
    for value in args.use_image:
        key,tag=value.split('=',1)
        if key not in RECIPES and key not in {'WEB_IMAGE','SPARKROUTE_IMAGE'}:parser.error('Unknown image key '+key)
        records[key]={'tag':tag,'provenance':{'source_verification':'operator-supplied; not established by build.py'}}
    for record in records.values():
        data=json.loads(output(['docker','image','inspect',record['tag']]))[0]
        record.update(id=data['Id'],architecture=data['Architecture'],os=data['Os'],
                      labels=data['Config'].get('Labels') or {},repo_digests=data.get('RepoDigests',[]))
    record_path.write_text(json.dumps(records,indent=2)+'\n')
    # IDs pin this local engine's image content; they are not registry manifest digests.
    (LOCAL/'images.env').write_text(''.join(f'{k}={v["tag"] if k in {"SAHARA_IMAGE","CODE_IMAGE","SIDECAR_IMAGE"} else v["id"]}\n' for k,v in sorted(records.items())))
    print('Recorded',len(records),'local images in .local/images.json and images.env')

if __name__=='__main__':main()
