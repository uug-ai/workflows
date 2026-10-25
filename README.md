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

`pr-build.yml` defaults to `language: go`. When the caller sets `language: python`, the workflow skips Go setup, formatting, vetting, tests, and the local `go build` step, so Go-specific inputs such as `go_version`, `go_dir`, and `go_sum_path` do not need to be provided.

`pr-build.yml` and `test-coverage.yaml` default to regular `go test` runs. Set `go_test_race: true` in the caller to enable `go test -race`; the reusable workflow enables `CGO_ENABLED=1` automatically for that test step.

```yaml
jobs:
  build:
    uses: uug-ai/workflows/.github/workflows/pr-build.yml@v1.0.0
    with:
      project: ${{ github.event.repository.name }}
      go_test_race: true
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
      organization: your-org
      gitops_repo: your-org/gitops
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

permissions:
  pull-requests: write

jobs:
  describe:
    uses: uug-ai/workflows/.github/workflows/pr-description.yml@main
    with:
      pr_number: ${{ github.event.number }}
      pull_request_url: https://pr${{ github.event.number }}.api.example.com
      azure_openai_deployment: your-azure-deployment
    secrets: inherit
```

The generator is maintained as the first-party composite action in
`.github/actions/pr-description` and calls the GitHub and Azure OpenAI APIs
directly without a container image or runtime package installation.
When `azure_openai_deployment` is omitted, the workflow uses the inherited
`AZURE_OPENAI_DEPLOYMENT` secret before checking configuration variables.
For the Azure OpenAI v1 API, set `AZURE_OPENAI_ENDPOINT` to the complete base
URL, such as `https://your-resource.openai.azure.com/openai/v1`. The generator
calls `chat/completions` below that URL and sends the deployment name as
`model`; `AZURE_OPENAI_VERSION` is not required. Legacy resource-root endpoints
remain supported and require `AZURE_OPENAI_VERSION`. The action retries
rate-limit, timeout, server, malformed-response, and empty-completion failures up
to three times. It respects numeric `Retry-After` headers and increases the
completion budget when Azure reports that the token limit was reached.

## Release workflow example

```yaml
name: Release

on:
  release:
    types: [created]

permissions:
  contents: read
  packages: write

jobs:
  release:
    uses: uug-ai/workflows/.github/workflows/release-create.yml@main
    with:
      project: ${{ github.event.repository.name }}
      tag: ${{ github.event.release.tag_name }}
      create_gitops_pr: true
      gitops_repo: your-org/gitops
      gitops_file: environments/staging/my-service/values.yaml
      gitops_key: image.tag
    secrets: inherit
```

Set `create_gitops_pr: false` if you only want to publish images and manifests without opening a GitOps pull request.

`release-create.yml` builds one image per entry in `runner_matrix` and then creates the published manifests from the `architecture` values in that matrix, so the manifest contents stay aligned with the configured build targets.

The caller must grant `packages: write`. A reusable workflow can narrow the
caller's `GITHUB_TOKEN`, but it cannot elevate a caller whose permission ceiling
is read-only. The repository's default workflow permission may remain `read`;
the explicit caller grant above applies only to this release workflow.

### Hardened Go worker releases

The existing `release-create.yml` also owns the hardened release path; callers
do not need a separate image-hardening workflow. It is opt-in so existing
services retain their current Dockerfile, authentication and permission behavior.

Set `hardened: true`, `use_private_module_auth: false` and the worker's
`smoke_queue`. Grant the calling release job `contents: read`, `packages: write`
and `id-token: write`. The signing job inherits the caller's OIDC grant; legacy
callers are not required to grant it. Set the native `runner_matrix` explicitly
when using GitHub-hosted arm64 runners.

This profile expects a Dockerfile `test` stage running native tests and
source/binary reachability checks, an image running as `65532:65532` with
entrypoint `/app/main`, and a MongoDB/RabbitMQ worker exposing `/metrics` on
port 8080. Builds, read-only/no-capability smoke tests, source secret/config
checks, and medium/high/critical image gates precede publishing. Native registry
rescans use the explicit platform. SBOMs, maximum provenance and verified
keyless signatures precede GitOps promotion. All evidence remains in Actions
artifacts; signing identities use
`https://github.com/uug-ai/workflows/.github/workflows/release-create.yml@refs/...`.
Native images retain the existing package layout:
`ghcr.io/<organization>/<project>-platform:<architecture>-<tag>`.
The version and `latest` manifests in `ghcr.io/<organization>/<project>` are
assembled from those native package digests; the unsuffixed package is not used
as the native architecture-tag destination.

```yaml
permissions:
  contents: read
  packages: write
  id-token: write

jobs:
  release:
    uses: uug-ai/workflows/.github/workflows/release-create.yml@main
    with:
      tag: ${{ github.event.release.tag_name }}
      hardened: true
      use_private_module_auth: false
      smoke_queue: kcloud-monitor-queue
      runner_matrix: >-
        [
          {"architecture":"amd64","runner":"ubuntu-24.04"},
          {"architecture":"arm64","runner":"ubuntu-24.04-arm"}
        ]
      create_gitops_pr: true
      gitops_key: kerberospipeline.monitor.tag
    secrets: inherit
```

The same call belongs after `needs: bump-release` in the bump wrapper, using
`needs.bump-release.outputs.tag`. For pull-request or push validation, set
`publish: false` and leave `create_gitops_pr: false`; neither manifests,
signatures, registry writes nor deployment proposals are produced. A manual
caller can pass an existing release tag to retry publication without a new
version or moving the source tag. Hardened builds currently support public
modules only and never accept credential build arguments.

## Release bump example

`release-bump.yml` determines the next semantic version from the latest `v*` tag, creates a GitHub release with generated notes, and exposes the new tag as the `tag` output. Because a release created with the default `GITHUB_TOKEN` does not emit a `release: created` event, chain `release-create.yml` directly after it.

```yaml
name: Bump release

on:
  workflow_dispatch:
    inputs:
      bump:
        description: "Which part of the version to bump"
        required: true
        default: fix
        type: choice
        options: [major, minor, fix]

permissions:
  contents: write
  packages: write

jobs:
  bump-release:
    uses: uug-ai/workflows/.github/workflows/release-bump.yml@main
    with:
      bump: ${{ github.event.inputs.bump }}
    secrets: inherit

  release:
    needs: bump-release
    uses: uug-ai/workflows/.github/workflows/release-create.yml@main
    with:
      project: ${{ github.event.repository.name }}
      tag: ${{ needs.bump-release.outputs.tag }}
      create_gitops_pr: true
      gitops_repo: your-org/gitops
      gitops_file: environments/staging/my-service/values.yaml
      gitops_key: image.tag
      gitops_value: ghcr.io/your-org/my-service:${{ needs.bump-release.outputs.tag }}
    secrets: inherit
```

The `bump` input accepts `major`, `minor`, or `fix` and defaults to `fix`.

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

Azure OpenAI endpoint/version can be provided as inherited secrets or repository variables:

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

```bash
python check_workflows.py
```
