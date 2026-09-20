#!/usr/bin/env python3
"""Install a verified customer bundle in a named Kubernetes PVC revision.

No registry rebuild is needed. The PVC is retained; workload mounts select an
immutable revision. This operator command requires an explicit cluster context.
"""
# SPDX-License-Identifier: AGPL-3.0-only
import argparse
import hashlib
import inspect
import json
from pathlib import Path
import re
import subprocess
import tempfile
import uuid

from customer_bundle import install_stream
from deployment_config import ConfigError


def objects(*, tenant, namespace, storage_class, image, revision, node_selector=None, tolerations=None):
    for value in (tenant, namespace):
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,62}', value):
            raise ConfigError('Invalid tenant or namespace')
    if not re.fullmatch(r'[a-f0-9]{64}', revision):
        raise ConfigError('Invalid source revision')
    claim = tenant + '-customer-code'
    labels = {'platform.scitrera.io/customer-code': tenant}
    pvc = {'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim',
           'metadata': {'name': claim, 'namespace': namespace, 'labels': labels,
                        'annotations': {'helm.sh/resource-policy': 'keep'}},
           'spec': {'accessModes': ['ReadWriteOnce'], 'storageClassName': storage_class,
                    'resources': {'requests': {'storage': '1Gi'}}}}
    pod = {'apiVersion': 'v1', 'kind': 'Pod',
           'metadata': {'name': tenant + '-code-stage-' + uuid.uuid4().hex[:10], 'namespace': namespace,
                        'labels': labels},
           'spec': {'restartPolicy': 'Never', 'activeDeadlineSeconds': 900,
                    'automountServiceAccountToken': False, 'nodeSelector': node_selector or {},
                    'tolerations': tolerations or [], 'terminationGracePeriodSeconds': 5,
                    'securityContext': {'runAsNonRoot': True, 'runAsUser': 1000, 'runAsGroup': 1000,
                                        'fsGroup': 1000, 'seccompProfile': {'type': 'RuntimeDefault'}},
                    'containers': [{'name': 'install', 'image': image,
                        'command': ['python', '-c', 'import time; time.sleep(880)'],
                        'resources': {'requests': {'cpu': '25m', 'memory': '64Mi'},
                                      'limits': {'cpu': '500m', 'memory': '256Mi'}},
                        'securityContext': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                                            'capabilities': {'drop': ['ALL']}},
                        'volumeMounts': [{'name': 'source', 'mountPath': '/customer'}]}],
                    'volumes': [{'name': 'source', 'persistentVolumeClaim': {'claimName': claim}}]}}
    return pvc, pod


def verify_claim(current, desired):
    if current['metadata'].get('labels', {}).get('platform.scitrera.io/customer-code') != desired['metadata']['labels']['platform.scitrera.io/customer-code']:
        raise ConfigError('Refusing to adopt an unowned customer source PVC')
    for key in ('storageClassName', 'accessModes'):
        if current['spec'].get(key) != desired['spec'][key]:
            raise ConfigError('Existing source PVC has different ' + key)
    if current['metadata'].get('deletionTimestamp'):
        raise ConfigError('Source PVC is being deleted')


def installer(revision):
    return ('import sys,json,hashlib,tarfile,tempfile\nfrom pathlib import Path\nConfigError=ValueError\n'
            + inspect.getsource(install_stream) + '\ninstall_stream(sys.stdin.buffer, "/customer", '
            + repr(revision) + ')\nprint("Verified customer source revision installed")\n')


def stage(bundle, kube, desired, pod, revision):
    # Check every archive member and digest before sending any bytes to a cluster.
    with tempfile.TemporaryDirectory(prefix='platform-source-check-') as root:
        with Path(bundle).open('rb') as stream:
            install_stream(stream, root, revision)
    name = desired['metadata']['name']
    result = subprocess.run(kube + ['get', 'pvc', name, '--ignore-not-found', '-o', 'json'],
                            check=True, text=True, capture_output=True)
    if result.stdout.strip():
        verify_claim(json.loads(result.stdout), desired)
    else:
        # Create, never apply, so a racing installer cannot change an existing claim.
        subprocess.run(kube + ['create', '-f', '-'], input=json.dumps(desired), text=True, check=True)
    pod_name = pod['metadata']['name']
    subprocess.run(kube + ['create', '-f', '-'], input=json.dumps(pod), text=True, check=True)
    try:
        subprocess.run(kube + ['wait', '--for=condition=Ready', 'pod/' + pod_name, '--timeout=300s'], check=True)
        with Path(bundle).open('rb') as stream:
            subprocess.run(kube + ['exec', '-i', pod_name, '--', 'python', '-c', installer(revision)],
                           stdin=stream, check=True, timeout=300)
    finally:
        subprocess.run(kube + ['delete', 'pod', pod_name, '--wait=false', '--ignore-not-found'], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--tenant', required=True)
    parser.add_argument('--namespace', required=True)
    parser.add_argument('--context', required=True)
    parser.add_argument('--kubeconfig', type=Path)
    parser.add_argument('--storage-class', required=True)
    parser.add_argument('--image', required=True, help='Published backend image containing Python')
    parser.add_argument('--placement', type=Path, help='Rendered tenant Helm values (placement only)')
    parser.add_argument('--allow-local-image', action='store_true')
    args = parser.parse_args()
    if not args.allow_local_image and not re.fullmatch(r'\S+@sha256:[a-f0-9]{64}', args.image):
        parser.error('Production staging requires a registry image digest')
    from deployment_config import _read
    placement = _read(args.placement) if args.placement else {}
    desired, pod = objects(tenant=args.tenant, namespace=args.namespace, storage_class=args.storage_class,
        image=args.image, revision=args.revision, node_selector=placement.get('nodeSelector'),
        tolerations=placement.get('tolerations'))
    kube = ['kubectl', '--context', args.context, '-n', args.namespace]
    if args.kubeconfig:
        kube += ['--kubeconfig', str(args.kubeconfig.resolve())]
    stage(args.bundle, kube, desired, pod, args.revision)


if __name__ == '__main__':
    main()
