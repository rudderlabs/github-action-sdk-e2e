"""Dispatch contract tests. No real GitHub workflows are started."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import dispatch

SHA = "a" * 40
ENVIRONMENT = {
    "GITHUB_REPOSITORY": "rudderlabs/analytics-go",
    "GITHUB_SHA": SHA,
    "GITHUB_RUN_ID": "42",
    "GITHUB_RUN_ATTEMPT": "2",
}


class InputTests(unittest.TestCase):
    def test_push_release_and_manual_events_use_the_exact_event_sha(self):
        for event in ("push", "release", "workflow_dispatch"):
            with self.subTest(event=event):
                selected = dispatch.inputs(dict(ENVIRONMENT, GITHUB_EVENT_NAME=event))
                self.assertEqual(selected["source_repository"], "rudderlabs/analytics-go")
                self.assertEqual(selected["source_sha"], SHA)
                self.assertEqual(selected["suite"], "reliability")
                self.assertNotIn("source_pr", selected)

    def test_pr_sends_merge_sha_and_pr_number(self):
        selected = dispatch.inputs(dict(ENVIRONMENT, GITHUB_EVENT_NAME="pull_request",
                                        GITHUB_REF="refs/pull/87/merge", SDK_E2E_SUITE="full"))
        self.assertEqual(selected["source_pr"], "87")
        self.assertEqual(selected["source_sha"], SHA)
        self.assertEqual(selected["suite"], "full")

    def test_new_sdk_repository_does_not_require_an_action_allowlist_update(self):
        selected = dispatch.inputs(dict(ENVIRONMENT, GITHUB_REPOSITORY="rudderlabs/rudder-sdk-python"))
        self.assertEqual(selected["selection"], "auto")
        self.assertEqual(selected["source_repository"], "rudderlabs/rudder-sdk-python")

    def test_rejects_invalid_repository_sha_suite_or_identity(self):
        for field, value in (("GITHUB_REPOSITORY", "someone/analytics-go"),
                             ("GITHUB_REPOSITORY", "rudderlabs/repo/../../other"),
                             ("GITHUB_SHA", "master"), ("GITHUB_SHA", "A" * 40),
                             ("SDK_E2E_SUITE", "skip"), ("GITHUB_RUN_ID", "42\n"),
                             ("GITHUB_RUN_ATTEMPT", "bad")):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                dispatch.inputs(dict(ENVIRONMENT, **{field: value}))

    def test_pr_rejects_non_merge_refs(self):
        for ref in ("", "refs/heads/master", "refs/pull/87/head", "refs/pull/0/merge"):
            with self.subTest(ref=ref), self.assertRaisesRegex(ValueError, "PR merge ref"):
                dispatch.inputs(dict(ENVIRONMENT, GITHUB_EVENT_NAME="pull_request", GITHUB_REF=ref))

    def test_retries_have_distinct_request_ids(self):
        first = dispatch.inputs(ENVIRONMENT)
        second = dispatch.inputs(dict(ENVIRONMENT, GITHUB_RUN_ATTEMPT="3"))
        self.assertNotEqual(first["request_id"], second["request_id"])


class RequestTests(unittest.TestCase):
    def test_dispatch_uses_versioned_api_and_json_body(self):
        with patch.object(dispatch, "urlopen", return_value=io.BytesIO(b'{"workflow_run_id":123}')) as call:
            result = dispatch.request("/actions/workflows/server-sdk-validation.yml/dispatches",
                                      "test-token", {"ref": "main", "inputs": {"source_sha": SHA}})
        request = call.call_args.args[0]
        self.assertEqual(request.full_url, dispatch.API + "/actions/workflows/server-sdk-validation.yml/dispatches")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("X-github-api-version"), "2026-03-10")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
        self.assertEqual(json.loads(request.data)["inputs"]["source_sha"], SHA)
        self.assertEqual(result["workflow_run_id"], 123)

    def test_http_failure_does_not_expose_response_body(self):
        error = HTTPError("https://api.github.com", 403, "Forbidden", {},
                          io.BytesIO(b"private server response"))
        with patch.object(dispatch, "urlopen", side_effect=error), \
                self.assertRaisesRegex(RuntimeError, "^GitHub API request failed: HTTP 403$"):
            dispatch.request("/actions/runs/123", "test-token")


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.selected = dispatch.inputs(ENVIRONMENT)
        self.run = {"id": 123, "repository": {"full_name": dispatch.REPOSITORY},
                    "event": "workflow_dispatch", "head_branch": "main",
                    "path": ".github/workflows/server-sdk-validation.yml",
                    "display_title": f"sdk-e2e {self.selected['request_id']}",
                    "status": "completed", "conclusion": "success"}
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name) / "output"
        self.summary = Path(temporary.name) / "summary"
        self.environment = {"GITHUB_OUTPUT": str(self.output), "GITHUB_STEP_SUMMARY": str(self.summary)}

    def wait(self, responses, **kwargs):
        with patch.object(dispatch, "request", side_effect=responses) as request, \
                patch.dict(os.environ, self.environment), contextlib.redirect_stdout(io.StringIO()):
            result = dispatch.dispatch_and_wait("test-token", self.selected, poll=0, **kwargs)
        return result, request

    def test_waits_for_exact_run_and_exports_outputs(self):
        url, request = self.wait([{"workflow_run_id": 123},
                                  dict(self.run, status="in_progress"), self.run])
        self.assertTrue(url.endswith("/123"))
        self.assertEqual(request.call_args_list[1].args[0], "/actions/runs/123")
        self.assertEqual(request.call_args_list[2].args[0], "/actions/runs/123")
        self.assertEqual(request.call_args_list[0].args[2],
                         {"ref": "main", "inputs": self.selected})
        self.assertEqual(self.output.read_text(), f"run-id=123\nrun-url={url}\n")
        self.assertIn(SHA, self.summary.read_text())
        self.assertIn(url, self.summary.read_text())
        self.assertNotIn("test-token", self.summary.read_text())

    def test_all_unsuccessful_conclusions_fail(self):
        for conclusion in ("failure", "cancelled", "timed_out", "skipped", "neutral", "action_required", None):
            with self.subTest(conclusion=conclusion), self.assertRaisesRegex(RuntimeError, "concluded"):
                self.wait([{"workflow_run_id": 123}, dict(self.run, conclusion=conclusion)])
        self.assertIn("run-id=123", self.output.read_text())

    def test_missing_or_invalid_run_id_never_guesses_a_run(self):
        for response in ({}, {"workflow_run_id": None}, {"workflow_run_id": True},
                         {"workflow_run_id": "123"}, {"workflow_run_id": -1}):
            with self.subTest(response=response), self.assertRaisesRegex(RuntimeError, "refusing to guess"):
                self.wait([response])
        self.assertFalse(self.output.exists())

    def test_rejects_other_run_repository_ref_workflow_event_and_request(self):
        for field, value in (("id", 124), ("repository", {"full_name": "other/repo"}),
                             ("event", "push"), ("head_branch", "other"), ("path", "other.yml"),
                             ("display_title", "sdk-e2e another-request")):
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, "identity"):
                self.wait([{"workflow_run_id": 123}, dict(self.run, **{field: value})])

    def test_deadline_fails_and_keeps_run_url(self):
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            self.wait([{"workflow_run_id": 123}], timeout=0)
        self.assertIn("/actions/runs/123", self.output.read_text())

    def test_poll_api_failure_does_not_pass(self):
        with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
            self.wait([{"workflow_run_id": 123}, RuntimeError("GitHub API request failed: HTTP 403")])


class EntrypointTests(unittest.TestCase):
    def test_missing_token_and_invalid_timeout_fail_before_dispatch(self):
        for extra in ({}, {"GH_TOKEN": ""}, *({"GH_TOKEN": "test-token", "SDK_E2E_TIMEOUT_SECONDS": value}
                                            for value in ("0", "-1", "3001", "1.5", "bad", "15\n"))):
            with self.subTest(extra=extra), patch.dict(os.environ, dict(ENVIRONMENT, **extra), clear=True), \
                    patch.object(dispatch, "dispatch_and_wait") as wait, \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(dispatch.main(), 1)
                wait.assert_not_called()

    def test_valid_timeout_reaches_dispatch_and_failure_reaches_exit_status(self):
        environment = dict(ENVIRONMENT, GH_TOKEN="test-token", SDK_E2E_TIMEOUT_SECONDS="60")
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(dispatch, "dispatch_and_wait") as wait:
            self.assertEqual(dispatch.main(), 0)
            wait.assert_called_once_with("test-token", dispatch.inputs(environment), timeout=60)
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(dispatch, "dispatch_and_wait", side_effect=RuntimeError("validation failed")), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(dispatch.main(), 1)


if __name__ == "__main__":
    unittest.main()
