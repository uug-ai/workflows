#!/usr/bin/env python3

import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


MAX_CHANGED_FILES = 300
MAX_PROMPT_CHARACTERS = 30_000
MAX_COMPLETION_TOKENS = 1_000
SYSTEM_PROMPT = """You write concise pull request descriptions for software engineers.
Return only the Markdown body without a title or a 'Description' heading.
Explain the motivation, important behavior changes, and implementation details
supported by the title and diff. Mention tests only when the diff provides evidence.
Do not invent context, behavior, or validation that is not present in the input."""


class RequestError(RuntimeError):
    def __init__(self, method: str, url: str, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"{method} {url} failed with HTTP {status}: {body}")


@dataclass(frozen=True)
class Config:
    github_api_url: str
    github_repository: str
    github_token: str
    pull_request_number: int
    pull_request_url: str
    overwrite_description: bool
    azure_openai_api_key: str
    azure_openai_endpoint: str
    azure_openai_version: str
    azure_openai_deployment: str

    @classmethod
    def from_environment(cls) -> "Config":
        try:
            pull_request_number = int(required_environment("INPUT_PULL_REQUEST_NUMBER"))
        except ValueError as error:
            raise ValueError("INPUT_PULL_REQUEST_NUMBER must be an integer") from error

        if pull_request_number < 1:
            raise ValueError("INPUT_PULL_REQUEST_NUMBER must be positive")

        return cls(
            github_api_url=required_environment("GITHUB_API_URL").rstrip("/"),
            github_repository=required_environment("GITHUB_REPOSITORY"),
            github_token=required_environment("INPUT_GITHUB_TOKEN"),
            pull_request_number=pull_request_number,
            pull_request_url=os.environ.get("INPUT_PULL_REQUEST_URL", "").strip(),
            overwrite_description=parse_boolean(
                os.environ.get("INPUT_OVERWRITE_DESCRIPTION", "true")
            ),
            azure_openai_api_key=required_environment("INPUT_AZURE_OPENAI_API_KEY"),
            azure_openai_endpoint=required_environment(
                "INPUT_AZURE_OPENAI_ENDPOINT"
            ).rstrip("/"),
            azure_openai_version=required_environment("INPUT_AZURE_OPENAI_VERSION"),
            azure_openai_deployment=required_environment(
                "INPUT_AZURE_OPENAI_DEPLOYMENT"
            ),
        )


JsonRequest = Callable[..., Any]


def required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def parse_boolean(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"invalid boolean value: {value}")


def request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: Any = None,
) -> Any:
    request_headers = {"User-Agent": "uug-ai-pr-description"}
    request_headers.update(headers or {})
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"

    request = Request(url, data=data, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=60) as response:
            body = response.read().decode("utf-8")
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RequestError(method, url, error.code, body) from error
    except URLError as error:
        raise RuntimeError(f"{method} {url} failed: {error.reason}") from error

    return json.loads(body) if body else None


def github_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def github_url(config: Config, path: str) -> str:
    repository = quote(config.github_repository, safe="/")
    return f"{config.github_api_url}/repos/{repository}{path}"


def get_pull_request(config: Config, request: JsonRequest) -> dict[str, Any]:
    response = request(
        github_url(config, f"/pulls/{config.pull_request_number}"),
        headers=github_headers(config.github_token),
    )
    if not isinstance(response, dict):
        raise RuntimeError("GitHub returned an invalid pull request response")
    return response


def get_changed_files(config: Config, request: JsonRequest) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    pages = MAX_CHANGED_FILES // 100
    for page in range(1, pages + 1):
        response = request(
            github_url(
                config,
                f"/pulls/{config.pull_request_number}/files?per_page=100&page={page}",
            ),
            headers=github_headers(config.github_token),
        )
        if not isinstance(response, list):
            raise RuntimeError("GitHub returned an invalid changed-files response")
        files.extend(file for file in response if isinstance(file, dict))
        if len(response) < 100:
            break
    return files


def build_prompt(title: str, files: list[dict[str, Any]]) -> str:
    prompt = f"Pull request title: {title}\n\nChanges:\n"
    truncated = False

    for changed_file in files:
        patch = changed_file.get("patch")
        if not isinstance(patch, str) or not patch:
            continue

        filename = changed_file.get("filename", "unknown")
        status = changed_file.get("status", "modified")
        section = f"\nFile: {filename} ({status})\n{patch}\n"
        remaining = MAX_PROMPT_CHARACTERS - len(prompt)
        if remaining <= 0:
            truncated = True
            break
        if len(section) > remaining:
            prompt += section[:remaining]
            truncated = True
            break
        prompt += section

    if truncated:
        suffix = "\n[Diff truncated to fit the model context.]"
        prompt = prompt[: MAX_PROMPT_CHARACTERS - len(suffix)] + suffix
    elif prompt.endswith("Changes:\n"):
        prompt += "No textual patches were available. Base the description on the title."

    return prompt


def azure_completions_url(config: Config) -> str:
    deployment = quote(config.azure_openai_deployment, safe="")
    query = urlencode({"api-version": config.azure_openai_version})
    return (
        f"{config.azure_openai_endpoint}/openai/deployments/{deployment}"
        f"/chat/completions?{query}"
    )


def generate_description(config: Config, prompt: str, request: JsonRequest) -> str:
    payload = {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "max_completion_tokens": MAX_COMPLETION_TOKENS,
    }
    headers = {"api-key": config.azure_openai_api_key}

    try:
        response = request(
            azure_completions_url(config),
            method="POST",
            headers=headers,
            payload=payload,
        )
    except RequestError as error:
        if error.status != 400 or "max_completion_tokens" not in error.body:
            raise
        payload["max_tokens"] = payload.pop("max_completion_tokens")
        response = request(
            azure_completions_url(config),
            method="POST",
            headers=headers,
            payload=payload,
        )

    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise RuntimeError("Azure OpenAI returned an invalid completion response") from error
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("Azure OpenAI returned an empty description")
    return content.strip()


def format_description(generated: str, pull_request_url: str) -> str:
    description = generated.strip()
    if description.startswith("## Description"):
        description = description.removeprefix("## Description").lstrip()

    sections = []
    if pull_request_url:
        sections.append(
            "## Live Environment\n\n"
            f"Access the pull request environment [here]({pull_request_url})."
        )
    sections.append(f"## Description\n\n{description}")
    return "\n\n".join(sections)


def update_pull_request(
    config: Config, description: str, request: JsonRequest
) -> None:
    request(
        github_url(config, f"/pulls/{config.pull_request_number}"),
        method="PATCH",
        headers=github_headers(config.github_token),
        payload={"body": description},
    )


def run(config: Config, request: JsonRequest = request_json) -> bool:
    pull_request = get_pull_request(config, request)
    current_body = pull_request.get("body")
    if isinstance(current_body, str) and current_body.strip() and not config.overwrite_description:
        print("Pull request already has a description; skipping generation.")
        return False

    title = pull_request.get("title")
    if not isinstance(title, str) or not title.strip():
        raise RuntimeError("GitHub returned a pull request without a title")

    files = get_changed_files(config, request)
    prompt = build_prompt(title, files)
    generated = generate_description(config, prompt, request)
    description = format_description(generated, config.pull_request_url)
    update_pull_request(config, description, request)
    print(f"Updated pull request #{config.pull_request_number} description.")
    return True


def main() -> int:
    try:
        run(Config.from_environment())
    except (RequestError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())