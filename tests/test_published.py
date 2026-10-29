"""Published-package dispatch contract, without network or credentials."""
import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import dispatch

ENV = {"GITHUB_REPOSITORY": "rudderlabs/rudder-sdk-node", "GITHUB_SHA": "a" * 40,
       "GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_EVENT_NAME": "release"}


class PublishedDispatchTests(unittest.TestCase):
    def test_optional_input_preserves_existing_candidate_contract(self):
        self.assertEqual(dispatch.inputs(ENV), dispatch.inputs(dict(ENV, SDK_E2E_PACKAGE_VERSION="")))
        self.assertNotIn("package_version", dispatch.inputs(ENV))

    def test_every_sdk_uses_its_event_identity_and_a_distinct_published_request(self):
        repositories = ("analytics-go", "rudder-sdk-node", "rudder-sdk-python", "rudder-sdk-ruby", "rudder-sdk-ruby-sync",
                        "rudder-sdk-java", "rudder-sdk-.net", "rudder-sdk-rust", "rudder-php-sdk")
        for repo in repositories:
            env = dict(ENV, GITHUB_REPOSITORY=f"rudderlabs/{repo}")
            with self.subTest(repo=repo):
                candidate = dispatch.inputs(env)
                published = dispatch.inputs(dict(env, SDK_E2E_PACKAGE_VERSION="v1.2.3", SDK_E2E_SUITE="full"))
                self.assertEqual(published["package_version"], "v1.2.3")
                self.assertEqual(published["source_sha"], env["GITHUB_SHA"])
                self.assertEqual(published["source_repository"], env["GITHUB_REPOSITORY"])
                self.assertNotEqual(published["request_id"], candidate["request_id"])
                self.assertRegex(published["request_id"], r"\A[a-zA-Z0-9_-]+\Z")
                other = dispatch.inputs(dict(env, SDK_E2E_PACKAGE_VERSION="v1.2.4"))
                self.assertNotEqual(published["request_id"], other["request_id"])

    def test_invalid_version_and_pr_events_fail_before_dispatch(self):
        invalid = [{"SDK_E2E_PACKAGE_VERSION": version} for version in
                   ("latest", "^1.2.3", "1.2", "01.2.3", "1.2.3\n", "1.2.3-beta", "1.2.3+build", "../1.2.3", "$(id)")]
        invalid += [{"SDK_E2E_PACKAGE_VERSION": "1.2.3", "GITHUB_EVENT_NAME": event}
                    for event in ("pull_request", "pull_request_target")]
        invalid += [dict(SDK_E2E_PACKAGE_VERSION="1.2.3", GITHUB_REF="refs/pull/12/merge"),
                    dict(SDK_E2E_PACKAGE_VERSION="1.2.3", SDK_E2E_PR_NUMBER="12", SDK_E2E_MERGE_SHA="a" * 40)]
        for extra in invalid:
            with self.subTest(extra=extra), patch.dict(os.environ, dict(ENV, GH_TOKEN="private-token", **extra), clear=True), \
                    patch.object(dispatch, "dispatch_and_wait") as wait, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(dispatch.main(), 1)
                wait.assert_not_called()

    def test_exact_version_reaches_dispatch_and_failure_retains_outputs(self):
        selected = dispatch.inputs(dict(ENV, SDK_E2E_PACKAGE_VERSION="1.2.3"))
        run = {"id": 123, "repository": {"full_name": dispatch.REPOSITORY}, "event": "workflow_dispatch",
               "head_branch": "main", "path": f".github/workflows/{dispatch.WORKFLOW}",
               "display_title": f"sdk-e2e {selected['request_id']}", "status": "completed", "conclusion": "failure"}
        with tempfile.TemporaryDirectory() as tmp:
            output, summary = Path(tmp) / "output", Path(tmp) / "summary"
            with patch.dict(os.environ, {"GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary)}), \
                    patch.object(dispatch, "request", side_effect=[{"workflow_run_id": 123}, run]) as request, \
                    contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, "concluded failure"):
                dispatch.dispatch_and_wait("private-token", selected)
            self.assertEqual(request.call_args_list[0].args[2]["inputs"]["package_version"], "1.2.3")
            self.assertIn("run-id=123", output.read_text())
            self.assertIn("published package `1.2.3`", summary.read_text())
            self.assertNotIn("private-token", summary.read_text())
