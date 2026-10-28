# RudderStack SDK E2E action

Dispatch the central server SDK validation workflow and wait for its exact result.
SDK repositories use this action instead of copying the dispatch script.

The action repository is public so public SDK repositories can use it directly.
The test runner stays in the internal `rudderlabs/rudder-sdk-e2e-tests` repository.
The action contains no test credentials or live pipeline configuration.
Publishing an action on GitHub Marketplace is not required.

## Usage

Run on a Linux runner with Bash and Python 3.9 or later. The action uses only the
Python standard library. No checkout of the SDK repository is required.

Pin `rudderlabs/github-action-sdk-e2e` to a reviewed full commit SHA.
Replace `ACTION_COMMIT_SHA` below with that SHA.

```yaml
name: Server SDK E2E

on:
  pull_request:
    branches: [master]
    types: [opened, reopened, synchronize]
  push:
    branches: [master]
  workflow_dispatch:

permissions:
  contents: read

jobs:
  validate:
    # Fork and Dependabot PRs cannot access the release App secret.
    if: >-
      github.event_name != 'pull_request' ||
      (github.event.pull_request.head.repo.full_name == github.repository &&
       github.actor != 'dependabot[bot]' &&
       github.event.pull_request.user.login != 'dependabot[bot]')
    runs-on: ubuntu-24.04
    timeout-minutes: 55
    steps:
      - name: Harden the runner
        uses: step-security/harden-runner@e14015d583714f6e62063499dc959a02595150a1 # v2.21.1
        with:
          egress-policy: audit
      - name: Create a central validation token
        id: token
        uses: actions/create-github-app-token@bcd2ba49218906704ab6c1aa796996da409d3eb1 # v3.2.0
        with:
          client-id: ${{ vars.RELEASE_APP_CLIENT_ID }}
          private-key: ${{ secrets.RELEASE_PRIVATE_KEY }}
          owner: rudderlabs
          repositories: rudder-sdk-e2e-tests
          permission-actions: write
      - name: Run SDK E2E
        id: e2e
        uses: rudderlabs/github-action-sdk-e2e@ACTION_COMMIT_SHA
        with:
          token: ${{ steps.token.outputs.token }}
          suite: ${{ github.event_name == 'pull_request' && 'reliability' || vars.SDK_E2E_SUITE || 'reliability' }}
```

Reuse the existing release App credentials already available to the SDK workflow.
Verify that its installation includes the central test repository and grants
Actions write permission. Scope the generated token to that repository and
permission, as shown above. A separate E2E App or private key is not required.

Use the identifier configured by the repository's release workflow. Node uses
`client-id: vars.RELEASE_APP_CLIENT_ID`; Go currently uses the supported legacy
`app-id: vars.RELEASE_APP_ID` input. The Client ID and numeric App ID are distinct.
Keep the private key in GitHub Secrets and pass only the generated token to this
action. The caller's ordinary `GITHUB_TOKEN` cannot dispatch into the internal
test repository.
The workflow also supports release events; the action uses the event's commit SHA.

Keep `SDK_E2E_SUITE` unset or set to `reliability` until live delivery and cleanup
acceptance are complete. Then set it to `full` for automatic default-branch and
release validation. The expression above keeps PR candidates on `reliability`.
Full candidate validation requires a commit in the SDK's default-branch history.
Fork and Dependabot PRs skip this privileged caller; merged changes still run on
the default-branch push. Do not copy the release App key into Dependabot secrets.

## Published package validation

The reusable workflow `.github/workflows/published-sdk-e2e.yml` validates the
**published package**, not a source candidate. After the SDK's native publisher
has completed successfully, call the workflow on the `release: published` event.
For a validation-only retry of an existing published version, use
`workflow_dispatch` on its release tag; do not republish or dispatch the central
suite from a PR or branch. The caller must pass its exact stable package version
(`X.Y.Z` or `vX.Y.Z`). Both input spellings require the canonical `vX.Y.Z`
release tag; the central published-package validator verifies that tag.
Bare release tags, prereleases, and drafts are rejected.

