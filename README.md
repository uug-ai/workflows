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
and `id-token: write`, or `contents: write` when using `finalize_release: true`.
The signing job inherits the caller's OIDC grant; legacy
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
as the native architecture-tag destination. Native builds first push
`staging-<run-id>-<attempt>-<architecture>` tags, and the assembled manifest uses
`staging-<run-id>-<attempt>`. Final native architecture tags, the version tag and
`latest` are assigned only after registry scans and signing/verification succeed.
Each promoted tag must resolve to its expected digest before the job continues.

```yaml
permissions:
  contents: write
  packages: write
  id-token: write

jobs:
  bump-release:
    uses: uug-ai/workflows/.github/workflows/release-bump.yml@main
    with:
      draft: true

  release:
    needs: bump-release
    uses: uug-ai/workflows/.github/workflows/release-create.yml@main
    with:
      tag: ${{ needs.bump-release.outputs.tag }}
      hardened: true
      finalize_release: true
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

This call belongs after `needs: bump-release` in a manually dispatched bump
wrapper. Set `draft: true` on its `release-bump.yml` call: it explicitly reserves
the source tag and creates a draft with generated notes. With
`finalize_release: true`, the hardened release job publishes that draft only
after all image checks, signature verification, final tag digest checks and
release-evidence upload succeed. GitOps waits for this entire job to succeed.
Keep the draft-release route as the sole release entrypoint; a
`release: created` or `release: published` build trigger cannot gate a release
that a user has already published manually.

Failures before verification leave only staging image tags. A failure partway
through promotion can leave some final aliases, but they reference verified
images; the GitHub release remains draft. Retry with the existing source tag.
Already-published releases remain published on retry, and their source tag is
not moved. Registry aliases and the GitHub release cannot be updated atomically;
the release is finalized only after every promotion succeeds.

For pull-request or push validation, set
`publish: false` and leave `create_gitops_pr: false`; neither manifests,
signatures, registry writes nor deployment proposals are produced. A manual
caller can pass an existing release tag to retry publication without a new
version or moving the source tag. Hardened builds never accept credential build
arguments. Worker/API callers with private dependencies may opt into
`use_private_module_auth: true`; `secrets.TOKEN` is then required and is passed
only as a BuildKit secret. Public-module workers should continue to set it false.

The expected image entrypoint defaults to `["/app/main"]`. To retain an existing
executable path, set `hardened_entrypoint` to its JSON array, for example
`'["./hub-pipeline-counting"]'`. Inspection must match that exact array; the
numeric identity, runtime file gates, smoke tests and scan gates still apply.
For a CMD-only image, set `hardened_entrypoint: 'null'` and an explicit
`hardened_command`, for example `'["./main"]'`. Both actual metadata fields are
then checked independently; they are not conflated into a combined argument
array. Null/empty entrypoints without a non-empty command are rejected.
Set `hardened_workdir` to assert a required absolute working directory without
changing existing callers' defaults.

Go binaries may use `-ldflags="-w -buildid="` to omit DWARF debug information
while retaining Go symbols and build/module information for vulnerability
analysis. Do not add `-s` or run a stripping tool. Both source and actual-binary
`govulncheck` remain required after applying these flags.

### Additional runtime contracts

Use `hardened_profile: standalone` for CLI, scheduler or HTTP images whose
repository smoke needs no shared databases or queues. Use `mongo-task` when the
smoke requires only the isolated MongoDB service (for example retention
cleanup). Both profiles must provide `image/smoke-test.sh hardened:scan`; it
must exercise the actual image with read-only root, dropped capabilities,
no-new-privileges and only explicitly required writable paths. Never point smoke
at production resources or send real notifications. Missing/failed smoke blocks
scans and publication; the common identity, file, vulnerability, secret, SBOM,
provenance, signing and promotion gates remain mandatory.

An application such as Vault that also ships browser assets must set
`browser_inventory: true` and include its inventory at the same
`/usr/share/frontend/package.json` and `package-lock.json` paths as the frontend.
Extraction, scanning, policy enforcement and browser SPDX generation then apply
regardless of runtime profile; frontend always requires them.

For native media workers, `worker_additional_smoke: true` requires that same
repository script **after** the existing queue readiness check. This is not a
replacement for queue checks: use it to verify the actual image's FFmpeg/OpenCV
or other native resources with a small local fixture. It is rejected for other
profiles rather than silently ignored.
Legacy workers that consume an already-provisioned durable quorum queue can set
`smoke_predeclare_quorum: true`. Only this opt-in enables the isolated RabbitMQ
management fixture and declares `smoke_queue` before worker startup. Declaration
failures block validation; queue names are URL-encoded and readiness still
requires the actual worker consumer. Existing callers retain their queue
bootstrap behavior. This option is rejected for non-worker profiles.

Image inspection also checks that UID/GID 65532 can search the working-directory
ancestors and CA directories and read the trust bundle (plus the API license
public key). `COPY --chmod=0444` of a single file can create unsearchable parent
directories. Prepare a root-owned tree with 0755 directories and 0444 public
files in the builder, then copy that tree without a recursive chmod. A passing
host-IP service smoke alone does not establish working DNS or TLS.
When the image contains an APK or dpkg package database, both local and
immutable-published scans must detect a supported OS and enumerate OS packages.
An unsupported/EOL OS or a Go-only result cannot certify copied native
libraries. Retain the matching distribution identity and package metadata;
repository-native inventory checks must additionally cover the actual copied
components.

Repository smoke receives `RUNTIME_EVIDENCE_DIR=runtime-evidence`; use it for
native test reports. The workflow retains that directory and `*-smoke.log` files
alongside the common evidence. Private native-test builds must use the provided
`GH_TOKEN` environment value only through a BuildKit secret, never command-line
credentials.

Set `hardened_dockerfile` only when validating a non-default Dockerfile. The
native test target, inspection image and published image all use that same
file; an empty value preserves the Dockerfile selected by the build context.
Each selected Dockerfile must expose the `test` target. Different variants
still need distinct consumer jobs/publication destinations to avoid image and
artifact collisions; this input does not invent new release variants.

### Hardened frontend releases

Set `hardened: true` and `hardened_profile: frontend` to reuse the same release
pipeline for the Hub nginx frontend. The `worker` profile remains the default;
existing callers and their MongoDB/RabbitMQ smoke checks are unchanged.
The frontend profile skips those services and runs the repository's
`image/smoke-test.sh hardened:scan` against the built image. That script must
verify read-only/no-capability startup with `/tmp` tmpfs, port 8080, public runtime
configuration, SPA routing, cache policies and missing static files. Both
profiles retain the numeric `/app/main` contract, native `test` build stage,
source/image scans, immutable rescans, SBOM/provenance, staged promotion,
signing and optional draft finalization. `smoke_queue` is required only for the
worker profile; frontend requires `use_private_module_auth: false`.
The frontend exports `/usr/share/frontend/package.json` and `package-lock.json`
from the inspected image to `frontend-inventory`, then scans it explicitly in
filesystem mode and emits a browser SPDX report. An OS/library image scan alone
does not detect standalone npm lockfiles. Both reports must satisfy the same
medium/high/critical policy; missing browser scan output must fail the gate.

### Hardened API releases

Set `hardened: true`, `hardened_profile: api` and
`use_private_module_auth: true` for Hub API. Supply `secrets.TOKEN` with read
access to its private Go dependencies. Native test, inspection and publication
builds pass it only as the BuildKit secret `github_token`, never a credential
build argument. The API Dockerfile requires that secret, scopes Git credential
lookup to GitHub and must not persist a token-bearing Git configuration.
Frontend retains its no-authentication requirement. Workers may use the same
secret-based opt-in when their dependency graph includes private modules.

The API profile starts MongoDB and RabbitMQ, retains the common `/app/main`,
numeric non-root identity, scans/SBOM/provenance/signing/promotion contracts,
and checks `CMD ["serve"]`, `/home/hub-api`, and the unchanged public key.
Its repository-provided `image/smoke-test.sh hardened:scan` must verify port8081
HTTP, database warmup, audit queue declaration, rejected unauthenticated access
and unchanged unlicensed health behavior under read-only/no-capability
constraints with writable `/tmp`. `smoke_queue` remains worker-only.

The reusable PR build and preview workflows additionally expose
`use_buildkit_secrets: true` for this Dockerfile. That opt-in replaces credential
build arguments with the same secret; defaults preserve existing callers.
Previews must still be explicitly gated by the consuming repository until its
runtime manifests support the hardened identity and temporary storage.

## Release bump example

`release-bump.yml` determines the next semantic version from the latest `v*` tag, creates a GitHub release with generated notes, and exposes the new tag as the `tag` output. Its `draft` input defaults to `false` for existing callers; hardened callers should use `draft: true` and chain `release-create.yml` with `finalize_release: true`. A release created with the default `GITHUB_TOKEN` does not emit a `release: created` event, so chain the workflows directly.

```yaml
name: Bump release

on:
  workflow_dispatch:
    inputs:
      bump:
        description: "Which part of the version to bump"
        required: true
        default: patch
        type: choice
        options: [major, minor, patch]

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

The `bump` input accepts `major`, `minor`, or `patch` and defaults to `patch`.

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
