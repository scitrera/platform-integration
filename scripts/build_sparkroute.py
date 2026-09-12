#!/usr/bin/env python3
"""Build an explicitly supplied, licensed SparkRoute composition locally."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
from build import ROOT, LOCAL, run, snapshot, output

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enterprise-source", type=Path, required=True)
    parser.add_argument("--oss-source", type=Path, required=True)
    parser.add_argument("--include-untracked", type=Path, help="Reviewed JSON list of enterprise source paths")
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args()
    extras = json.loads(args.include_untracked.read_text()) if args.include_untracked else []
    enterprise, ep = snapshot("sparkroute-enterprise-private", args.enterprise_source.resolve(), args.allow_dirty, extras)
    oss, op = snapshot("sparkroute-oss", args.oss_source.resolve(), args.allow_dirty)
    source_hash = hashlib.sha256(json.dumps([ep, op], sort_keys=True).encode()).hexdigest()
    context = LOCAL / ("sparkroute-build-" + source_hash[:16])
    if not context.exists():
        context.mkdir()
        shutil.copytree(enterprise / "enterprise", context / "enterprise")
        shutil.copytree(oss, context / "oss")
        (context / "go.work").write_text("go 1.26.8\n\nuse (\n ./oss\n ./enterprise\n)\n")
    tag = "platform-integration/sparkroute-enterprise:" + source_hash[:16]
    bases = {
      "GO_IMAGE": "golang:1.26.8-bookworm@sha256:9fdc884aacc3bec89b20ffc69f4bb369c78210e3e4f600387b5128b12c199f81",
      "NODE_IMAGE": "node:24.13.0-bookworm-slim@sha256:4660b1ca8b28d6d1906fd644abe34b2ed81d15434d26d845ef0aced307cf4b6f",
      "RUNTIME_IMAGE": "gcr.io/distroless/static-debian12:nonroot@sha256:afa5c872c891853ca7fcf1f12c3edb23f7eeef36189728842dd51042ff57f7ab",
    }
    flags = [v for key, value in bases.items() for v in ["--build-arg", key+"="+value]]
    run(["docker", "build", *flags, "-f", str(context / "enterprise/Dockerfile"), "-t", tag, str(context)])
    run(["python3", str(ROOT / "scripts/build.py"), "--use-image", "SPARKROUTE_IMAGE="+tag])
    records = json.loads((LOCAL / "images.json").read_text())
    records["SPARKROUTE_IMAGE"]["provenance"] = {
        "enterprise": ep, "oss": op, "build_bases": bases, "composition_sha256": source_hash,
        "distribution": "Operator-supplied licensed input; public distribution permission not established",
    }
    (LOCAL / "images.json").write_text(json.dumps(records, indent=2)+"\n")

if __name__ == "__main__":
    main()
