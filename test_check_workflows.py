import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from check_workflows import (
    WORKFLOWS_DIR,
    load_yaml,
    validate_release_bump_workflow,
    validate_release_workflow,
)


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
        for key, value in (("hardened", True), ("publish", False), ("finalize_release", True)):
            with self.subTest(key=key):
                workflow = copy.deepcopy(self.workflow)
                workflow["on"]["workflow_call"]["inputs"][key]["default"] = value
                self.assertTrue(self.errors(workflow))

    def test_native_registry_platform_is_required(self):
        self.workflow["jobs"]["hardened-images"]["env"]["TRIVY_PLATFORM"] = "linux/amd64"
        self.assertTrue(self.errors(self.workflow))

    def test_worker_profile_is_the_compatible_default(self):
        self.workflow["on"]["workflow_call"]["inputs"]["hardened_profile"]["default"] = "frontend"
        self.assertTrue(self.errors(self.workflow))

    def test_new_runtime_options_preserve_existing_defaults(self):
        for name, value in (("hardened_command", '["serve"]'),
                            ("hardened_workdir", "/home/other"),
                            ("browser_inventory", True),
                            ("hardened_dockerfile", "Dockerfile.cv"),
                            ("worker_additional_smoke", True)):
            with self.subTest(name=name):
                workflow = copy.deepcopy(self.workflow)
                workflow["on"]["workflow_call"]["inputs"][name]["default"] = value
                self.assertTrue(self.errors(workflow))

    def test_repository_smoke_cannot_be_missing_or_unconditional(self):
        for condition in (None, "always()"):
            with self.subTest(condition=condition):
                workflow = copy.deepcopy(self.workflow)
                steps = workflow["jobs"]["hardened-images"]["steps"]
                for step in steps:
                    if step.get("name") == "Smoke test repository runtime contract":
                        if condition is None:
                            steps.remove(step)
                        else:
                            step["if"] = condition
                        break
                self.assertTrue(self.errors(workflow))

    def test_selected_dockerfile_is_used_by_every_native_build(self):
        for name in ("Run native tests and vulnerability reachability checks",
                     "Build final image for inspection", "Publish staged image with provenance and SBOM"):
            with self.subTest(name=name):
                workflow = copy.deepcopy(self.workflow)
                for step in workflow["jobs"]["hardened-images"]["steps"]:
                    if step.get("name") == name:
                        if "uses" in step:
                            step["with"].pop("file")
                        else:
                            step["env"].pop("DOCKERFILE")
                self.assertTrue(self.errors(workflow))

    def test_base_evidence_uses_the_selected_or_context_dockerfile(self):
        recording = next(
            step for step in self.workflow["jobs"]["hardened-images"]["steps"]
            if step.get("name") == "Record immutable digest and base references"
        )
        lines = recording["run"].splitlines()
        script = "\n".join(line for line in lines if "dockerfile=" in line or line.startswith("grep "))
        for selected, expected in (("", "FROM default@sha256:default\n"),
                                    ("custom with space.Dockerfile", "FROM selected@sha256:selected\n")):
            with self.subTest(selected=selected):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    (root / "nested").mkdir()
                    (root / "nested" / "Dockerfile").write_text("FROM default@sha256:default\n")
                    (root / "custom with space.Dockerfile").write_text("FROM selected@sha256:selected\n")
                    result = subprocess.run(
                        ["bash", "-e", "-c", script], cwd=root,
                        env=dict(os.environ, BUILD_CONTEXT="nested", DOCKERFILE=selected),
                        capture_output=True, text=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual((root / "base-images.txt").read_text(), expected)
        recording["env"].pop("DOCKERFILE")
        self.assertTrue(self.errors(self.workflow))

    def test_existing_entrypoint_is_the_compatible_default(self):
        self.workflow["on"]["workflow_call"]["inputs"]["hardened_entrypoint"]["default"] = '["./other"]'
        self.assertTrue(self.errors(self.workflow))

    def test_worker_smoke_must_select_the_tested_transport(self):
        for step in self.workflow["jobs"]["hardened-images"]["steps"]:
            if step.get("name") == "Smoke test with a read-only filesystem and no capabilities":
                step["run"] = step["run"].replace("-e QUEUE_SYSTEM=RABBITMQ", "")
        self.assertTrue(self.errors(self.workflow))

    def test_entrypoint_inspection_cannot_ignore_the_declared_contract(self):
        for step in self.workflow["jobs"]["hardened-images"]["steps"]:
            if step.get("name") == "Inspect image contract":
                step["env"].pop("EXPECTED_ENTRYPOINT")
        self.assertTrue(self.errors(self.workflow))

    def test_entrypoint_inspection_matches_the_image_not_just_valid_json(self):
        inspection = next(
            step["run"] for step in self.workflow["jobs"]["hardened-images"]["steps"]
            if step.get("name") == "Inspect image contract"
        )
        assertion = next(
            line for line in inspection.splitlines()
            if "{{json .Config.Entrypoint}}" in line
        )
        for actual, expected, status in (
            ('["/app/main"]', '["/app/main"]', 0),
            ('["./hub-pipeline-counting"]', '[ "./hub-pipeline-counting" ]', 0),
            ('["/app/main"]', '["./hub-pipeline-counting"]', 1),
            ('null', '["/app/main"]', 1),
        ):
            with self.subTest(actual=actual, expected=expected):
                result = subprocess.run(
                    ["bash", "-e", "-c",
                     'docker() { printf "%s\\n" "$ACTUAL_ENTRYPOINT"; }\n' + assertion],
                    env=dict(os.environ, ACTUAL_ENTRYPOINT=actual, EXPECTED_ENTRYPOINT=expected),
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, status, result.stdout + result.stderr)

    def test_command_and_workdir_inspection_match_actual_metadata(self):
        inspection = next(
            step["run"] for step in self.workflow["jobs"]["hardened-images"]["steps"]
            if step.get("name") == "Inspect image contract"
        )
        for field, variable, actual, expected, status in (
            ("Cmd", "EXPECTED_COMMAND", '["./main"]', '["./main"]', 0),
            ("Cmd", "EXPECTED_COMMAND", '["./other"]', '["./main"]', 1),
            ("WorkingDir", "EXPECTED_WORKDIR", "/home/vault", "/home/vault", 0),
            ("WorkingDir", "EXPECTED_WORKDIR", "/app", "/home/vault", 1),
        ):
            with self.subTest(field=field, actual=actual):
                assertion = next(
                    line for line in inspection.splitlines() if f".Config.{field}}}" in line
                )
                result = subprocess.run(
                    ["bash", "-e", "-c",
                     'docker() { printf "%s\\n" "$ACTUAL_METADATA"; }\n' + assertion],
                    env=dict(os.environ, ACTUAL_METADATA=actual, **{variable: expected}),
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, status, result.stdout + result.stderr)

    def test_frontend_smoke_test_cannot_be_missing_or_unconditional(self):
        for condition in (None, "always()"):
            with self.subTest(condition=condition):
                workflow = copy.deepcopy(self.workflow)
                steps = workflow["jobs"]["hardened-images"]["steps"]
                for step in steps:
                    if step.get("name") == "Smoke test frontend routing and runtime configuration":
                        if condition is None:
                            steps.remove(step)
                        else:
                            step["if"] = condition
                        break
                self.assertTrue(self.errors(workflow))

    def test_api_smoke_test_cannot_be_missing_or_unconditional(self):
        for condition in (None, "always()"):
            with self.subTest(condition=condition):
                workflow = copy.deepcopy(self.workflow)
                steps = workflow["jobs"]["hardened-images"]["steps"]
                for step in steps:
                    if step.get("name") == "Smoke test API HTTP and database readiness":
                        if condition is None:
                            steps.remove(step)
                        else:
                            step["if"] = condition
                        break
                self.assertTrue(self.errors(workflow))

    def test_api_dependencies_cannot_be_skipped(self):
        for name in ("mongodb", "rabbitmq"):
            with self.subTest(service=name):
                workflow = copy.deepcopy(self.workflow)
                workflow["jobs"]["hardened-images"]["services"][name]["image"] = ""
                self.assertTrue(self.errors(workflow))

    def test_module_secrets_are_required_in_every_native_build_surface(self):
        for name in ("Run native tests and vulnerability reachability checks",
                     "Build final image for inspection", "Publish staged image with provenance and SBOM"):
            with self.subTest(step=name):
                workflow = copy.deepcopy(self.workflow)
                for step in workflow["jobs"]["hardened-images"]["steps"]:
                    if step.get("name") == name:
                        if "run" in step:
                            step["env"] = {}
                        else:
                            del step["with"]["secrets"]
                self.assertTrue(self.errors(workflow))

    def test_native_builds_cannot_reintroduce_credential_build_arguments(self):
        for name in ("Build final image for inspection", "Publish staged image with provenance and SBOM"):
            with self.subTest(step=name):
                workflow = copy.deepcopy(self.workflow)
                for step in workflow["jobs"]["hardened-images"]["steps"]:
                    if step.get("name") == name:
                        step["with"]["build-args"] += "\ngithub_token=unsafe"
                self.assertTrue(self.errors(workflow))

    def test_pr_buildkit_mode_uses_secret_ids_not_token_arguments(self):
        for name in ("pr-build.yml", "pr-update-environment.yml"):
            workflow = load_yaml(WORKFLOWS_DIR / name)
            self.assertFalse(workflow["on"]["workflow_call"]["inputs"]["use_buildkit_secrets"]["default"])
            build = next(step for job in workflow["jobs"].values() for step in job.get("steps", [])
                         if step.get("name") == "Run Build")
            script = build["run"]
            replacements = {
                "${{ inputs.use_buildkit_secrets }}": "true",
                "${{ inputs.use_private_module_auth }}": "true",
                "${{ secrets.USERNAME }}": "ci",
                "${{ secrets.TOKEN }}": "private-build-token",
                "${{ env.PROJECT }}": "example",
                "${{ env.BUILD_CONTEXT }}": ".",
                "${{ matrix.architecture }}": "amd64",
            }
            for old, new in replacements.items():
                script = script.replace(old, new)
            for token in ("private-build-token", ""):
                with self.subTest(workflow=name, supplied=bool(token)):
                    with tempfile.TemporaryDirectory() as directory:
                        root = Path(directory)
                        docker = root / "docker"
                        docker.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$BUILD_ARGS"\n')
                        docker.chmod(0o755)
                        arguments = root / "arguments"
                        result = subprocess.run(
                            ["bash", "-e", "-c", script], cwd=root,
                            env=dict(os.environ, PATH=f"{root}:{os.environ['PATH']}",
                                     GH_TOKEN=token, BUILD_ARGS=str(arguments)),
                            capture_output=True, text=True,
                        )
                        if token:
                            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                            values = arguments.read_text()
                            self.assertIn("--secret\ngithub_token,env=GH_TOKEN", values.replace("id=", ""))
                            self.assertNotIn(token, values)
                            self.assertNotIn("github_username=", values)
                            self.assertNotIn("github_token=", values)
                        else:
                            self.assertNotEqual(result.returncode, 0)
                            self.assertIn("::error::TOKEN is required", result.stdout)
                            self.assertFalse(arguments.exists())

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
                    if step.get("name") == "Publish staged image with provenance and SBOM":
                        step["with"][key] = value
                self.assertTrue(self.errors(workflow))

    def test_read_only_callers_do_not_need_an_oidc_permission_grant(self):
        self.assertNotIn("permissions", self.workflow)
        self.assertNotIn("permissions", self.workflow["jobs"]["hardened-manifest"])

    def test_native_images_and_registry_scans_use_platform_package(self):
        for name, key in (
            ("Publish staged image with provenance and SBOM", "tags"),
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

    def test_native_version_tags_cannot_be_published_before_registry_gates(self):
        for step in self.workflow["jobs"]["hardened-images"]["steps"]:
            if step.get("id") == "publish":
                step["with"]["tags"] = (
                    "ghcr.io/${{ env.ORGANIZATION }}/${{ env.PROJECT }}-platform:"
                    "${{ matrix.architecture }}-${{ env.TAG }}"
                )
        self.assertTrue(self.errors(self.workflow))

    def test_manifest_verification_must_precede_promotion_and_finalization(self):
        steps = self.workflow["jobs"]["hardened-manifest"]["steps"]
        for name in (
            "Assemble and verify staged multi-platform manifest",
            "Promote verified image tags",
            "Finalize verified GitHub release",
        ):
            with self.subTest(name=name):
                workflow = copy.deepcopy(self.workflow)
                workflow["jobs"]["hardened-manifest"]["steps"] = [
                    step for step in steps if step.get("name") != name
                ]
                self.assertTrue(self.errors(workflow))
        promotion = next(step for step in steps if step.get("name") == "Promote verified image tags")
        steps.remove(promotion)
        steps.insert(0, promotion)
        self.assertTrue(self.errors(self.workflow))

    def test_unsigned_manifest_cannot_use_a_version_tag(self):
        for step in self.workflow["jobs"]["hardened-manifest"]["steps"]:
            if step.get("name") == "Assemble and verify staged multi-platform manifest":
                step["run"] = step["run"].replace(
                    'staging="$IMAGE:staging-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"',
                    'staging="$IMAGE:$TAG"',
                )
        self.assertTrue(self.errors(self.workflow))

    def test_release_finalization_cannot_precede_evidence_upload(self):
        steps = self.workflow["jobs"]["hardened-manifest"]["steps"]
        finalization = steps.pop()
        steps.insert(0, finalization)
        self.assertTrue(self.errors(self.workflow))

    def test_release_finalization_cannot_be_unconditional(self):
        self.workflow["jobs"]["hardened-manifest"]["steps"][-1]["if"] = "always()"
        self.assertTrue(self.errors(self.workflow))

    def test_bump_preserves_legacy_default(self):
        workflow = load_yaml(WORKFLOWS_DIR / "release-bump.yml")
        errors = []
        validate_release_bump_workflow(workflow, errors)
        self.assertEqual(errors, [])
        workflow["on"]["workflow_call"]["inputs"]["draft"]["default"] = True
        validate_release_bump_workflow(workflow, errors)
        self.assertTrue(errors)


class ReleaseExecutionTest(unittest.TestCase):
    COMMAND_STUB = """#!/bin/bash
set -euo pipefail
tool=${0##*/}
printf '%s' "$tool" >> "$EVENT_LOG"
printf ' %s' "$@" >> "$EVENT_LOG"
printf '\\n' >> "$EVENT_LOG"
case "$tool" in
  docker)
    if [ "$3" = create ]; then
      target=$5
      case "$target" in
        *:staging-*) point=stage ;;
        *-platform:*) point=native ;;
        *:latest) point=latest ;;
        *) point=version ;;
      esac
      if [ "$FAIL_AT" = "$point" ]; then exit 1; fi
      source=${@: -1}
      digest=${source##*@}
      if [ "$point" = stage ]; then digest=sha256:verified; fi
      if [ "$FAIL_AT" = mismatch ] && [ "$point" = version ]; then digest=sha256:wrong; fi
      printf '%s\\n' "$digest" > "$STATE_DIR/${target##*:}"
    elif [ "$5" = --raw ]; then
      printf '{"schemaVersion":2}\\n'
    else
      if [ "$FAIL_AT" = stage-inspect ] && [[ "$4" == *:staging-* ]]; then exit 1; fi
      cat "$STATE_DIR/${4##*:}"
    fi
    ;;
  cosign)
    if [ "$FAIL_AT" = "$1" ]; then exit 1; fi
    printf '[]\\n'
    ;;
  gh)
    if [ "$2" = view ]; then
      if [ "$FAIL_AT" = release-view ]; then exit 1; fi
      printf '%s\\n' "$DRAFT_STATE"
    elif [ "$2" = edit ] && [ "$FAIL_AT" = release-edit ]; then
      exit 1
    fi
    ;;
  git)
    if [ "$FAIL_AT" = "$1" ]; then exit 1; fi
    ;;
esac
"""

    def setUp(self):
        workflow = load_yaml(WORKFLOWS_DIR / "release-create.yml")
        self.steps = workflow["jobs"]["hardened-manifest"]["steps"]
        self.registry_gate = next(
            step["run"] for step in workflow["jobs"]["hardened-images"]["steps"]
            if step.get("name") == "Gate immutable published image"
        )
        self.image_gate = next(
            step["run"] for step in workflow["jobs"]["hardened-images"]["steps"]
            if step.get("name") == "Enforce vulnerability and secret policy"
        )
        bump = load_yaml(WORKFLOWS_DIR / "release-bump.yml")
        self.bump_script = bump["jobs"]["bump-release"]["steps"][-1]["run"]

    def execute(self, scripts, fail_at="", draft_state="true", draft="true", report=None,
                profile="worker", browser_report=None, browser_inventory=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for command in ("docker", "cosign", "gh", "git"):
                executable = root / command
                executable.write_text(self.COMMAND_STUB)
                executable.chmod(0o755)
            for arch in ("amd64", "arm64"):
                (root / f"digest-{arch}.txt").write_text(f"sha256:{arch}\n")
            if report is not None:
                (root / "published-image-security.json").write_text(json.dumps(report))
                (root / "image-security.json").write_text(json.dumps(report))
            if browser_report is not None:
                (root / "browser-security.json").write_text(json.dumps(browser_report))
            env = dict(
                os.environ,
                PATH=f"{root}{os.pathsep}{os.environ['PATH']}",
                EVENT_LOG=str(root / "events"),
                STATE_DIR=str(root),
                FAIL_AT=fail_at,
                DRAFT_STATE=draft_state,
                DRAFT=draft,
                PROFILE=profile,
                BROWSER_INVENTORY="true" if profile == "frontend" or browser_inventory else "false",
                IMAGE="ghcr.io/uug-ai/example",
                PLATFORM_IMAGE="ghcr.io/uug-ai/example-platform",
                TAG="v1.2.3",
                GITHUB_SHA="source-commit",
                GITHUB_REPOSITORY="uug-ai/example",
                GITHUB_RUN_ID="42",
                GITHUB_RUN_ATTEMPT="2",
                GITHUB_STEP_SUMMARY=str(root / "summary"),
            )
            result = subprocess.run(
                ["bash", "-e", "-c", "\n".join(scripts)],
                cwd=root, env=env, capture_output=True, text=True,
            )
            event_log = root / "events"
            events = event_log.read_text().splitlines() if event_log.exists() else []
            return result, events, (root / "verified-digest.txt").exists()

    def manifest_scripts(self):
        return [step["run"] for step in self.steps if "run" in step]

    def test_final_tags_and_github_publication_follow_verification(self):
        result, events, verified = self.execute(self.manifest_scripts())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(verified)
        verify_index = next(i for i, event in enumerate(events) if event.startswith("cosign verify "))
        staging = [event for event in events[:verify_index] if event.startswith("docker buildx imagetools create")]
        self.assertEqual(len(staging), 1)
        self.assertIn("example:staging-42-2 ", staging[0])
        for target in (
            "example-platform:amd64-v1.2.3",
            "example-platform:arm64-v1.2.3",
            "example:v1.2.3",
            "example:latest",
        ):
            index = next(i for i, event in enumerate(events) if event.startswith(
                f"docker buildx imagetools create -t ghcr.io/uug-ai/{target} "
            ))
            self.assertGreater(index, verify_index)
        self.assertEqual(events[-1], "gh release edit v1.2.3 --repo uug-ai/example --draft=false --verify-tag")

    def test_staging_or_signing_failure_does_not_expose_final_tags(self):
        for point in ("stage", "stage-inspect", "sign", "verify"):
            with self.subTest(point=point):
                result, events, verified = self.execute(self.manifest_scripts(), fail_at=point)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(verified)
                self.assertFalse(any("example:v1.2.3" in event or "example:latest" in event for event in events))
                self.assertFalse(any(event.startswith("gh release ") for event in events))

    def test_registry_policy_failure_prevents_all_finalization(self):
        for kind in ("Vulnerabilities", "Secrets"):
            for severity in ("MEDIUM", "HIGH", "CRITICAL"):
                with self.subTest(kind=kind, severity=severity):
                    report = {"Results": [{kind: [{"Severity": severity}]}]}
                    result, events, verified = self.execute(
                        [self.registry_gate, *self.manifest_scripts()], report=report,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(events, [])
                    self.assertFalse(verified)

    def test_browser_findings_block_even_when_os_image_scan_is_clean(self):
        for severity in ("MEDIUM", "HIGH", "CRITICAL"):
            with self.subTest(severity=severity):
                result, events, _ = self.execute(
                    [self.image_gate, *self.manifest_scripts()],
                    profile="frontend", report={"Results": []},
                    browser_report={"Results": [{"Vulnerabilities": [{"Severity": severity}]}]},
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(events, [])

    def test_frontend_cannot_pass_with_a_missing_browser_scan(self):
        result, events, _ = self.execute(
            [self.image_gate, *self.manifest_scripts()],
            profile="frontend", report={"Results": []},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(events, [])

    def test_worker_policy_does_not_require_frontend_inventory(self):
        result, _, _ = self.execute([self.image_gate], report={"Results": []})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_nonblocking_inventory_findings_preserve_existing_policy(self):
        for severity in ("UNKNOWN", "LOW"):
            with self.subTest(severity=severity):
                report = {"Results": [{"Vulnerabilities": [{"Severity": severity}]}]}
                result, events, verified = self.execute(
                    [self.registry_gate, *self.manifest_scripts()], report=report,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue(verified)
                self.assertTrue(events[-1].startswith("gh release edit "))

    def test_promotion_failure_or_digest_mismatch_keeps_release_draft(self):
        for point in ("native", "version", "latest", "mismatch"):
            with self.subTest(point=point):
                result, events, verified = self.execute(self.manifest_scripts(), fail_at=point)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(verified)
                self.assertFalse(any(event.startswith("gh release ") for event in events))
                if point == "mismatch":
                    self.assertIn("::error::Promoted digest", result.stdout)

    def test_github_errors_are_not_reported_as_success(self):
        for point in ("release-view", "release-edit"):
            with self.subTest(point=point):
                result, _, _ = self.execute(self.manifest_scripts(), fail_at=point)
                self.assertNotEqual(result.returncode, 0)

    def test_already_published_release_is_not_modified(self):
        result, events, _ = self.execute(self.manifest_scripts(), draft_state="false")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(any(event.startswith("gh release edit ") for event in events))
        self.assertIn("already published", result.stdout)

    def test_invalid_release_state_is_an_explicit_error(self):
        result, events, _ = self.execute(self.manifest_scripts(), draft_state="null")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("::error::Unexpected draft state", result.stdout)
        self.assertFalse(any(event.startswith("gh release edit ") for event in events))

    def test_draft_bump_reserves_source_tag_before_creating_draft(self):
        result, events, _ = self.execute([self.bump_script])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(events[:2], [
            "git tag v1.2.3 source-commit",
            "git push origin refs/tags/v1.2.3",
        ])
        self.assertIn("--draft --verify-tag", events[2])

    def test_source_tag_failure_prevents_draft_creation(self):
        for point in ("tag", "push"):
            with self.subTest(point=point):
                result, events, _ = self.execute([self.bump_script], fail_at=point)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(event.startswith("gh release create ") for event in events))

    def test_legacy_bump_does_not_reserve_or_draft(self):
        result, events, _ = self.execute([self.bump_script], draft="false")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].startswith("gh release create v1.2.3 "))
        self.assertNotIn("--draft", events[0])
        self.assertNotIn("--verify-tag", events[0])

    def test_hardened_configuration_requires_only_profile_dependencies(self):
        workflow = load_yaml(WORKFLOWS_DIR / "release-create.yml")
        script = next(
            step["run"] for step in workflow["jobs"]["hardened-source"]["steps"]
            if step.get("name") == "Validate hardened release configuration"
        )
        for profile, queue, auth, expected in (
            ("worker", "queue", "false", 0),
            ("worker", "", "false", 1),
            ("frontend", "", "false", 0),
            ("api", "", "true", 0),
            ("standalone", "", "false", 0),
            ("mongo-task", "", "false", 0),
            ("worker", "queue", "true", 0),
            ("frontend", "", "true", 1),
            ("unknown", "queue", "false", 1),
        ):
            with self.subTest(profile=profile, queue=queue):
                result = subprocess.run(
                    ["bash", "-e", "-c", script],
                    env=dict(os.environ, PROFILE=profile, SMOKE_QUEUE=queue,
                             PRIVATE_MODULE_AUTH=auth, PUBLISH="false", RELEASE_TAG="",
                             EXPECTED_ENTRYPOINT='["/app/main"]', MODULE_TOKEN="fixture-token",
                             ADDITIONAL_SMOKE="false"),
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)

    def test_additional_worker_smoke_cannot_be_silently_ignored(self):
        workflow = load_yaml(WORKFLOWS_DIR / "release-create.yml")
        script = next(
            step["run"] for step in workflow["jobs"]["hardened-source"]["steps"]
            if step.get("name") == "Validate hardened release configuration"
        )
        for profile, expected in (("worker", 0), ("frontend", 1),
                                  ("api", 1), ("standalone", 1), ("mongo-task", 1)):
            with self.subTest(profile=profile):
                result = subprocess.run(
                    ["bash", "-e", "-c", script],
                    env=dict(os.environ, PROFILE=profile, SMOKE_QUEUE="queue",
                             PRIVATE_MODULE_AUTH="false", PUBLISH="false", RELEASE_TAG="",
                             EXPECTED_ENTRYPOINT='["/app/main"]', MODULE_TOKEN="",
                             ADDITIONAL_SMOKE="true"),
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                if expected:
                    self.assertIn("worker_additional_smoke", result.stdout)

    def test_cmd_only_images_preserve_both_parts_of_the_contract(self):
        workflow = load_yaml(WORKFLOWS_DIR / "release-create.yml")
        script = next(
            step["run"] for step in workflow["jobs"]["hardened-source"]["steps"]
            if step.get("name") == "Validate hardened release configuration"
        )
        for entrypoint, command, expected in (
            ("null", '["./main"]', 0), ("[]", '["./main"]', 0),
            ("null", "", 1), ("null", "[]", 1), ("[]", "[]", 1),
            ("null", "null", 1), ("null", '[""]', 1),
            ("null", " ", 1), ("null", '["./main"] ["./other"]', 1),
            ('["/app/main"]', "[]", 0),
        ):
            with self.subTest(entrypoint=entrypoint, command=command):
                result = subprocess.run(
                    ["bash", "-e", "-c", script],
                    env=dict(os.environ, PROFILE="standalone", SMOKE_QUEUE="",
                             PRIVATE_MODULE_AUTH="false", PUBLISH="false", RELEASE_TAG="",
                             EXPECTED_ENTRYPOINT=entrypoint, EXPECTED_COMMAND=command,
                             MODULE_TOKEN="", ADDITIONAL_SMOKE="false"),
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                if expected:
                    self.assertIn("::error::hardened_", result.stdout)

    def test_hardened_entrypoint_configuration_rejects_invalid_shapes(self):
        workflow = load_yaml(WORKFLOWS_DIR / "release-create.yml")
        script = next(
            step["run"] for step in workflow["jobs"]["hardened-source"]["steps"]
            if step.get("name") == "Validate hardened release configuration"
        )
        for entrypoint, expected in (
            ('["/app/main"]', 0),
            ('[ "./hub-pipeline-counting" ]', 0),
            ('[]', 1), ('null', 1), ('{}', 1), ('"main"', 1),
            ('[""]', 1), ('[false]', 1), ('not-json', 1),
            ('', 1), (' ', 1), ('["/app/main"] ["./other"]', 1),
        ):
            with self.subTest(entrypoint=entrypoint):
                result = subprocess.run(
                    ["bash", "-e", "-c", script],
                    env=dict(os.environ, PROFILE="worker", SMOKE_QUEUE="queue",
                             PRIVATE_MODULE_AUTH="false", PUBLISH="false", RELEASE_TAG="",
                             EXPECTED_ENTRYPOINT=entrypoint, MODULE_TOKEN=""),
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                if expected:
                    self.assertIn("::error::hardened_entrypoint", result.stdout)

    def test_non_frontend_browser_inventory_is_required_and_gated(self):
        for browser_report, expected in (
            (None, 1), ({"Results": []}, 0),
            ({"Results": [{"Vulnerabilities": [{"Severity": "MEDIUM"}]}]}, 1),
        ):
            with self.subTest(browser_report=browser_report):
                result, _, _ = self.execute(
                    [self.image_gate], report={"Results": []}, profile="mongo-task",
                    browser_report=browser_report, browser_inventory=True,
                )
                self.assertEqual(result.returncode == 0, expected == 0,
                                 result.stdout + result.stderr)

    def test_private_module_authentication_requires_a_token(self):
        workflow = load_yaml(WORKFLOWS_DIR / "release-create.yml")
        script = next(
            step["run"] for step in workflow["jobs"]["hardened-source"]["steps"]
            if step.get("name") == "Validate hardened release configuration"
        )
        for profile in ("worker", "api"):
            with self.subTest(profile=profile):
                result = subprocess.run(
                    ["bash", "-e", "-c", script],
                    env=dict(os.environ, PROFILE=profile, SMOKE_QUEUE="queue",
                             PRIVATE_MODULE_AUTH="true", PUBLISH="false", RELEASE_TAG="",
                             EXPECTED_ENTRYPOINT='["/app/main"]', MODULE_TOKEN=""),
                    capture_output=True, text=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("requires secrets.TOKEN", result.stdout)


if __name__ == "__main__":
    unittest.main()
