from pathlib import Path
import re
import sys

import yaml


ROOT = Path(__file__).resolve().parent
WORKFLOWS_DIR = ROOT / ".github" / "workflows"
PR_DESCRIPTION_ACTION_DIR = ROOT / ".github" / "actions" / "pr-description"
README = ROOT / "README.md"
FORBIDDEN_SECRETS = {"OPENAI_API_KEY"}
PR_DESCRIPTION_ACTION_REFERENCE = (
    "uug-ai/workflows/.github/actions/pr-description@main"
)
BUILD_MODULE_SECRET = (
    "${{ inputs.use_private_module_auth && secrets.TOKEN != '' && "
    "format('github_token={0}', secrets.TOKEN) || '' }}"
)
EXPECTED_WORKFLOWS = {
    "issue-userstory-create.yml",
    "pr-build.yml",
    "pr-delete-environment.yml",
    "pr-description.yml",
    "pr-update-environment.yml",
    "release-bump.yml",
    "release-create.yml",
    "security-scan.yml",
    "test-coverage.yaml",
}


class ActionLoader(yaml.SafeLoader):
    pass


def construct_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        if isinstance(key_node, yaml.ScalarNode) and key_node.value == "on":
            key = "on"
        else:
            key = loader.construct_object(key_node, deep=deep)
        value = loader.construct_object(value_node, deep=deep)
        mapping[key] = value
    return mapping


ActionLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    construct_mapping,
)


def fail(message: str) -> None:
    print(f"ERROR: {message}")


def load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.load(handle, Loader=ActionLoader)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} does not contain a top-level mapping")
    return data


def validate_workflow(path: Path, errors: list[str]) -> None:
    data = load_yaml(path)
    on_section = data.get("on")
    if not isinstance(on_section, dict) or "workflow_call" not in on_section:
        errors.append(f"{path.name}: missing on.workflow_call")
        return

    workflow_call = on_section["workflow_call"]
    if not isinstance(workflow_call, dict):
        errors.append(f"{path.name}: on.workflow_call must be a mapping")
        return

    secrets = workflow_call.get("secrets", {})
    if not isinstance(secrets, dict):
        errors.append(f"{path.name}: workflow_call.secrets must be a mapping")
        return

    forbidden = FORBIDDEN_SECRETS.intersection(secrets.keys())
    if forbidden:
        names = ", ".join(sorted(forbidden))
        errors.append(f"{path.name}: forbidden secrets declared: {names}")


def validate_readme(errors: list[str]) -> None:
    readme_text = README.read_text(encoding="utf-8")
    for secret_name in sorted(FORBIDDEN_SECRETS):
        pattern = rf"(?<![A-Z0-9_]){re.escape(secret_name)}(?![A-Z0-9_])"
        if re.search(pattern, readme_text):
            errors.append(f"README.md: forbidden secret still documented: {secret_name}")


def validate_pr_description_action(errors: list[str]) -> None:
    action_path = PR_DESCRIPTION_ACTION_DIR / "action.yml"
    generator_path = PR_DESCRIPTION_ACTION_DIR / "generate_pr_description.py"
    if not action_path.is_file():
        errors.append("Missing PR description action.yml")
        return
    if not generator_path.is_file():
        errors.append("Missing PR description generator")

    action = load_yaml(action_path)
    runs = action.get("runs")
    if not isinstance(runs, dict) or runs.get("using") != "composite":
        errors.append("PR description action must be a composite action")

    expected_inputs = {
        "github_token",
        "pull_request_number",
        "azure_openai_api_key",
        "azure_openai_deployment",
        "azure_openai_endpoint",
        "azure_openai_version",
    }
    inputs = action.get("inputs")
    if not isinstance(inputs, dict) or not expected_inputs.issubset(inputs):
        errors.append("PR description action is missing required inputs")

    workflow_path = WORKFLOWS_DIR / "pr-description.yml"
    workflow_text = workflow_path.read_text(encoding="utf-8")
    if PR_DESCRIPTION_ACTION_REFERENCE not in workflow_text:
        errors.append("PR description workflow must use the first-party action")


