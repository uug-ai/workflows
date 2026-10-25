import copy
from pathlib import Path
import subprocess
import tempfile
import unittest

from check_workflows import WORKFLOWS_DIR, load_yaml, validate_release_workflow


class ReleaseContractsTest(unittest.TestCase):
    def setUp(self):
        self.workflow = load_yaml(WORKFLOWS_DIR / "release-create.yml")

    def errors(self, workflow):
        errors = []
        validate_release_workflow(workflow, errors)
        return errors

    def test_current_release_contract(self):
        self.assertEqual(self.errors(self.workflow), [])

    def test_legacy_defaults_cannot_enable_hardening_or_disable_publication(self):
        for key, value in (("hardened", True), ("publish", False)):
            with self.subTest(key=key):
                workflow = copy.deepcopy(self.workflow)
                workflow["on"]["workflow_call"]["inputs"][key]["default"] = value
                self.assertTrue(self.errors(workflow))

    def test_native_registry_platform_is_required(self):
        self.workflow["jobs"]["hardened-images"]["env"]["TRIVY_PLATFORM"] = "linux/amd64"
        self.assertTrue(self.errors(self.workflow))

    def test_each_hardened_gate_is_required(self):
        steps = self.workflow["jobs"]["hardened-images"]["steps"]
        required = (
            "Enforce vulnerability and secret policy",
            "Scan immutable published image",
            "Gate immutable published image",
            "Smoke test with a read-only filesystem and no capabilities",
        )
        for name in required:
            with self.subTest(name=name):
                workflow = copy.deepcopy(self.workflow)
                workflow["jobs"]["hardened-images"]["steps"] = [
                    step for step in steps if step.get("name") != name
                ]
                self.assertTrue(self.errors(workflow))

    def test_publishing_requires_provenance_and_sbom(self):
        for key, value in (("provenance", False), ("sbom", False)):
            with self.subTest(key=key):
                workflow = copy.deepcopy(self.workflow)
                for step in workflow["jobs"]["hardened-images"]["steps"]:
                    if step.get("name") == "Publish verified image with provenance and SBOM":
                        step["with"][key] = value
                self.assertTrue(self.errors(workflow))

    def test_read_only_callers_do_not_need_an_oidc_permission_grant(self):
        self.assertNotIn("permissions", self.workflow)
        self.assertNotIn("permissions", self.workflow["jobs"]["hardened-manifest"])

    def test_native_images_and_registry_scans_use_platform_package(self):
        for name, key in (
            ("Publish verified image with provenance and SBOM", "tags"),
            ("Scan immutable published image", "image-ref"),
        ):
            with self.subTest(name=name):
                workflow = copy.deepcopy(self.workflow)
                for step in workflow["jobs"]["hardened-images"]["steps"]:
                    if step.get("name") == name:
                        step["with"][key] = step["with"][key].replace("-platform", "")
                self.assertTrue(self.errors(workflow))

    def test_runtime_file_gates_reject_shells_and_special_permissions(self):
        inspection = next(
            step["run"] for step in self.workflow["jobs"]["hardened-images"]["steps"]
            if step.get("name") == "Inspect image contract"
        )
        gates = inspection[inspection.index("if grep -E"):]
        for files, permissions, expected in (
            ("app/main\nusr/lib/libc.so.6\n", "-r-xr-xr-x root/root app/main\n", 0),
            ("app/main\nbin/sh\n", "-r-xr-xr-x root/root app/main\n", 1),
            ("app/main\n", "-rwsr-xr-x root/root app/main\n", 1),
            ("app/main\n", "-r-xr-sr-x root/root app/main\n", 1),
        ):
            with self.subTest(files=files, permissions=permissions):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    (root / "rootfs-files.txt").write_text(files)
                    (root / "rootfs-permissions.txt").write_text(permissions)
                    result = subprocess.run(
                        ["bash", "-e", "-c", gates], cwd=root, capture_output=True, text=True
                    )
                    self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                    if expected:
                        self.assertIn("::error::", result.stdout)

    def test_validation_only_cannot_push_or_promote(self):
        self.workflow["jobs"]["hardened-manifest"]["if"] = "inputs.hardened"
        self.assertTrue(self.errors(self.workflow))

    def test_promotion_must_wait_for_verified_manifest(self):
        self.workflow["jobs"]["update-gitops"]["needs"] = ["build"]
        self.assertTrue(self.errors(self.workflow))


if __name__ == "__main__":
    unittest.main()
