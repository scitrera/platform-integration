#!/usr/bin/env python3
"""Disrupt an isolated session release to qualify promotion and quorum recovery."""
import argparse
import json
from pathlib import Path
import subprocess
import time
import uuid


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--context', required=True)
    p.add_argument('--namespace', required=True)
    p.add_argument('--release', required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--allow-disruption', action='store_true', required=True)
    a = p.parse_args()
    base = ['kubectl', '--context', a.context, '-n', a.namespace]
    evidence = {'context': a.context, 'namespace': a.namespace, 'release': a.release, 'checks': []}
    key = 'qualification:' + uuid.uuid4().hex

    def kube(*args, check=True):
        r = subprocess.run(base + list(args), text=True, capture_output=True, timeout=50)
        if check and r.returncode:
            raise RuntimeError(r.stderr.strip())
        return r.stdout.strip()

    def cli(pod, *args, port=6379, check=True):
        return kube('exec', pod, '--', 'valkey-cli', '--raw', '-p', str(port), *args, check=check)

    def until(fn, message, timeout=100):
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            try:
                value = fn()
                if value:
                    return round(time.monotonic() - start, 3)
            except (RuntimeError, subprocess.TimeoutExpired):
                pass
            time.sleep(1)
        raise RuntimeError(message)

    voters = [f'{a.release}-sentinel-{i}' for i in range(3)]
    data = [f'{a.release}-data-{i}' for i in range(2)]

    def primary():
        votes = [cli(v, 'SENTINEL', 'get-master-addr-by-name', 'sessions', port=26379).splitlines()[0] for v in voters]
        for v in votes:
            if votes.count(v) >= 2:
                return v.split('.')[0]
        raise RuntimeError('No primary majority')

    def ready():
        pods = json.loads(kube('get', 'pods', '-l', 'app.kubernetes.io/instance=' + a.release, '-o', 'json'))['items']
        return len(pods) == 5 and all(any(c['type'] == 'Ready' and c['status'] == 'True' for c in p['status'].get('conditions', [])) for p in pods)

    def record(check, seconds):
        evidence['checks'].append({'check': check, 'seconds': seconds, 'passed': True})
        print(f'{check}: passed ({seconds}s)', flush=True)

    evidence['resourcesBefore'] = kube('top', 'pods', '--containers', check=False)
    restore_voters = False
    try:
        until(ready, 'Session release not healthy')
        old = primary()
        assert cli(old, 'SET', key, 'synthetic-session', 'EX', '172800') == 'OK'
        until(lambda: all(cli(p, 'GET', key) == 'synthetic-session' for p in data), 'Session did not replicate')
        started = time.monotonic()
        assert cli(voters[0], 'SENTINEL', 'failover', 'sessions', port=26379) == 'OK'
        until(lambda: primary() != old and cli(primary(), 'GET', key) == 'synthetic-session', 'Promotion lost session')
        record('promotion_preserves_session', round(time.monotonic() - started, 3))
        new = primary()
        started = time.monotonic()
        kube('delete', 'pod', old, '--wait=false')
        until(ready, 'Old primary failed to rejoin', timeout=180)
        assert primary() == new
        assert cli(old, 'ROLE').splitlines()[0] in ['slave', 'replica']
        assert cli(new, 'GET', key) == 'synthetic-session'
        record('old_primary_rejoins_without_erasing_session', round(time.monotonic() - started, 3))
        assert cli(new, 'DEL', key) == '1'
        until(lambda: all(cli(p, 'EXISTS', key) == '0' for p in data), 'Logout did not replicate')
        record('revocation_replicates', 0)
        # Remove quorum only in this isolated release. Restore it even on failure.
        restore_voters = True
        kube('scale', 'statefulset', a.release + '-sentinel', '--replicas=1')
        started = time.monotonic()
        until(lambda: cli(new, 'PING', check=False) != 'PONG', 'Minority primary kept serving without quorum', timeout=70)
        record('minority_primary_stops_serving', round(time.monotonic() - started, 3))
        kube('scale', 'statefulset', a.release + '-sentinel', '--replicas=3')
        restore_voters = False
        seconds = until(ready, 'Recovery after restoring voters failed', timeout=240)
        assert cli(primary(), 'EXISTS', key) == '0'
        assert cli(primary(), 'SET', key, 'new-synthetic-login', 'EX', '172800') == 'OK'
        assert cli(primary(), 'GET', key) == 'new-synthetic-login'
        record('quorum_recovery_accepts_new_login', seconds)
        evidence['passed'] = True
    finally:
        if restore_voters:
            kube('scale', 'statefulset', a.release + '-sentinel', '--replicas=3')
        try:
            cli(primary(), 'DEL', key)
        except Exception:
            pass  # synthetic record also has a bounded TTL
        evidence['resourcesAfter'] = kube('top', 'pods', '--containers', check=False)
        a.output.parent.mkdir(parents=True, exist_ok=True)
        a.output.write_text(json.dumps(evidence, indent=2) + '\n')


if __name__ == '__main__':
    main()
