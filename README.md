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
For source candidates, including release-event pre-publication checks, the action
uses the event's commit SHA. The release workflow must ensure its event/checkout
points to the exact commit it builds; it must not substitute the latest branch.

Keep `SDK_E2E_SUITE` unset or set to `reliability` until live delivery and cleanup
acceptance are complete. Then set it to `full` for automatic default-branch and
release validation. The expression above keeps PR candidates on `reliability`.
Full candidate validation requires a commit in the SDK's default-branch history.
Fork and Dependabot PRs skip this privileged caller; merged changes still run on
the default-branch push. Do not copy the release App key into Dependabot secrets.

## Interface

| Input | Default | Purpose |
| --- | --- | --- |
| `token` | Required | Short-lived GitHub App token for central dispatch and polling. |
| `suite` | `reliability` | `reliability` or `full`; pass `full` explicitly for release acceptance. |
| `package-version` | Empty | Exact published Node npm version, without `v` or a range; requires `source-sha`. |
| `source-sha` | Empty | Full lowercase release commit SHA; only accepted with `package-version`. |
| `timeout-seconds` | `3000` | Wait budget from 1 to 3000 seconds. Set the caller job timeout higher. |

| Output | Purpose |
| --- | --- |
| `run-id` | ID returned by GitHub for this dispatch. |
| `run-url` | Link to that run. Also written to the job summary. |

The action derives the source repository from the caller's GitHub event and,
for source candidates, uses its SHA. For `pull_request`, it also sends the PR
number from `refs/pull/<number>/merge`. Published Node requests instead use the
explicit `source-sha` release commit.
The central runner validates repository authorization and candidate provenance.
Its source-candidate adapters support Go and Node; other SDKs need central adapter
support before using this action.

### Exact published Node package

After npm publication, pass the **exact published version and its immutable
release source commit** together. The central Node adapter selects that registry
package (not the source tarball or latest dist-tag), verifies its source version,
and checks the source commit against default-branch history. The action validates
input syntax but cannot prove version/SHA provenance locally; central validation
must verify the requested package and source belong together. The release
workflow must resolve the release tag/metadata to its commit and verify the
checked-out publish artifact's
version matches the release metadata. Never use the latest branch head.

```yaml
- name: Accept the published Node release
  uses: rudderlabs/github-action-sdk-e2e@ACTION_COMMIT_SHA
  with:
    token: ${{ steps.token.outputs.token }}
    suite: full
    package-version: ${{ needs.deploy-tag.outputs.version }}
    source-sha: ${{ needs.deploy-tag.outputs.source-sha }}
```

`ACTION_COMMIT_SHA` must be replaced with the **reviewed full commit** of this
action after central published-package support is deployed. Do not pin a
placeholder, moving branch, or unreviewed commit in an SDK workflow. Keep the
pre-publication source-candidate gate ahead of publishing, and gate release-
complete notifications on successful post-publication full acceptance. If that
acceptance fails, report it visibly: the published npm package is not rolled back.

For validation-only retries, resolve the requested version's release tag and
immutable commit independently of the manually triggered workflow's `GITHUB_SHA`.
Pass both to this action with `suite: full` and **do not call the publish job**.
The action rejects missing/malformed release SHA, ranges/dist-tags and PR or
source-candidate SHA overrides. Retries get their own request identity; inspect
previous central runs before retrying, since a timed-out caller cannot cancel a
central live run. The Node caller should have a static test where a manual event
SHA differs from the release commit but dispatch receives the latter.

Tracking: [SDK-5962 (shared interface)](https://linear.app/rudderstack/issue/SDK-5962/expose-exact-published-version-validation-through-the-shared-e2e),
[SDK-5961 (central published-package adapter)](https://linear.app/rudderstack/issue/SDK-5961/validate-exact-published-server-sdk-packages-in-the-central-e2e),
[SDK-5963 (Node workflow)](https://linear.app/rudderstack/issue/SDK-5963).

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
