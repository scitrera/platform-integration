#!/usr/bin/env python3
"""Run only this installation's Compose project with its explicit generated inputs."""
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]

def command(*args):
    env=ROOT/'.local/compose.env'
    images=ROOT/'.local/images.env'
    if not env.is_file() or not images.is_file():
        raise SystemExit('Run configure.py and build.py first (.local inputs missing)')
    result=['docker','compose','--project-directory',str(ROOT/'compose'),
            '--env-file',str(env),'--env-file',str(images),'-f',str(ROOT/'compose/compose.yaml')]
    if (ROOT/'.local/fixtures.enabled').exists():
        result += ['-f',str(ROOT/'compose/profiles/fixtures.yaml')]
    return result+list(args)

if __name__=='__main__':
    if any(arg in {'--project-directory','--env-file','-f','--file','-p','--project-name'} for arg in sys.argv[1:]):
        raise SystemExit('Project/file overrides belong in explicit installation configuration')
    raise SystemExit(subprocess.call(command(*sys.argv[1:]),cwd=ROOT))
