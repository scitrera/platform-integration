#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-License-Identifier: AGPL-3.0-only
"""Build the public-source platform-sparkroute distribution locally; never publish."""
import argparse
import json
from pathlib import Path
from build import ROOT, LOCAL, run, snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="platform-sparkroute Git checkout")
    parser.add_argument("--include-untracked", type=Path, help="Reviewed JSON list of additional distribution source paths")
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args()
    extras = json.loads(args.include_untracked.read_text()) if args.include_untracked else []
    source, provenance = snapshot("platform-sparkroute", args.source.resolve(), args.allow_dirty, extras)
    run(["python3", str(source / "scripts/check-release.py")])
    upstream = json.loads((source / "upstream-source.json").read_text())
    tag = "platform-integration/platform-sparkroute:" + provenance["tracked_content_sha256"][:16]
    revision = provenance["revision"] + ("-dirty" if provenance["dirty"] else "")
    run(["docker", "build", "--build-arg", "COMMIT=" + revision,
         "--build-arg", "UPSTREAM_VERSION=" + upstream["version"],
         "--build-arg", "UPSTREAM_COMMIT=" + upstream["revision"],
         "-t", tag, str(source)])
    run(["python3", str(ROOT / "scripts/build.py"), "--use-image", "SPARKROUTE_IMAGE=" + tag])
    records = json.loads((LOCAL / "images.json").read_text())
    records["SPARKROUTE_IMAGE"]["provenance"] = {
        "platform_sparkroute": provenance,
        "upstream": upstream,
        "distribution": "AGPL-3.0-only public-source candidate; local build, no publication performed",
    }
    (LOCAL / "images.json").write_text(json.dumps(records, indent=2) + "\n")


if __name__ == "__main__":
    main()
