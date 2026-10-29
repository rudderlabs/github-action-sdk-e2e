# RudderStack SDK E2E action

Dispatch the central server SDK validation workflow and wait for its exact result.
SDK repositories use this action instead of copying the dispatch script.

The action repository is public so public SDK repositories can use it directly.
The test runner stays in the internal `rudderlabs/rudder-sdk-e2e-tests` repository.
The action contains no test credentials or live pipeline configuration.
Publishing an action on GitHub Marketplace is not required.

## Usage

Run on a Linux runner with Bash and Python 3.9–3.14. The action uses only the
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
        uses: step-security/harden-runner@dc2769abcc0fcfbadfc1df54081c1608579ef6b2 # v2.22.2
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
identifier. Use the identifier already configured in the SDK's release workflow.
The shared workflow accepts either identifier for every approved SDK. Current Go
callers use `vars.RELEASE_APP_ID`; the other eight callers use
`vars.RELEASE_APP_CLIENT_ID`.

In the SDK's existing `release: published` workflow, add this job under `jobs:`
after the native publisher job. Replace `publish` with the job that verifies the
package on the public registry. The `needs:` dependency allows acceptance only
after that job succeeds. Do not add `workflow_dispatch` to the publisher for
validation retries:

```yaml
  published-e2e:
    needs: publish
    permissions:
      contents: read
    uses: rudderlabs/github-action-sdk-e2e/.github/workflows/published-sdk-e2e.yml@WORKFLOW_COMMIT_SHA
    with:
      package-version: ${{ github.event.release.tag_name }}
      release-app-client-id: ${{ vars.RELEASE_APP_CLIENT_ID }}
    secrets:
      RELEASE_PRIVATE_KEY: ${{ secrets.RELEASE_PRIVATE_KEY }}
```

Create a separate `.github/workflows/retry-published-sdk-e2e.yml` for manual
validation of an existing release. In the Run workflow UI, select that release's
`vX.Y.Z` tag and enter the same exact version. This workflow has no publisher job
or publishing credentials:

```yaml
name: Retry published SDK E2E

on:
  workflow_dispatch:
    inputs:
      package-version:
        description: Existing published stable version to revalidate
        required: true
        type: string

permissions: {}

jobs:
  published-e2e:
    permissions:
      contents: read
    uses: rudderlabs/github-action-sdk-e2e/.github/workflows/published-sdk-e2e.yml@WORKFLOW_COMMIT_SHA
    with:
      package-version: ${{ inputs.package-version }}
      release-app-client-id: ${{ vars.RELEASE_APP_CLIENT_ID }}
    secrets:
      RELEASE_PRIVATE_KEY: ${{ secrets.RELEASE_PRIVATE_KEY }}
```

For Go, replace `release-app-client-id` in both jobs with
`release-app-id: ${{ vars.RELEASE_APP_ID }}`. Keep publication and public-registry
verification in the SDK's release workflow; the retry workflow cannot invoke
them. Do not run validation for release candidates or fork PRs. The preflight,
before any App token is created, checks the approved SDK
repository, inherited ref and SHA, the published release, and the tag's commit
(including annotated tags) using the caller's read-only token. The generated App
token is limited to Actions write on `rudder-sdk-e2e-tests` and is revoked at
job completion. The workflow runs the full suite and exposes `run-id` and
`run-url` outputs on success. On failure, use the central run URL in the logs or
job summary; failed reusable jobs may not deliver outputs. A retry never cancels
a previous central run; inspect that run and its cleanup before retrying.

The composite action above remains the entry point for source candidates. The
reusable workflow pins merged action PR #5 at
`a60faa2d07b1fe88478011e5388e94b168d46c67`. Central PR #32 provides published
adapters and version selection; merged central PRs #33 and #35 provide shared
isolation and validation results. Pin each SDK caller to the reviewed reusable
workflow commit after this workflow merges. Record hosted delivery and cleanup
acceptance at each SDK's next normal release. No SDK callers are changed here.

## Interface

| Input | Default | Purpose |
| --- | --- | --- |
| `token` | Required | Short-lived GitHub App token for central dispatch and polling. |
| `suite` | `reliability` | `reliability` or `full`. |
| `package-version` | Empty | Exact stable published version (`X.Y.Z` or `vX.Y.Z`). |
| `pr-number` | Unset | Explicit PR number for a trusted caller; requires `merge-sha`. |
| `merge-sha` | Unset | Explicit full lowercase current PR merge SHA; requires `pr-number`. |
| `timeout-seconds` | `3000` | Wait budget from 1 to 3000 seconds. Set the caller job timeout higher. |

| Output | Purpose |
| --- | --- |
| `run-id` | ID returned by GitHub for this dispatch. |
| `run-url` | Link to that run. Also written to the job summary. |

By default, the action derives the source repository and SHA from the caller's
GitHub event. For `pull_request`, it also sends the PR number from
`refs/pull/<number>/merge`. A trusted workflow that runs at a different event
SHA (for example, a base-branch workflow selecting a Release Please PR) may
pass both `pr-number` and `merge-sha` to select an explicit PR candidate:

