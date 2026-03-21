from pathlib import Path
import re
import sys

import yaml


ROOT = Path(__file__).resolve().parent
WORKFLOWS_DIR = ROOT / ".github" / "workflows"
README = ROOT / "README.md"
FORBIDDEN_SECRETS = {"OPENAI_API_KEY", "OPENAI_MODEL"}
EXPECTED_WORKFLOWS = {
    "issue-userstory-create.yml",
    "pr-build.yml",
    "pr-delete-environment.yml",
    "pr-description.yml",
    "pr-update-environment.yml",
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

    if errors:
        for message in errors:
            fail(message)
        return 1

    print("Reusable workflow contract checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())