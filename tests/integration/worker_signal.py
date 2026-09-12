#!/usr/bin/env python3
"""Pause/resume only the Sahara process in an explicitly selected disposable allocation."""
import argparse
import json
from pathlib import Path
import subprocess
import uuid

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["pause", "resume"])
    parser.add_argument("--profile", choices=["compose", "kind"], required=True)
    parser.add_argument("--sandbox", required=True)
    parser.add_argument("--project")
    parser.add_argument("--pod")
    parser.add_argument("--kubeconfig", type=Path)
    parser.add_argument("--context")
    args = parser.parse_args()
    uuid.UUID(args.sandbox)
    if not (ROOT / ".local/fixtures.enabled").is_file():
        parser.error("This check requires an enabled disposable fixture profile")
    if args.profile == "compose":
        if not args.project or not args.project.startswith("platform-smoke-"):
            parser.error("An explicit disposable smoke project is required")
        names = subprocess.check_output(["docker", "ps", "--filter", "label=scitrera.instance_id=" + args.project,
            "--filter", "label=scitrera.sandbox_id=" + args.sandbox,
            "--filter", "label=scitrera.tenant_id=alpha", "--format", "{{.Names}}"], text=True).splitlines()
        names = [name for name in names if name.startswith("scitrera-sandbox-")]
        if len(names) != 1:
            parser.error("Exactly one running Sahara container must match the full allocation identity")
        execute = ["docker", "exec", names[0]]
    else:
        if not args.kubeconfig or args.context != "kind-platform-integration" or not args.pod:
            parser.error("An explicit disposable kubeconfig, context and pod are required")
        kube = ["kubectl", "--kubeconfig", str(args.kubeconfig.resolve()), "--context", args.context, "-n", "tenant-alpha"]
        pod = json.loads(subprocess.check_output(kube + ["get", "pod", args.pod, "-o", "json"]))
        labels = pod["metadata"]["labels"]
        if labels.get("scitrera.io/sandbox-id") != args.sandbox or labels.get("scitrera.io/tenant-id") != "alpha" or labels.get("scitrera.io/thread-id") != "user-alice-example-test":
            parser.error("Pod does not match the selected alpha/Alice fixture allocation")
        execute = kube + ["exec", args.pod, "-c", "sandbox", "--"]
    program = (
        "from pathlib import Path; import os,signal; "
        "pids=[int(p.name) for p in Path('/proc').iterdir() if p.name.isdigit() "
        "and (p/'exe').exists() and str((p/'exe').resolve())=='/usr/local/bin/scitrera-agent-harness']; "
        "assert len(pids)==1, 'Expected one exact Sahara executable'; "
        "os.kill(pids[0], signal." + ("SIGSTOP" if args.action == "pause" else "SIGCONT") + "); "
        "print('Signalled selected Sahara process')"
    )
    subprocess.run(execute + ["python", "-c", program], check=True)


if __name__ == "__main__":
    main()