def validate_release_workflow(data: dict, errors: list[str]) -> None:
    inputs = data["on"]["workflow_call"]["inputs"]
    if inputs["hardened"]["default"] is not False:
        errors.append("release-create.yml: hardening must remain opt-in")
    if inputs["publish"]["default"] is not True:
        errors.append("release-create.yml: existing callers must still publish")
    if inputs["finalize_release"]["default"] is not False:
        errors.append("release-create.yml: draft finalization must remain opt-in")
    if inputs["hardened_profile"]["default"] != "worker":
        errors.append("release-create.yml: existing hardened callers must retain the worker profile")
    if inputs.get("hardened_entrypoint", {}).get("default") != '["/app/main"]':
        errors.append("release-create.yml: existing hardened callers must retain their entrypoint")
    if inputs.get("hardened_command", {}).get("default") != "":
        errors.append("release-create.yml: existing callers must not acquire a new command assertion")
    if inputs.get("hardened_workdir", {}).get("default") != "":
        errors.append("release-create.yml: existing callers must not acquire a new workdir assertion")
    if inputs.get("browser_inventory", {}).get("default") is not False:
        errors.append("release-create.yml: non-frontend browser inventory must remain opt-in")
    if inputs.get("hardened_dockerfile", {}).get("default") != "":
        errors.append("release-create.yml: existing callers must retain their context Dockerfile")
    if inputs.get("worker_additional_smoke", {}).get("default") is not False:
        errors.append("release-create.yml: additional worker smoke must remain opt-in")
    if "id-token" in data.get("permissions", {}):
        errors.append("release-create.yml: legacy callers must not require OIDC")

    jobs = data["jobs"]
    for job_id in ("build", "create-manifest"):
        if jobs[job_id]["if"] != "${{ !inputs.hardened && inputs.publish }}":
            errors.append(f"release-create.yml: {job_id} must isolate legacy builds")
    if jobs["hardened-source"]["if"] != "inputs.hardened":
        errors.append("release-create.yml: source gates must be opt-in")
    images = jobs["hardened-images"]
    if images["needs"] != "hardened-source":
        errors.append("release-create.yml: images must require source gates")
    if images["env"].get("TRIVY_PLATFORM") != "linux/${{ matrix.architecture }}":
        errors.append("release-create.yml: registry scans must use the native platform")
    steps = images["steps"]
    browser_condition = "inputs.hardened_profile == 'frontend' || inputs.browser_inventory"
    inspection = next(
        (step for step in steps if step.get("name") == "Inspect image contract"),
        {},
    )
    if (
        inspection.get("env", {}).get("EXPECTED_ENTRYPOINT") != "${{ inputs.hardened_entrypoint }}"
        or inspection.get("env", {}).get("EXPECTED_COMMAND") != "${{ inputs.hardened_command }}"
        or inspection.get("env", {}).get("EXPECTED_WORKDIR") != "${{ inputs.hardened_workdir }}"
        or inspection.get("env", {}).get("BROWSER_INVENTORY") != "${{ " + browser_condition + " }}"
        or '$(jq -c . <<< "$EXPECTED_ENTRYPOINT")' not in inspection.get("run", "")
        or '$(jq -c . <<< "$EXPECTED_COMMAND")' not in inspection.get("run", "")
    ):
        errors.append("release-create.yml: inspection must enforce the declared image entrypoint")
    worker_smoke = next(
        (step for step in steps if step.get("name") == "Smoke test with a read-only filesystem and no capabilities"),
        {},
    )
    frontend_smoke = next(
        (step for step in steps if step.get("name") == "Smoke test frontend routing and runtime configuration"),
        {},
    )
    api_smoke = next(
        (step for step in steps if step.get("name") == "Smoke test API HTTP and database readiness"),
        {},
    )
    repository_smoke = next(
        (step for step in steps if step.get("name") == "Smoke test repository runtime contract"),
        {},
    )
    if (
        repository_smoke.get("if") != (
            "inputs.hardened_profile == 'standalone' || inputs.hardened_profile == 'mongo-task' || "
            "(inputs.hardened_profile == 'worker' && inputs.worker_additional_smoke)"
        )
        or repository_smoke.get("run") != "bash image/smoke-test.sh hardened:scan"
    ):
        errors.append("release-create.yml: standalone/native runtime contracts require repository image smoke")
    if worker_smoke.get("if") != "inputs.hardened_profile == 'worker'":
        errors.append("release-create.yml: worker smoke checks must be profile-specific")
    if "-e QUEUE_SYSTEM=RABBITMQ" not in worker_smoke.get("run", ""):
        errors.append("release-create.yml: worker smoke checks must select RabbitMQ explicitly")
    if frontend_smoke.get("if") != "inputs.hardened_profile == 'frontend'":
        errors.append("release-create.yml: frontend routing/configuration smoke checks are required")
    if frontend_smoke.get("run") != "bash image/smoke-test.sh hardened:scan":
        errors.append("release-create.yml: frontend checks must exercise the built image")
    if (
        api_smoke.get("if") != "inputs.hardened_profile == 'api'"
        or api_smoke.get("run") != "bash image/smoke-test.sh hardened:scan"
        or api_smoke.get("env", {}).get("RABBITMQ_CONTAINER") != "${{ job.services.rabbitmq.id }}"
    ):
        errors.append("release-create.yml: API HTTP/database/queue checks must exercise the built image")
    for service, image in (("mongodb", "mongo:8"), ("rabbitmq", "rabbitmq:4-alpine")):
        profiles = "inputs.hardened_profile == 'worker' || inputs.hardened_profile == 'api'"
        if service == "mongodb":
            profiles += " || inputs.hardened_profile == 'mongo-task'"
        expected = (
            "${{ (" + profiles + ") "
            f"&& '{image}' || '' }}}}"
        )
        if images["services"][service]["image"] != expected:
            errors.append(f"release-create.yml: {service} must match the profile dependencies")
    native_tests = next(
        (step for step in steps if step.get("name") == "Run native tests and vulnerability reachability checks"),
        {},
    )
    if (
        native_tests.get("env", {}).get("GH_TOKEN") != "${{ inputs.use_private_module_auth && secrets.TOKEN || '' }}"
        or '--secret "id=github_token,env=GH_TOKEN"' not in native_tests.get("run", "")
    ):
        errors.append("release-create.yml: native tests require scoped BuildKit module secrets")
    if (
        native_tests.get("env", {}).get("DOCKERFILE") != "${{ inputs.hardened_dockerfile }}"
        or 'args+=(--file "$DOCKERFILE")' not in native_tests.get("run", "")
    ):
        errors.append("release-create.yml: native tests must use the selected Dockerfile")
    browser_scan = next(
        (step for step in steps if step.get("name") == "Scan browser dependency inventory"),
        {},
    )
    if (
        browser_scan.get("if") != browser_condition
        or browser_scan.get("with", {}).get("scan-ref") != "frontend-inventory"
        or browser_scan.get("with", {}).get("scan-type") != "fs"
    ):
        errors.append("release-create.yml: browser lock inventory extracted from the image must be scanned")
    browser_sbom = next(
        (step for step in steps if step.get("name") == "Generate browser dependency SPDX SBOM"),
        {},
    )
    image_policy = next(
        (step for step in steps if step.get("name") == "Enforce vulnerability and secret policy"),
        {},
    )
    if (
        browser_sbom.get("if") != browser_condition
        or image_policy.get("env", {}).get("BROWSER_INVENTORY") != "${{ " + browser_condition + " }}"
    ):
        errors.append("release-create.yml: all browser inventory profiles require SBOM and policy gates")
    names = [step.get("name") for step in steps]
    required_order = [
        "Run native tests and vulnerability reachability checks",
        "Inspect image contract",
        "Smoke test with a read-only filesystem and no capabilities",
        "Smoke test frontend routing and runtime configuration",
        "Smoke test API HTTP and database readiness",
        "Smoke test repository runtime contract",
        "Scan browser dependency inventory",
        "Enforce vulnerability and secret policy",
        "Publish staged image with provenance and SBOM",
        "Scan immutable published image",
        "Gate immutable published image",
        "Record immutable digest and base references",
    ]
    if any(name not in names for name in required_order):
        errors.append("release-create.yml: missing required hardened image gate")
    elif [names.index(name) for name in required_order] != sorted(
        names.index(name) for name in required_order
    ):
        errors.append("release-create.yml: hardened gates must precede promotion")
    for step in steps:
        name = step.get("name")
        if name in ("Build final image for inspection", "Publish staged image with provenance and SBOM"):
            options = step.get("with", {})
            if options.get("secrets") != BUILD_MODULE_SECRET:
                errors.append(f"release-create.yml: {name} must supply optional private modules via BuildKit secrets")
            if options.get("file") != "${{ inputs.hardened_dockerfile }}":
                errors.append(f"release-create.yml: {name} must use the tested Dockerfile")
            if re.search(r"github_(?:username|token)", options.get("build-args", "")):
                errors.append(f"release-create.yml: {name} must not use credential build arguments")
        if name == "Publish staged image with provenance and SBOM":
            if step["with"].get("provenance") != "mode=max" or step["with"].get("sbom") is not True:
                errors.append("release-create.yml: publishing requires provenance and SBOM")
            if step["with"].get("tags") != (
                "ghcr.io/${{ env.ORGANIZATION }}/${{ env.PROJECT }}-platform:"
                "staging-${{ github.run_id }}-${{ github.run_attempt }}-${{ matrix.architecture }}"
            ):
                errors.append("release-create.yml: native images must use run-specific staging tags in the -platform package")
        if name == "Scan immutable published image" and step["with"].get("image-ref") != (
            "ghcr.io/${{ env.ORGANIZATION }}/${{ env.PROJECT }}-platform@"
            "${{ steps.publish.outputs.digest }}"
        ):
            errors.append("release-create.yml: immutable scans must inspect the -platform package")
        if name in required_order[required_order.index("Publish staged image with provenance and SBOM"):] and step.get("if") != "inputs.publish":
            errors.append(f"release-create.yml: {name} must respect validation-only mode")

    manifest = jobs["hardened-manifest"]
    if manifest["needs"] != "hardened-images" or manifest["if"] != "inputs.hardened && inputs.publish":
        errors.append("release-create.yml: signing must require successful image gates")
    if "id-token" in manifest.get("permissions", {}):
        errors.append("release-create.yml: signing must inherit the caller's optional OIDC grant")
    manifest_steps = manifest["steps"]
    manifest_names = [step.get("name") for step in manifest_steps]
    required_manifest_order = [
        "Assemble and verify staged multi-platform manifest",
        "Promote verified image tags",
        "Finalize verified GitHub release",
    ]
    if any(name not in manifest_names for name in required_manifest_order):
        errors.append("release-create.yml: missing staged verification, promotion or release finalization")
    elif [manifest_names.index(name) for name in required_manifest_order] != sorted(
        manifest_names.index(name) for name in required_manifest_order
    ):
        errors.append("release-create.yml: final tags and releases must follow signature verification")
    signing = next(
        (step for step in manifest_steps if step.get("name") == required_manifest_order[0]),
        {},
    )
    if signing.get("env", {}).get("PLATFORM_IMAGE") != "ghcr.io/${{ env.ORGANIZATION }}/${{ env.PROJECT }}-platform":
        errors.append("release-create.yml: manifests must reference the native package")
    signing_script = signing.get("run", "")
    if 'staging="$IMAGE:staging-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"' not in signing_script:
        errors.append("release-create.yml: unsigned manifests must use a run-specific staging tag")
    if '"$IMAGE:$TAG"' in signing_script or '"$IMAGE:latest"' in signing_script:
        errors.append("release-create.yml: signing must not expose final image tags")
    finalization = next(
        (step for step in manifest_steps if step.get("name") == required_manifest_order[2]),
        {},
    )
    if finalization.get("if") != "inputs.finalize_release":
        errors.append("release-create.yml: GitHub release finalization must remain opt-in")
    evidence_indexes = [
        index for index, step in enumerate(manifest_steps)
        if step.get("uses", "").startswith("actions/upload-artifact@")
    ]
    if (
        not evidence_indexes
        or required_manifest_order[2] not in manifest_names
        or max(evidence_indexes) >= manifest_names.index(required_manifest_order[2])
    ):
        errors.append("release-create.yml: release finalization must follow evidence upload")
    promotion = jobs["update-gitops"]
    if promotion["needs"] != ["create-manifest", "hardened-manifest"]:
        errors.append("release-create.yml: GitOps must wait for either manifest path")
    if promotion["if"] != (
        "${{ always() && !cancelled() && inputs.create_gitops_pr && inputs.publish && "
        "(needs.create-manifest.result == 'success' || needs.hardened-manifest.result == 'success') }}"
    ):
        errors.append("release-create.yml: GitOps must require successful publication")


