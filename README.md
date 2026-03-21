# Reusable GitHub Workflows

This repository publishes reusable GitHub Actions workflows from `.github/workflows`.

The reusable workflows in this repository are exposed through `on.workflow_call` only. Event triggers such as `pull_request`, `push`, `release`, and `workflow_dispatch` belong in the calling repository's wrapper workflows.

Other repositories can call them by referencing this repository on `main` or on a tag:

```yaml
jobs:
  call-workflow:
    uses: uug-ai/workflows/.github/workflows/pr-build.yml@main
    secrets: inherit
```

To pin to a version, replace `@main` with a tag such as `@v1.0.0`.

## General calling pattern

Create a small wrapper workflow in the consuming repository and forward the event-specific values as inputs.

```yaml
name: Pull Request Build

on:
  pull_request:
    types: [opened, synchronize, reopened]

jobs:
  build:
    uses: uug-ai/workflows/.github/workflows/pr-build.yml@v1.0.0
    with:
      project: ${{ github.event.repository.name }}
    secrets: inherit
```

## Required caller pattern for pull-request workflows

Reusable workflows do not automatically receive the original pull request payload. The caller must forward pull request values explicitly.

Example for `.github/workflows/pr-update-environment.yml`:

```yaml
name: Update Pull Request Environment

on:
  pull_request:
    types: [opened, synchronize, reopened]

jobs:
  update-environment:
    uses: uug-ai/workflows/.github/workflows/pr-update-environment.yml@main
    with:
      action: ${{ github.event.action }}
      project: ${{ github.event.repository.name }}
      pr_number: ${{ github.event.number }}
      sha: ${{ github.sha }}
      organization: uug-ai
      gitops_repo: uug-ai/gitops
    secrets: inherit
```

Example for `.github/workflows/pr-delete-environment.yml`:

```yaml
name: Delete Pull Request Environment

on:
  pull_request:
    types: [closed]

jobs:
  delete-environment:
    uses: uug-ai/workflows/.github/workflows/pr-delete-environment.yml@main
    with:
      action: ${{ github.event.action }}
      project: ${{ github.event.repository.name }}
      pr_number: ${{ github.event.number }}
    secrets: inherit
```

Example for `.github/workflows/pr-description.yml`:

```yaml
name: Autofill PR Description

on:
  pull_request:

jobs:
  describe:
    uses: uug-ai/workflows/.github/workflows/pr-description.yml@main
    with:
      pr_number: ${{ github.event.number }}
      pull_request_url: https://pr${{ github.event.number }}.api.kerberos.lol
    secrets: inherit
```

## Release workflow example

```yaml
name: Release

on:
  release:
    types: [created]

jobs:
  release:
    uses: uug-ai/workflows/.github/workflows/release-create.yml@main
    with:
      project: ${{ github.event.repository.name }}
      tag: ${{ github.event.release.tag_name }}
      gitops_repo: uug-ai/gitops
      gitops_file: environments/staging/my-service/values.yaml
      gitops_key: image.tag
    secrets: inherit
```

## Manual workflow example

```yaml
name: Create User Story

on:
  workflow_dispatch:
    inputs:
      issue_title:
        required: true
        type: string
      issue_description:
        required: true
        type: string
      complexity:
        required: true
        type: choice
        options: [Low, Medium, High]
      duration:
        required: true
        type: choice
        options: [1 day, 3 days, 1 week, 2 weeks, 1 month]

jobs:
  create-issue:
    uses: uug-ai/workflows/.github/workflows/issue-userstory-create.yml@main
    with:
      issue_title: ${{ inputs.issue_title }}
      issue_description: ${{ inputs.issue_description }}
      complexity: ${{ inputs.complexity }}
      duration: ${{ inputs.duration }}
    secrets: inherit
```

## Secrets

These reusable workflows expect the consuming repository to provide the same secrets the original workflows used, usually via `secrets: inherit`:

- `TOKEN`
- `USERNAME`
- `CODECOV_TOKEN`
- `AZURE_OPENAI_API_KEY`

Non-secret Azure OpenAI settings should be provided as repository variables or workflow inputs:

- `AZURE_OPENAI_ENDPOINT`
- `AZURE_OPENAI_VERSION`

The reusable workflows no longer require a direct OpenAI API key.

`issue-userstory-create.yml` can use the built-in GitHub token automatically, or an explicit `github_token` secret if you want to override it.

## Testing

This repository now validates workflow changes with a dedicated CI workflow:

- `actionlint` checks GitHub Actions syntax and common authoring mistakes.
- `check_workflows.py` enforces reusable-workflow contract rules for this repository, including the current secret policy.

That validation is intentionally static. Full end-to-end execution still needs to happen from a consuming repository, because several reusable workflows depend on caller repository contents, pull request event data, and external secrets.

If you want to exercise a reusable workflow directly during development, create a small wrapper workflow in the caller repository, or add a temporary wrapper in this repository that triggers on the event you want to test and then calls the reusable workflow.

For local validation, this repository also includes a Python devcontainer. Opening the repository in that devcontainer installs the dependencies needed to run:

```bash
python check_workflows.py
```