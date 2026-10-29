"""Offline contract tests for the reusable published SDK workflow."""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import textwrap
import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import unquote

WORKFLOW = (Path(__file__).resolve().parents[1] / ".github/workflows/published-sdk-e2e.yml").read_text()
README = (Path(__file__).resolve().parents[1] / "README.md").read_text()
# Run exactly the Python block embedded in the workflow, not a parallel implementation.
SCRIPT = textwrap.dedent(WORKFLOW.split("python3 - <<'PY'\n", 1)[1].split("\n          PY", 1)[0])
SHA = "a" * 40
REPOSITORIES = {
    "rudderlabs/analytics-go", "rudderlabs/rudder-sdk-node", "rudderlabs/rudder-sdk-python",
    "rudderlabs/rudder-sdk-ruby", "rudderlabs/rudder-sdk-ruby-sync",
    "rudderlabs/rudder-php-sdk", "rudderlabs/rudder-sdk-java",
    "rudderlabs/rudder-sdk-.net", "rudderlabs/rudder-sdk-rust",
}


class PreflightTests(unittest.TestCase):
    def run_preflight(self, *, repository="rudderlabs/analytics-go", version="1.2.3",
                      ref="refs/tags/v1.2.3", event_name="release", sha=SHA,
                      action="published", draft=False, prerelease=False, release_id=42,
                      published_at="2026-01-01T00:00:00Z", api_tag=None,
                      api_draft=False, api_prerelease=False, target_type="commit",
                      target_sha=SHA, api_failure=None):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            event = Path(directory) / "event.json"
            tag = ref.removeprefix("refs/tags/")
            event.write_text(json.dumps({"action": action, "release": {
                "tag_name": tag, "draft": draft, "prerelease": prerelease, "id": release_id,
            }}))
            calls = []

            def urlopen(request, timeout):
                calls.append(request)
                if api_failure:
                    raise api_failure
                path = unquote(request.full_url.split(f"/repos/{repository}/", 1)[1])
                if path.startswith("releases/tags/"):
                    data = {"id": 42, "tag_name": api_tag or tag, "draft": api_draft,
                            "prerelease": api_prerelease, "published_at": published_at}
                elif path.startswith("git/ref/tags/"):
                    data = {"object": {"type": target_type, "sha": target_sha}}
                elif path.startswith("git/tags/"):
                    data = {"object": {"type": "commit", "sha": SHA}}
                else:
                    raise AssertionError(path)
                return io.BytesIO(json.dumps(data).encode())

            environment = {"GITHUB_REPOSITORY": repository, "PACKAGE_VERSION": version,
                           "GITHUB_REF": ref, "GITHUB_SHA": sha,
                           "GITHUB_EVENT_NAME": event_name, "GITHUB_EVENT_PATH": str(event),
                           "GITHUB_OUTPUT": str(output), "CALLER_TOKEN": "read-only-token"}
            stderr = io.StringIO()
            with patch.dict(os.environ, environment, clear=True), \
                    patch("urllib.request.urlopen", side_effect=urlopen), \
                    contextlib.redirect_stderr(stderr):
                try:
                    exec(compile(SCRIPT, "published-sdk-e2e preflight", "exec"), {"__name__": "__main__"})
                    status = 0
                except SystemExit as error:
                    status = error.code
            return status, output.read_text() if output.exists() else "", calls, stderr.getvalue()

    def test_all_approved_repositories_pass_release_and_manual_retry(self):
        for repository in REPOSITORIES:
            for event_name in ("release", "workflow_dispatch"):
                with self.subTest(repository=repository, event_name=event_name):
                    status, output, calls, _ = self.run_preflight(
                        repository=repository, event_name=event_name)
                    self.assertEqual(status, 0)
                    self.assertEqual(output, "version=1.2.3\n")
                    self.assertEqual(len(calls), 2)
                    self.assertEqual(calls[0].get_header("Authorization"), "Bearer read-only-token")
                    self.assertEqual(calls[0].get_header("X-github-api-version"), "2026-03-10")

    def test_both_version_spellings_accept_annotated_v_tag(self):
        for version in ("1.2.3", "v1.2.3"):
            with self.subTest(version=version):
                status, output, calls, _ = self.run_preflight(
                    version=version, ref="refs/tags/v1.2.3",
                    target_type="tag", target_sha="b" * 40)
                self.assertEqual((status, output, len(calls)), (0, "version=1.2.3\n", 3))

    def test_bare_and_double_v_tags_fail_before_api_for_release_and_retry(self):
        for event_name in ("release", "workflow_dispatch"):
            for version in ("1.2.3", "v1.2.3"):
                for ref in ("refs/tags/1.2.3", "refs/tags/vv1.2.3"):
                    with self.subTest(event_name=event_name, version=version, ref=ref):
                        status, output, calls, _ = self.run_preflight(
                            version=version, ref=ref, event_name=event_name)
                        self.assertEqual((status, output, calls), (1, "", []))

    def test_invalid_identity_version_event_and_tag_never_reach_api(self):
        changes = (
            {"repository": "outsider/analytics-go"}, {"repository": "rudderlabs/other"},
            {"sha": "master"}, {"event_name": "pull_request"}, {"event_name": "push"},
            {"version": "1.2.3-rc.1"}, {"version": "1.2.3+build"},
            {"version": "01.2.3"}, {"version": ""}, {"version": "main"},
            {"ref": "refs/heads/main"}, {"ref": "refs/tags/v1.2.4"},
            {"version": "v1.2.3", "ref": "refs/tags/1.2.3"},
            {"action": "created"}, {"draft": True}, {"prerelease": True},
        )
        for change in changes:
            with self.subTest(change=change):
                status, output, calls, error = self.run_preflight(**change)
                self.assertEqual((status, output, calls), (1, "", []))
                self.assertNotIn("read-only-token", error)

    def test_rejects_stale_release_unpublished_tag_and_commit_mismatch(self):
        for change in ({"release_id": 43}, {"published_at": None},
                       {"api_draft": True}, {"api_prerelease": True},
                       {"api_tag": "v1.2.4"},
                       {"target_sha": "b" * 40}, {"target_type": "tree"},
                       {"event_name": "workflow_dispatch", "target_sha": "b" * 40}):
            with self.subTest(change=change):
                status, output, calls, _ = self.run_preflight(**change)
                self.assertEqual(status, 1)
                self.assertEqual(output, "")
                self.assertGreater(len(calls), 0)

    def test_api_failure_has_no_response_body_or_token_in_logs(self):
        failure = URLError("private response")
        status, output, _, error = self.run_preflight(api_failure=failure)
        self.assertEqual((status, output), (1, ""))
        self.assertNotIn("private response", error)
        self.assertNotIn("read-only-token", error)


