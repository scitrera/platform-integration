# SPDX-License-Identifier: AGPL-3.0-only
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from customer_bundle import install_stream, pack
from deployment_config import ConfigError, resolve
import test_deployment_render
from deployment_render import compose, helm


class CustomerDeploymentTests(test_deployment_render.DeploymentRenderTests):
    def setUp(self):
        super().setUp()
        source = self.root / "src"
        source.mkdir()
        (source / "example.py").write_text("VALUE = 42\n")
        (self.root / "skill.md").write_text("---\nname: example\n---\nOnly use permitted sources.")
        self.fixture.config["customer"] = {
            "sourcePath": self.root.name + "/src", "pythonModule": "example.worker",
            "principal": "sv::example::worker", "statePath": "/var/lib/example",
            "readinessFile": "/tmp/ready", "provisionModule": "example.provision",
            "environment": {"EXAMPLE_REVIEW": "/run/customer-config/review.yaml"},
            "provisionEnvironment": {"EXAMPLE_ACCESS": "[]"},
            "workProfileSkills": {"document-review": [self.root.name + "/skill.md"]},
        }

    def test_customer_worker_and_compiled_skills_match(self):
        resolved = self.resolved()
        docker, kube = compose(resolved), helm(resolved)
        worker = docker["compose.yaml"]["services"]["customer-worker-example"]
        self.assertEqual(worker["command"], ["python", "-m", "example.worker"])
        self.assertEqual(kube["helm/tenant.yaml"]["extension"]["workerModule"], "example.worker")
        self.assertEqual(worker["environment"]["EXAMPLE_REVIEW"], "/run/customer-config/review.yaml")
        self.assertIn("Only use permitted sources.", docker["work-profiles.json"]["profiles"]["document-review"]["instructions"])
        self.assertNotIn("name: example", docker["work-profiles.json"]["profiles"]["document-review"]["instructions"])

    def test_archive_determinism_replay_and_modified_revision(self):
        resolved = self.resolved()
        archive = self.root / "bundle.tar.gz"
        # Fixture config is written at root/deployment.yaml by run_config.
        config = self.root / "deployment.yaml"
        first = pack(config, resolved, archive)
        original = archive.read_bytes()
        self.assertEqual(pack(config, resolved, archive), first)
        self.assertEqual(archive.read_bytes(), original)
        destination = self.root / "claim"
        with archive.open("rb") as stream:
            installed = install_stream(stream, destination, first["revision"])
        with archive.open("rb") as stream:
            self.assertEqual(install_stream(stream, destination, first["revision"]), installed)
        (installed / "src/example.py").write_text("modified")
        with archive.open("rb") as stream, self.assertRaises(ConfigError):
            install_stream(stream, destination, first["revision"])

    def test_archive_rejects_traversal_and_links(self):
        for name, kind in (("../outside", tarfile.REGTYPE), ("src/link", tarfile.SYMTYPE)):
            output = io.BytesIO()
            with tarfile.open(fileobj=output, mode="w:gz") as archive:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.linkname = "/tmp/outside"
                archive.addfile(member)
            with self.assertRaises(ConfigError):
                install_stream(io.BytesIO(output.getvalue()), self.root / "claim", "1"*64)

    def test_stage_is_immutable_private_and_rejects_unowned_claim(self):
        from customer_stage import objects, verify_claim, installer
        pvc, pod = objects(tenant="example", namespace="tenant-example", storage_class="block",
            image="backend", revision="1"*64, node_selector={"pool": "example"})
        verify_claim(pvc, pvc)
        import copy
        unrelated = copy.deepcopy(pvc)
        unrelated["metadata"]["labels"] = {}
        with self.assertRaises(ConfigError):
            verify_claim(unrelated, pvc)
        self.assertFalse(pod["spec"]["automountServiceAccountToken"])
        self.assertEqual(pod["spec"]["nodeSelector"], {"pool": "example"})
        self.assertEqual(pod["spec"]["activeDeadlineSeconds"], 900)
        # Execute the actual standalone payload used inside the staging container.
        resolved = self.resolved()
        bundle = self.root / "source.tar.gz"
        info = pack(self.root / "deployment.yaml", resolved, bundle)
        destination = str(self.root / "installed")
        code = installer(info["revision"]).replace('"/customer"', repr(destination))
        import subprocess, sys
        with bundle.open("rb") as stream:
            subprocess.run([sys.executable, "-c", code], stdin=stream, check=True, capture_output=True)
        self.assertEqual((Path(destination)/info["revision"]/"src/example.py").read_text(), "VALUE = 42\n")