def validate_release_bump_workflow(data: dict, errors: list[str]) -> None:
    inputs = data["on"]["workflow_call"]["inputs"]
    if inputs["draft"]["default"] is not False:
        errors.append("release-bump.yml: draft creation must remain opt-in")


def validate_buildkit_auth_workflow(data: dict, errors: list[str], name: str) -> None:
    if data["on"]["workflow_call"]["inputs"]["use_buildkit_secrets"]["default"] is not False:
        errors.append(f"{name}: BuildKit credential mode must remain opt-in")
    steps = [step for job in data["jobs"].values() for step in job.get("steps", [])]
    build = next((step for step in steps if step.get("name") == "Run Build"), {})
    if (
        '--secret "id=github_token,env=GH_TOKEN"' not in build.get("run", "")
        or build.get("env", {}).get("GH_TOKEN") != "${{ secrets.TOKEN }}"
    ):
        errors.append(f"{name}: BuildKit credential mode must use the TOKEN environment secret")


def main() -> int:
    errors: list[str] = []
    workflow_names = {path.name for path in WORKFLOWS_DIR.glob("*.yml")}
    workflow_names.update(path.name for path in WORKFLOWS_DIR.glob("*.yaml"))

    missing = EXPECTED_WORKFLOWS.difference(workflow_names)
    if missing:
        errors.append(f"Missing expected workflow files: {', '.join(sorted(missing))}")

    for workflow_name in sorted(EXPECTED_WORKFLOWS.intersection(workflow_names)):
        validate_workflow(WORKFLOWS_DIR / workflow_name, errors)

    validate_readme(errors)
    validate_pr_description_action(errors)
    validate_release_workflow(load_yaml(WORKFLOWS_DIR / "release-create.yml"), errors)
    validate_release_bump_workflow(load_yaml(WORKFLOWS_DIR / "release-bump.yml"), errors)
    for name in ("pr-build.yml", "pr-update-environment.yml"):
        validate_buildkit_auth_workflow(load_yaml(WORKFLOWS_DIR / name), errors, name)

    if errors:
        for message in errors:
            fail(message)
        return 1

    print("Reusable workflow contract checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())