#!/usr/bin/env python3
"""Run only this installation's Compose project with its explicit generated inputs."""
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]

def command(*args, overlays=()):
    env=ROOT/'.local/compose.env'
    images=ROOT/'.local/images.env'
    if not env.is_file() or not images.is_file():
        raise SystemExit('Run configure.py and build.py first (.local inputs missing)')
    generated=ROOT/'.local/compose.yaml'
    if (ROOT/'.local/tenants.json').exists() and not generated.is_file():
        raise SystemExit('Generated tenant Compose configuration is missing; rerun configure.py')
    base=generated if generated.is_file() else ROOT/'compose/compose.yaml'
    fixtures=ROOT/'.local/fixtures.yaml' if generated.is_file() else ROOT/'compose/profiles/fixtures.yaml'
    result=['docker','compose','--project-directory',str(ROOT/'compose'),
            '--env-file',str(env),'--env-file',str(images),'-f',str(base)]
    if (ROOT/'.local/fixtures.enabled').exists():
        result += ['-f',str(fixtures)]
    modal=ROOT/'.local/compose.modal.yaml'
    if modal.is_file():
        result += ['-f',str(modal)]
    for overlay in overlays:
        overlay = Path(overlay).resolve()
        if not overlay.is_file():
            raise ValueError("Compose overlay does not exist: " + str(overlay))
        result += ['-f', str(overlay)]
    return result+list(args)

if __name__=='__main__':
    if any(arg in {'--project-directory','--env-file','-f','--file','-p','--project-name'} for arg in sys.argv[1:]):
        raise SystemExit('Project/file overrides belong in explicit installation configuration')
    raise SystemExit(subprocess.call(command(*sys.argv[1:]),cwd=ROOT))