Pin the **reusable workflow** to a reviewed full commit SHA when it is merged;
do not use `./.github/workflows/...` across repositories. The caller job must
allow `contents: read` (the preflight reads the caller's release and tag), pass
the existing release App secret explicitly, and use exactly one release App
identifier. Go uses `vars.RELEASE_APP_ID`; the other eight callers use
`vars.RELEASE_APP_CLIENT_ID`. For example, in an SDK publisher workflow:

```yaml
on:
  release:
    types: [published]
  workflow_dispatch:
    inputs:
      package-version:
        description: Existing published stable version to revalidate
        required: true
        type: string

permissions: {}

jobs:
  published-e2e:
    # Keep the SDK's existing publish job and its dependency ordering intact.
    # For manual retries select the existing release tag in GitHub's Run workflow UI.
    permissions:
      contents: read
    uses: rudderlabs/github-action-sdk-e2e/.github/workflows/published-sdk-e2e.yml@WORKFLOW_COMMIT_SHA
    with:
      package-version: ${{ github.event_name == 'release' && github.event.release.tag_name || inputs.package-version }}
      release-app-client-id: ${{ vars.RELEASE_APP_CLIENT_ID }}
    secrets:
      RELEASE_PRIVATE_KEY: ${{ secrets.RELEASE_PRIVATE_KEY }}
```

For Go, replace `release-app-client-id` with `release-app-id: ${{ vars.RELEASE_APP_ID }}`.
Adapt the trigger and `needs:` to each SDK's native publisher: the public package
must exist before validation. Do not run this workflow for release candidates or
fork PRs. The preflight, before any App token is created, checks the approved SDK
repository, inherited ref and SHA, the published release, and the tag's commit
(including annotated tags) using the caller's read-only token. The generated App
token is limited to Actions write on `rudder-sdk-e2e-tests` and is revoked at
job completion. The workflow runs the full suite and exposes `run-id` and
`run-url` outputs on success. On failure, use the central run URL in the logs or
job summary; failed reusable jobs may not deliver outputs. A retry never cancels
a previous central run; inspect that run and its cleanup before retrying.

The composite action above remains the entry point for source candidates. Its
published `package-version` input comes from shared action PR #5; the central
published adapters and version selection come from central PR #32. This reusable
workflow requires both reviewed and available commits, as well as hosted
acceptance of live delivery and cleanup, before the nine SDK caller workflows
are migrated. The SHA shown in the reusable workflow is the shared action PR #5
head and must be reviewed before adoption. No SDK callers are changed here.

## Interface

| Input | Default | Purpose |
| --- | --- | --- |
| `token` | Required | Short-lived GitHub App token for central dispatch and polling. |
| `suite` | `reliability` | `reliability` or `full`. |
| `timeout-seconds` | `3000` | Wait budget from 1 to 3000 seconds. Set the caller job timeout higher. |

| Output | Purpose |
| --- | --- |
| `run-id` | ID returned by GitHub for this dispatch. |
| `run-url` | Link to that run. Also written to the job summary. |

The action derives the source repository and SHA from the caller's GitHub event.
For `pull_request`, it also sends the PR number from `refs/pull/<number>/merge`.
The central runner validates repository authorization and candidate provenance.
Its current source-candidate adapters support Go and Node; other SDKs need central
adapter support before using this action.

The target is fixed to `server-sdk-validation.yml` on `main` in
`rudderlabs/rudder-sdk-e2e-tests`. That workflow must be deployed before use.
The action uses GitHub API version `2026-03-10`, which returns the dispatched run
ID directly. It verifies the run's repository, workflow, event, branch, and request
identity on every poll. It never guesses the latest matching run.

GitHub can briefly return the workflow name before expanding the request-specific
run title. The action waits up to 30 seconds for that title while checking all
other identity fields. This wait is part of the overall timeout. A completed run
must have the exact request title before its result can pass.

Only a `success` conclusion passes. Failed, cancelled, skipped, mismatched, or
timed-out runs fail the action. API errors and missing run IDs also fail.
The action does not retry the dispatch POST because that could start duplicate
runs. Once dispatched, the run URL remains available even if validation fails.

Timeout or cancellation of the caller does not cancel the central run. A live
run may need to restore its pipeline. Inspect the linked run before retrying.
The caller controls triggers and release dependencies. The action does not
change branch protection or require release-please to wait for E2E.

## Development

```sh
python3 -m unittest discover -s tests -v
```

Tests simulate the GitHub API and do not dispatch real workflows. CI also loads
the composite action and verifies that missing credentials fail the step.

## License

MIT
