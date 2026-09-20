#!/usr/bin/env python3
"""Create a deterministic customer-source archive; excludes config, secrets and data."""
# SPDX-License-Identifier: AGPL-3.0-only
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile

from deployment_config import ConfigError, resolve


def pack(config, resolved, output):
    customer = resolved["deployment"].get("customer")
    if not customer:
        raise ConfigError("No customer source configured")
    root = Path(config).resolve().parent.parent / customer["sourcePath"]
    manifest = resolved["inputs"]["customerSource"]
    revision = resolved["inputDigests"]["customerSource"]
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=output.parent, prefix=".bundle-")
    try:
        with os.fdopen(fd, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as compressed:
            with tarfile.open(fileobj=compressed, mode="w|") as archive:
                entries = {"manifest.json": json.dumps(manifest, sort_keys=True).encode()}
                for name, expected in sorted(manifest.items()):
                    file = root / name
                    if not file.resolve().is_relative_to(root.resolve()):
                        raise ConfigError("Customer source symlink escapes source directory")
                    content = file.read_bytes()
                    if hashlib.sha256(content).hexdigest() != expected:
                        raise ConfigError("Source changed after configuration resolution; render again")
                    entries["src/" + name] = content
                for name, content in sorted(entries.items()):
                    info = tarfile.TarInfo(name)
                    info.size, info.mode, info.uid, info.gid, info.mtime = len(content), 0o644, 1000, 1000, 0
                    archive.addfile(info, io.BytesIO(content))
        os.chmod(temporary, 0o600)
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {"revision": revision, "archiveSHA256": hashlib.sha256(output.read_bytes()).hexdigest(),
            "files": len(manifest), "bytes": output.stat().st_size}


def install_stream(stream, destination, revision):
    """Install to an existing writable claim. Existing revisions are immutable."""
    import shutil
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=root, prefix=".incoming-"))
    try:
        with tarfile.open(fileobj=stream, mode="r|gz") as archive:
            seen, size = set(), 0
            for member in archive:
                parts = Path(member.name).parts
                if (not member.isfile() or member.name in seen or member.name.startswith("/")
                        or ".." in parts or (member.name != "manifest.json" and not member.name.startswith("src/"))):
                    raise ConfigError("Invalid customer bundle member")
                seen.add(member.name)
                size += member.size
                if len(seen) > 10000 or size > 128 * 1024 * 1024:
                    raise ConfigError("Customer bundle exceeds installation limits")
                target = staging / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o644)
        manifest = json.loads((staging / "manifest.json").read_text())
        expected = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        if expected != revision:
            raise ConfigError("Customer bundle revision mismatch")
        if seen != {"manifest.json"} | {"src/" + name for name in manifest}:
            raise ConfigError("Customer bundle contains unlisted files")
        for name, digest in manifest.items():
            if hashlib.sha256((staging / "src" / name).read_bytes()).hexdigest() != digest:
                raise ConfigError("Customer bundle content mismatch")
        target = root / revision
        if target.exists():
            # Verify the entire immutable source tree before treating replay as success.
            prior = {str(p.relative_to(target)): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in target.rglob("*") if p.is_file()}
            current = {str(p.relative_to(staging)): hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in staging.rglob("*") if p.is_file()}
            if prior != current:
                raise ConfigError("Existing customer revision has been modified")
        else:
            staging.rename(target)
        return target
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(pack(args.config, resolve(args.config, profile=args.profile), args.output)))


if __name__ == "__main__":
    main()
