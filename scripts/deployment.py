#!/usr/bin/env python3
"""Validate/render one configuration for Compose or Helm without contacting services."""
# SPDX-License-Identifier: AGPL-3.0-only
import argparse
import json
import os
from pathlib import Path
import tempfile

import yaml
from deployment_config import ConfigError, public_summary, resolve
from deployment_render import compose, helm


def write_artifacts(output, artifacts, *, mode=0o644):
    # Rendered policy contains references, never credentials. Non-root containers
    # must be able to read individual bind-mounted files; the output directory
    # stays private to its owner. Secret writers must request mode=0o600.
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    changed = []
    for name, document in sorted(artifacts.items()):
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        content = (document if isinstance(document, str) else
                   json.dumps(document, indent=2, sort_keys=True) + "\n" if name.endswith(".json")
                   else yaml.safe_dump(document, sort_keys=False))
        if path.exists() and path.read_text() == content:
            if path.stat().st_mode & 0o777 != mode:
                path.chmod(mode)
            continue
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".render-")
        try:
            with os.fdopen(fd, "w") as target:
                target.write(content)
                target.flush()
                os.fsync(target.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        changed.append(name)
    return changed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["check", "render"])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--bindings", type=Path)
    parser.add_argument("--runtime", choices=["compose", "helm"])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        resolved = resolve(args.config, profile=args.profile, bindings_path=args.bindings)
        if args.action == "check":
            print(json.dumps(public_summary(resolved), indent=2))
            return
        if not args.runtime or not args.output:
            parser.error("render requires --runtime and --output")
        artifacts = (compose if args.runtime == "compose" else helm)(resolved)
        changed = write_artifacts(args.output, artifacts)
        print(json.dumps({"runtime": args.runtime, "configDigest": resolved["configDigest"], "changed": changed}))
    except (ConfigError, ValueError, OSError) as error:
        parser.exit(2, str(error) + "\n")


if __name__ == "__main__":
    main()