class WorkflowContractTests(unittest.TestCase):
    def test_documented_manual_retry_cannot_reach_publication(self):
        release_section = README.split("In the SDK's existing `release: published` workflow", 1)[1]
        release_job = release_section.split("```yaml\n", 1)[1].split("\n```", 1)[0]
        retry_section = README.split("Create a separate `.github/workflows/retry-published-sdk-e2e.yml`", 1)[1]
        retry_workflow = retry_section.split("```yaml\n", 1)[1].split("\n```", 1)[0]

        self.assertIn("needs: publish", release_job)
        self.assertIn("package-version: ${{ github.event.release.tag_name }}", release_job)
        self.assertIn("  workflow_dispatch:\n", retry_workflow)
        self.assertNotIn("  release:\n", retry_workflow)
        jobs = retry_workflow.split("\njobs:\n", 1)[1]
        self.assertEqual(re.findall(r"^  ([a-z][a-z0-9-]*):$", jobs, re.MULTILINE),
                         ["published-e2e"])
        self.assertNotIn("steps:", jobs)
        self.assertNotIn("needs:", jobs)
        self.assertNotIn("id-token: write", retry_workflow)
        self.assertIn("package-version: ${{ inputs.package-version }}", jobs)
        self.assertIn("uses: rudderlabs/github-action-sdk-e2e/.github/workflows/published-sdk-e2e.yml@WORKFLOW_COMMIT_SHA", jobs)
        self.assertEqual(re.findall(r"^      ([A-Z][A-Z_]+): \$\{\{ secrets\.[^}]+\}\}$",
                                    jobs, re.MULTILINE), ["RELEASE_PRIVATE_KEY"])
        self.assertEqual(re.findall(r"^  ([a-z][a-z0-9-]*):$",
                                    WORKFLOW.split("\njobs:\n", 1)[1], re.MULTILINE),
                         ["validate", "result"])
        self.assertNotIn("id-token: write", WORKFLOW)

    def test_failed_dispatch_retains_outputs_and_fails_acceptance(self):
        import subprocess
        import yaml

        jobs = yaml.load(WORKFLOW, Loader=yaml.BaseLoader)["jobs"]
        validate, result = jobs["validate"], jobs["result"]
        dispatch = next(step for step in validate["steps"] if step.get("id") == "e2e")
        self.assertEqual(dispatch["continue-on-error"], "true")
        self.assertEqual(validate["outputs"]["outcome"], "${{ steps.e2e.outcome }}")
        self.assertEqual(result["needs"], "validate")
        self.assertEqual(result["if"], "always()")
        self.assertEqual(result["permissions"], {})
        for job, outcome in (("success", "success"), ("success", "failure"),
                             ("failure", ""), ("cancelled", ""), ("skipped", "")):
            with self.subTest(job=job, outcome=outcome):
                checked = subprocess.run(["bash", "-e", "-c", result["steps"][0]["run"]],
                                         env=dict(os.environ, VALIDATE_RESULT=job, E2E_OUTCOME=outcome))
                self.assertEqual(checked.returncode == 0, job == outcome == "success")

    def test_step_order_and_least_privilege(self):
        self.assertIn("on:\n  workflow_call:\n", WORKFLOW)
        self.assertIn("permissions: {}", WORKFLOW)
        self.assertIn("      contents: read", WORKFLOW)
        self.assertIn("    timeout-minutes: 55", WORKFLOW)
        self.assertLess(WORKFLOW.index("step-security/harden-runner@"), WORKFLOW.index("id: preflight"))
        self.assertLess(WORKFLOW.index("id: preflight"), WORKFLOW.index("id: token"))
        self.assertLess(WORKFLOW.index("id: token"), WORKFLOW.index("id: e2e"))
        self.assertIn("CALLER_TOKEN: ${{ github.token }}", WORKFLOW)
        self.assertEqual(WORKFLOW.count("secrets.RELEASE_PRIVATE_KEY"), 1)
        self.assertNotIn("secrets: inherit", WORKFLOW)
        self.assertNotIn("actions/checkout", WORKFLOW)

    def test_app_token_and_published_dispatch_contract(self):
        guard = WORKFLOW.split("- name: Require exactly one release App identifier", 1)[1].split(
            "- name: Create a central validation token", 1)[0]
        self.assertIn("rudderlabs/analytics-go", guard)
        self.assertIn('"$APP_ID"', guard)
        self.assertIn('"$CLIENT_ID"', guard)
        self.assertIn("private-key: ${{ secrets.RELEASE_PRIVATE_KEY }}", WORKFLOW)
        self.assertIn("owner: rudderlabs", WORKFLOW)
        self.assertIn("repositories: rudder-sdk-e2e-tests", WORKFLOW)
        self.assertIn("permission-actions: write", WORKFLOW)
        self.assertNotIn("skip-token-revoke: true", WORKFLOW)
        self.assertIn("suite: full", WORKFLOW)
        self.assertIn("timeout-seconds: '3000'", WORKFLOW)
        self.assertIn("package-version: ${{ steps.preflight.outputs.version }}", WORKFLOW)
        self.assertIn("uses: rudderlabs/github-action-sdk-e2e@", WORKFLOW)
        self.assertIn("run-id: ${{ steps.e2e.outputs.run-id }}", WORKFLOW)
        self.assertIn("run-url: ${{ steps.e2e.outputs.run-url }}", WORKFLOW)
        self.assertIn("cancel-in-progress: false", WORKFLOW)
        self.assertIn("github.run_attempt", WORKFLOW)
        self.assertNotIn("SLACK", WORKFLOW.upper())
        self.assertNotIn("permission-contents: write", WORKFLOW)

    def test_wrong_app_identifier_fails_before_token_creation(self):
        import subprocess

        guard = WORKFLOW.split("- name: Require exactly one release App identifier", 1)[1].split(
            "- name: Create a central validation token", 1)[0].split("run: |\n", 1)[1]
        for repository, client, app, success in (
            ("rudderlabs/analytics-go", "", "123", True),
            ("rudderlabs/analytics-go", "client", "", False),
            ("rudderlabs/rudder-sdk-node", "client", "", True),
            ("rudderlabs/rudder-sdk-node", "", "123", False),
            ("rudderlabs/rudder-sdk-node", "client", "123", False),
            ("rudderlabs/rudder-sdk-node", "", "", False),
        ):
            with self.subTest(repository=repository, client=client, app=app):
                result = subprocess.run(["bash", "-e", "-c", textwrap.dedent(guard)],
                                        env=dict(os.environ, GITHUB_REPOSITORY=repository,
                                                 CLIENT_ID=client, APP_ID=app),
                                        text=True, capture_output=True, check=False)
                self.assertEqual(result.returncode == 0, success)


if __name__ == "__main__":
    unittest.main()
