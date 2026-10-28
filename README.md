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
    runs-on: ubuntu-latest
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
          permission-actions: write # Includes read access to run artifacts.
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

## Interface

| Input | Default | Purpose |
| --- | --- | --- |
| `token` | Required | Short-lived GitHub App token for central dispatch and polling. |
| `suite` | `reliability` | `reliability` or `full`. |
| `pr-number` | Unset | Explicit PR number for a trusted caller; requires `merge-sha`. |
| `merge-sha` | Unset | Explicit full lowercase current PR merge SHA; requires `pr-number`. |
| `timeout-seconds` | `3000` | Wait budget from 1 to 3000 seconds. Set the caller job timeout higher. |

| Output | Purpose |
| --- | --- |
| `run-id` | ID returned by GitHub for this dispatch. |
| `run-url` | Link to that run. Also written to the job summary. |
| `report` | Single-line JSON with verified full, non-PR Go candidate evidence; empty for PR reliability and other callers. |
| `validated-sha` | Go commit SHA copied from the verified report; empty for PR reliability and other callers. |

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

### Go candidate evidence (schema v1)

For a full, non-PR Go candidate, the central adapter uploads exactly one artifact
named `go-release-result-<central run ID>-<attempt>`, containing only
`release-result.json`, before its run concludes successfully. PR reliability
runs do not emit release evidence and pass only on the exact central run's
successful conclusion. The full-run JSON object has these required fields:

```json
{
  "schema_version": 1,
  "request_id": "analytics-go-42-2-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "repository": "rudderlabs/analytics-go",
  "commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "source_tree": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "module": "github.com/rudderlabs/analytics-go/v4",
  "version": "4.2.0",
  "central_run_id": 123,
  "central_run_attempt": 1,
  "delivery_passed": true,
  "restoration_verified": true,
  "lock_released": true
}
```

The central adapter derives module and version from the installed Go
candidate, and verifies live delivery, restoration, infrastructure cleanup,
and lock release before setting the three boolean results to `true`.
`central_run_attempt` is the central workflow attempt, distinct from the caller
attempt encoded in `request_id`.
The action lists artifacts **only for the returned successful run ID**, rejects
incomplete listings, and downloads the unique non-expired artifact by ID with
the existing central-repository App token (Actions write, including artifact
read). It verifies the GitHub artifact record's `sha256:` archive digest,
limits the download and JSON size, and checks every identity and result field
before exporting `report` and `validated-sha`. The signed download redirect
never receives the App token. No artifact URL or SHA is accepted from a caller;
a missing or mismatched report fails full non-PR Go validation. This digest
binds bytes to the GitHub artifact record, not to an independent attestation
of the tests. PR reliability and other SDK callers retain exact-run success
validation but have no Go report.

For Go publication, dispatch `suite: full`, require a nonempty verified
`report`, parse it as JSON (never as shell code), and compare its `commit`,
`source_tree`, `module`, and `version` to the proposed tag target **immediately
before creating a tag**.
Recheck the default-branch head at that boundary: a passing result for commit
A must never tag commit B. Run the full candidate check as a dependency of
publication, not an independent push workflow. Keep release PR generation
independent of this gate and keep post-publication registry-package validation
and notification as separate acceptance (including the exact registry-package
checks in analytics-go#91). A validation-only retry must not create tags or
upload a release. Roll out a stable aggregate required check in the Go caller:
run it even when a fork or Dependabot PR skips the privileged dispatch job;
make its policy explicit, and verify the repository rule configuration in a
separate reviewable step before claiming enforcement. This action does not
change repository rules. Do not rely on this output until the central
adapter report contract has been deployed and the caller pins the reviewed
commit of this action.

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
