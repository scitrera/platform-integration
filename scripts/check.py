#!/usr/bin/env python3
"""Offline chart/Compose contract checks. Synthetic image digests are render-only."""
# SPDX-License-Identifier: AGPL-3.0-only
import ast
import copy
import json
import hashlib
from pathlib import Path
import subprocess
import tempfile
import yaml

ROOT = Path(__file__).resolve().parents[1]


def main():
    manifest = yaml.safe_load((ROOT / "versions.yaml").read_text())
    for name, source in manifest["source_sets"].items():
        patch = source.get("patch")
        if patch:
            assert hashlib.sha256((ROOT / patch).read_bytes()).hexdigest() == source["patch_sha256"], name
    for name, artifact in manifest["images"].items():
        assert artifact["local_image_id"].startswith("sha256:") and len(artifact["local_image_id"]) == 71, name
        if artifact.get("source_set"):
            assert artifact["source_set"] in manifest["source_sets"], name
    for name, chart in manifest["charts"].items():
        actual = yaml.safe_load((ROOT / "charts" / name / "Chart.yaml").read_text())
        assert chart["version"] == actual["version"] and chart["dependencies"] == actual["dependencies"], name
    for folder in ("scripts", "tests"):
        for path in (ROOT / folder).rglob("*.py"):
            ast.parse(path.read_text(), filename=str(path))
    compose = yaml.safe_load((ROOT / "compose/compose.yaml").read_text())
    for name, service in compose["services"].items():
        if service.get("restart") == "no":
            assert service.get("labels", {}).get("platform.scitrera.io/lifecycle") == "job", name
        for mount in service.get("volumes", []):
            if "docker.sock" in str(mount):
                assert name.startswith("provider-"), name
    for kind, last in [("shared", 2), ("storage", 1), ("tenant", 4)]:
        chart = ROOT / "charts" / ("platform-" + kind)
        values = yaml.safe_load((chart / "values.yaml").read_text())
        values["images"] = {key: "registry.example.invalid/" + key.lower() + ":17.11@sha256:" + "1" * 64 for key in values["images"]}
        values["storage"]["className"] = "synthetic"
        if kind == "storage":
            values["uploadProxy"] = {"enabled": True, "endpoint": "http://objects.example.test:9000", "signedHost": "objects:9000"}
        if kind == "shared":
            values["modelCatalog"]["allowedProviderHosts"] = ["provider.example.test"]
            values["modelCatalog"]["credentialSecretNames"] = ["catalog-example"]
        with tempfile.TemporaryDirectory(prefix="platform-render-") as temp:
            path = Path(temp) / "values.yaml"
            path.write_text(yaml.safe_dump(values))
            subprocess.run(["helm", "lint", str(chart), "-f", str(path), "--kube-version", "1.34.0"], check=True)
            for phase in range(last + 1):
                rendered = subprocess.check_output(["helm", "template", "example", str(chart),
                    "-n", "example", "-f", str(path), "--set", "phase=" + str(phase),
                    "--kube-version", "1.34.0"], text=True)
                assert "argocd.argoproj.io" not in rendered
                assert "docker.sock" not in rendered
                assert "x_tenant_teardown" not in rendered
                for obj in yaml.safe_load_all(rendered):
                    if not obj:
                        continue
                    assert obj["kind"] != "Secret", "Charts must consume existing Secrets"
                    if obj["kind"] in ("Cluster", "PersistentVolumeClaim"):
                        assert obj["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"
                    if kind == "storage" and obj["kind"] == "NetworkPolicy" and obj["metadata"]["name"] == "example-download":
                        assert any(p["port"] == 9000 for rule in obj["spec"]["egress"] for p in rule.get("ports", []))
                    if kind == "storage" and obj["kind"] == "ConfigMap" and obj["metadata"]["name"] == "example-download":
                        conf = obj["data"]["nginx.conf"]
                        assert "limit_except PUT" in conf and "$request_uri $artifact_upload_path" in conf
                    if obj["kind"] == "Job":
                        assert obj["spec"]["activeDeadlineSeconds"] <= 600
                        assert obj["spec"]["template"]["spec"]["restartPolicy"] == "Never"
            invalid = copy.deepcopy(values)
            invalid["images"][next(iter(invalid["images"]))] = "unversioned:latest"
            path.write_text(yaml.safe_dump(invalid))
            failure = subprocess.run(["helm", "template", "example", str(chart), "-f", str(path),
                                      "--kube-version", "1.34.0"], capture_output=True, text=True)
            assert failure.returncode != 0 and "registry manifest digest" in failure.stderr
        print("Checked all lifecycle phases and digest refusal:", kind)
    subprocess.run(["python3", "-m", "unittest", "discover", "-s", str(ROOT / "tests/unit")], check=True)
    print("Static integration checks passed. These do not establish installation or runtime acceptance.")


if __name__ == "__main__":
    main()