```yaml
# In a trusted job, after retrieving the current PR number and merge_commit_sha
# from the GitHub API without checking out or executing PR-controlled code:
- uses: rudderlabs/github-action-sdk-e2e@ACTION_COMMIT_SHA
  with:
    token: ${{ steps.token.outputs.token }}
    suite: reliability
    pr-number: ${{ steps.candidate.outputs.pr-number }}
    merge-sha: ${{ steps.candidate.outputs.merge-sha }}
```

Both inputs are required together; partial, malformed, or conflicting
`pull_request` inputs fail before dispatch. On `pull_request_target`, explicit
PR numbers must match the event payload's PR number or dispatch fails.
Explicit inputs are claims,
not authorization: this action keeps the existing `selection: auto` central
contract, which checks repository eligibility and verifies that the supplied
SHA is the PR's **current merge SHA**. It never trusts a bot actor or branch
prefix as proof. The trusted caller must re-fetch and compare the PR's current
merge SHA after central validation succeeds; a changed PR invalidates the
result. For the Node release gate, use a separate caller PR pinned to the
reviewed full action commit and link it to this action change. Require a
stable aggregate check for eligible PRs and define fork, Dependabot, and
skipped-job behavior in that caller; review the required-check rule separately
and verify repository rules before claiming enforcement. This action alone
cannot gate npm publication or control upload order. Its current source-candidate
adapters support Go and Node; other SDKs need central adapter support before
using this action.

## Published-package validation

After the publishing job succeeds, call the action with `suite: full` and
`package-version: ${{ needs.publish.outputs.version }}`. Export that version from
the exact release commit used by the publishing job. The central workflow checks
that the corresponding `vX.Y.Z` tag resolves to the event commit and belongs to
the SDK's default-branch history. It installs the exact public registry package,
checks its resolved version and integrity, then runs the existing delivery and
cleanup checks. No SDK source package is substituted in this mode.

Deploy central `package_version` support before adopting this input. Published
adapters cover Go, Node, Python, Ruby, Ruby Sync, PHP, Rust, .NET and Java; each SDK
still needs its own caller workflow. This interface alone does not enable those
release workflows. PR events, ranges, prereleases and `latest` are rejected.

Keep the pre-publication source check before publishing and the published-package
check after publishing. For Go and PHP, creating the version tag starts
publication, so a release-event check is already post-publication. Report release
acceptance only after the published-package check passes. If acceptance fails,
the package remains published.

Retry only the failed validation job, or dispatch a validation-only workflow at
the existing release tag with the same exact package version. Never rerun a
successful publishing job merely to repeat E2E. Keep the shared action pinned to
a reviewed full commit SHA in both paths. Candidate and published invocations
use distinct request IDs, even within one caller run.

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
python3 -m pip install -r requirements-dev.txt
python3 -m ruff check .
go install github.com/rhysd/actionlint/cmd/actionlint@v1.7.12
"$(go env GOPATH)/bin/actionlint" -shellcheck=
```

Tests simulate the GitHub API and do not dispatch real workflows. CI checks each
Python minor version from 3.9 through 3.14. Prefer a currently supported Python
release for callers; the older versions are retained for compatibility.
The CLI and composite tests cover success, propagated failure, retained outputs,
missing credentials and transport-error redaction. All tests are credential-free.
The required `test` check fails if any matrix or lint job fails or is skipped.
Superseded local CI runs are cancelled; live central runs are never cancelled.

PR titles use Conventional Commits with lowercase subjects. The organization
title action skips bot authors, so reviewers must also check bot PR titles.
CODEOWNERS requests SDK team review. Dependabot and pinact delay external action
updates by seven days; internal `rudderlabs/*` actions are exempt. All action
references remain pinned to full commit SHAs.

## Releasing this action

Release Please runs on `main` with the existing release App. Its short-lived
token has contents and pull-request write access to this repository only.
The ordinary workflow token remains read-only. No npm package is published.

The first releasable commit after bootstrap commit
`8fcd9661885616652b992c1fea8df5c2a163f6e2` starts the `1.0.0` release PR.
The empty initial manifest means there is no previous release. `version.txt`
starts at `0.0.0` as an unreleased placeholder; it is not a published version.
The bootstrap boundary is ignored after the first release. Subsequent versions
follow Conventional Commits: fixes increment patch, features increment minor,
and breaking changes increment major. CI, docs and maintenance entries are hidden
from user-facing notes and do not alone require a release.

Review the generated version/changelog PR and its checks. Merging that release PR
creates the version tag and GitHub release. It does not publish or release an SDK.
Consumers continue to use full commit SHAs with version comments. Resolve the
approved version tag to its commit and update consumer pins in reviewed PRs;
there is no mutable major-version tag. Full live acceptance remains in each SDK's
normal release process.

## License

MIT
