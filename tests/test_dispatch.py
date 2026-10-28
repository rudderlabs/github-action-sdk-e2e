"""Dispatch contract tests. No real GitHub workflows are started."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
import zipfile

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

    def test_explicit_pr_from_trusted_non_pr_event_uses_merge_sha_not_event_sha(self):
        merge_sha = "b" * 40
        selected = dispatch.inputs(dict(ENVIRONMENT, GITHUB_EVENT_NAME="workflow_dispatch",
                                        SDK_E2E_PR_NUMBER="87", SDK_E2E_MERGE_SHA=merge_sha))
        self.assertEqual(selected["selection"], "auto")
        self.assertEqual(selected["source_repository"], ENVIRONMENT["GITHUB_REPOSITORY"])
        self.assertEqual(selected["source_pr"], "87")
        self.assertEqual(selected["source_sha"], merge_sha)
        self.assertEqual(selected["request_id"], f"analytics-go-42-2-{merge_sha}")
        self.assertNotEqual(selected["source_sha"], ENVIRONMENT["GITHUB_SHA"])

    def test_explicit_pr_matching_pull_request_event_is_allowed(self):
        selected = dispatch.inputs(dict(ENVIRONMENT, GITHUB_EVENT_NAME="pull_request",
                                        GITHUB_REF="refs/pull/87/merge", SDK_E2E_PR_NUMBER="87",
                                        SDK_E2E_MERGE_SHA=SHA))
        self.assertEqual(selected["source_pr"], "87")
        self.assertEqual(selected["source_sha"], SHA)

    def test_partial_or_malformed_explicit_candidate_is_rejected(self):
        for pr, sha in (("87", ""), ("", SHA), ("0", SHA), ("-87", SHA),
                        ("087", SHA), ("87\n", SHA), ("87", "not-a-sha"),
                        ("87", "B" * 40), ("87", SHA + "\n")):
            with self.subTest(pr=pr, sha=sha), self.assertRaises(ValueError):
                dispatch.inputs(dict(ENVIRONMENT, SDK_E2E_PR_NUMBER=pr, SDK_E2E_MERGE_SHA=sha))

    def test_explicit_pr_rejects_conflicting_pull_request_context(self):
        for pr, sha, ref in (("88", SHA, "refs/pull/87/merge"),
                             ("87", "b" * 40, "refs/pull/87/merge"),
                             ("87", SHA, "refs/heads/master")):
            with self.subTest(pr=pr, sha=sha, ref=ref), self.assertRaises(ValueError):
                dispatch.inputs(dict(ENVIRONMENT, GITHUB_EVENT_NAME="pull_request",
                                     GITHUB_REF=ref, SDK_E2E_PR_NUMBER=pr, SDK_E2E_MERGE_SHA=sha))

    def test_explicit_pr_rejects_conflicting_pull_request_target_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            event_path = Path(temporary) / "event.json"
            environment = dict(ENVIRONMENT, GITHUB_EVENT_NAME="pull_request_target",
                               GITHUB_REF="refs/heads/master", GITHUB_EVENT_PATH=str(event_path),
                               SDK_E2E_PR_NUMBER="87", SDK_E2E_MERGE_SHA="b" * 40)
            event_path.write_text(json.dumps({"number": 87}))
            selected = dispatch.inputs(environment)
            self.assertEqual(selected["source_pr"], "87")
            self.assertEqual(selected["source_sha"], "b" * 40)
            for payload in ({"number": 88}, {"number": "87"}, {"number": True}, {}, []):
                with self.subTest(payload=payload):
                    event_path.write_text(json.dumps(payload))
                    with self.assertRaisesRegex(ValueError, "conflicts with the pull_request_target"):
                        dispatch.inputs(environment)
            event_path.write_text("not JSON")
            with self.assertRaises(ValueError):
                dispatch.inputs(environment)
            with self.assertRaisesRegex(ValueError, "requires an event payload"):
                dispatch.inputs(dict(environment, GITHUB_EVENT_PATH=""))

    def test_explicit_candidate_does_not_trust_bot_identity_or_branch(self):
        selected = dispatch.inputs(dict(ENVIRONMENT, GITHUB_EVENT_NAME="workflow_dispatch",
                                        GITHUB_ACTOR="release-please[bot]",
                                        GITHUB_REF="refs/heads/release-please--branches--master",
                                        SDK_E2E_PR_NUMBER="87", SDK_E2E_MERGE_SHA="b" * 40))
        self.assertEqual(selected["selection"], "auto")
        self.assertEqual(selected["source_pr"], "87")

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


class ArtifactDownloadTests(unittest.TestCase):
    def test_api_redirect_does_not_forward_token_to_archive_host(self):
        redirected = HTTPError(dispatch.API + "/actions/artifacts/456/zip", 302, "Found",
                               {"Location": "https://signed.example/archive.zip"}, io.BytesIO(b""))
        with patch.object(dispatch, "build_opener") as build:
            build.return_value.open.side_effect = [redirected, contextlib.closing(io.BytesIO(b"archive"))]
            self.assertEqual(dispatch.download_artifact(456, "secret-token"), b"archive")
        api_request = build.return_value.open.call_args_list[0].args[0]
        archive_request = build.return_value.open.call_args_list[1].args[0]
        self.assertEqual(api_request.full_url, dispatch.API + "/actions/artifacts/456/zip")
        self.assertEqual(api_request.get_header("Authorization"), "Bearer secret-token")
        self.assertEqual(archive_request.full_url, "https://signed.example/archive.zip")
        self.assertIsNone(archive_request.get_header("Authorization"))

    def test_download_rejects_invalid_redirect_and_oversized_archive(self):
        for location in ("http://signed.example/archive", "https://user:pass@signed.example/archive", ""):
            with self.subTest(location=location), patch.object(dispatch, "build_opener") as build, \
                    self.assertRaisesRegex(RuntimeError, "invalid artifact download redirect"):
                build.return_value.open.side_effect = HTTPError(dispatch.API, 302, "Found",
                                                                 {"Location": location}, io.BytesIO(b""))
                dispatch.download_artifact(456, "token")
        with patch.object(dispatch, "build_opener") as build, self.assertRaisesRegex(RuntimeError, "oversized"):
            build.return_value.open.side_effect = [HTTPError(dispatch.API, 302, "Found",
                                                               {"Location": "https://signed.example/a"}, io.BytesIO(b"")),
                                                   contextlib.closing(io.BytesIO(b"x" * (dispatch.MAX_ARCHIVE_BYTES + 1)))]
            dispatch.download_artifact(456, "token")


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.selected = dispatch.inputs(ENVIRONMENT)
        self.run = {"id": 123, "run_attempt": 1, "repository": {"full_name": dispatch.REPOSITORY},
                    "event": "workflow_dispatch", "head_branch": "main",
                    "path": ".github/workflows/server-sdk-validation.yml",
                    "display_title": f"sdk-e2e {self.selected['request_id']}",
                    "status": "completed", "conclusion": "success"}
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name) / "output"
        self.summary = Path(temporary.name) / "summary"
        self.environment = {"GITHUB_OUTPUT": str(self.output), "GITHUB_STEP_SUMMARY": str(self.summary)}

    def set_report(self):
        self.selected = dispatch.inputs(dict(ENVIRONMENT, SDK_E2E_SUITE="full"))
        self.run["display_title"] = f"sdk-e2e {self.selected['request_id']}"
        self.report = {"schema_version": 1, "request_id": self.selected["request_id"],
                       "central_run_id": 123, "central_run_attempt": 1,
                       "repository": "rudderlabs/analytics-go", "commit": SHA,
                       "source_tree": "b" * 40, "module": dispatch.GO_MODULE,
                       "version": "4.2.0", "delivery_passed": True,
                       "restoration_verified": True, "lock_released": True}

    def archive_with_text(self, content):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as file:
            file.writestr(dispatch.REPORT_FILE, content)
        return buffer.getvalue()

    def evidence(self, *, report=None, artifact=None, archive=None):
        archive = archive if archive is not None else self.archive_with_text(
            json.dumps(report if report is not None else self.report))
        metadata = {"id": 456, "name": dispatch.REPORT_ARTIFACT.format(run_id=123, run_attempt=1), "expired": False,
                    "size_in_bytes": len(archive), "digest": "sha256:" + hashlib.sha256(archive).hexdigest()}
        if artifact:
            metadata.update(artifact)
        return {"total_count": 1, "artifacts": [metadata]}, archive

    def wait(self, responses, archive=None, **kwargs):
        with patch.object(dispatch, "request", side_effect=responses) as request, \
                patch.object(dispatch, "download_artifact", return_value=archive) as download, \
                patch.dict(os.environ, self.environment), contextlib.redirect_stdout(io.StringIO()):
            result = dispatch.dispatch_and_wait("test-token", self.selected, poll=0, **kwargs)
        return result, request, download

    def test_waits_for_exact_run_and_exports_outputs(self):
        self.set_report()
        listed, archive = self.evidence()
        url, request, download = self.wait([{"workflow_run_id": 123},
                                            dict(self.run, status="in_progress"), self.run, listed], archive)
        self.assertTrue(url.endswith("/123"))
        self.assertEqual(request.call_args_list[1].args[0], "/actions/runs/123")
        self.assertEqual(request.call_args_list[2].args[0], "/actions/runs/123")
        self.assertEqual(request.call_args_list[0].args[2],
                         {"ref": "main", "inputs": self.selected})
        self.assertEqual(request.call_args_list[3].args[0], "/actions/runs/123/artifacts?per_page=100")
        download.assert_called_once_with(456, "test-token")
        lines = dict(line.split("=", 1) for line in self.output.read_text().splitlines())
        self.assertEqual(lines["run-id"], "123")
        self.assertEqual(lines["run-url"], url)
        self.assertEqual(json.loads(lines["report"]), self.report)
        self.assertEqual(lines["validated-sha"], SHA)
        self.assertIn(SHA, self.summary.read_text())
        self.assertIn(url, self.summary.read_text())
        self.assertNotIn("test-token", self.summary.read_text())

    def test_pr_reliability_passes_exact_run_without_release_evidence(self):
        self.selected = dispatch.inputs(dict(ENVIRONMENT, GITHUB_EVENT_NAME="pull_request",
                                             GITHUB_REF="refs/pull/87/merge"))
        self.run["display_title"] = f"sdk-e2e {self.selected['request_id']}"
        url, request, download = self.wait([{"workflow_run_id": 123}, self.run])
        self.assertTrue(url.endswith("/123"))
        self.assertEqual(request.call_count, 2)
        download.assert_not_called()
        self.assertNotIn("report=", self.output.read_text())
        self.assertNotIn("validated-sha=", self.output.read_text())

    def test_non_go_caller_retains_exact_run_contract_without_go_evidence(self):
        self.selected = dispatch.inputs(dict(ENVIRONMENT, GITHUB_REPOSITORY="rudderlabs/rudder-sdk-node"))
        self.run["display_title"] = f"sdk-e2e {self.selected['request_id']}"
        url, request, download = self.wait([{"workflow_run_id": 123}, self.run])
        self.assertTrue(url.endswith("/123"))
        self.assertEqual(request.call_count, 2)
        download.assert_not_called()
        self.assertNotIn("validated-sha", self.output.read_text())

    def test_go_evidence_failures_emit_no_trusted_outputs(self):
        self.set_report()
        good, archive = self.evidence()
        cases = {
            "missing": ({"total_count": 0, "artifacts": []}, archive),
            "duplicate": ({"total_count": 2, "artifacts": good["artifacts"] * 2}, archive),
            "expired": self.evidence(artifact={"expired": True}),
            "digest": self.evidence(artifact={"digest": "sha256:" + "0" * 64}),
            "oversized": self.evidence(artifact={"size_in_bytes": dispatch.MAX_ARCHIVE_BYTES + 1}),
            "incomplete listing": ({"total_count": 101, "artifacts": good["artifacts"]}, archive),
            "malformed archive": self.evidence(archive=b"not a ZIP"),
            "unsupported schema": self.evidence(report=dict(self.report, schema_version=2)),
            "wrong attempt": self.evidence(report=dict(self.report, central_run_attempt=2)),
            "wrong request": self.evidence(report=dict(self.report, request_id="other")),
            "wrong run": self.evidence(report=dict(self.report, central_run_id=124)),
            "wrong source": self.evidence(report=dict(self.report, commit="c" * 40)),
            "wrong repository": self.evidence(report=dict(self.report, repository="other/repo")),
            "wrong artifact name": self.evidence(artifact={"name": "go-release-result-123-2"}),
            "missing field": self.evidence(report={key: value for key, value in self.report.items()
                                                     if key != "source_tree"}),
            "invalid JSON": self.evidence(archive=self.archive_with_text("not JSON")),
            "wrong tree": self.evidence(report=dict(self.report, source_tree="bad")),
            "wrong module": self.evidence(report=dict(self.report, module="other")),
            "wrong version": self.evidence(report=dict(self.report, version="latest")),
            "missing result": self.evidence(report={key: value for key, value in self.report.items()
                                                      if key != "restoration_verified"}),
            "failed delivery": self.evidence(report=dict(self.report, delivery_passed=False)),
            "failed restoration": self.evidence(report=dict(self.report, restoration_verified=False)),
            "failed lock release": self.evidence(report=dict(self.report, lock_released=False)),
        }
        for name, (listed, content) in cases.items():
            with self.subTest(name=name):
                self.output.unlink(missing_ok=True)
                with self.assertRaises(RuntimeError):
                    self.wait([{"workflow_run_id": 123}, self.run, listed], content)
                output = self.output.read_text()
                self.assertNotIn("report=", output)
                self.assertNotIn("validated-sha=", output)

    def test_incorrect_central_attempt_or_failed_download_never_exports_evidence(self):
        self.set_report()
        listed, archive = self.evidence()
        for run in (dict(self.run, run_attempt=2), dict(self.run, run_attempt=None)):
            with self.subTest(run=run), self.assertRaises(RuntimeError):
                self.wait([{"workflow_run_id": 123}, run, listed], archive)
            self.assertNotIn("report=", self.output.read_text())
        with patch.object(dispatch, "request", side_effect=[{"workflow_run_id": 123}, self.run, listed]), \
                patch.object(dispatch, "download_artifact", side_effect=RuntimeError("HTTP 410")), \
                patch.dict(os.environ, self.environment), contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaisesRegex(RuntimeError, "HTTP 410"):
            dispatch.dispatch_and_wait("test-token", self.selected, poll=0)
        self.assertNotIn("report=", self.output.read_text())

    def test_explicit_candidate_dispatch_keeps_exact_run_identity_checks(self):
        self.selected = dispatch.inputs(dict(ENVIRONMENT, GITHUB_EVENT_NAME="workflow_dispatch",
                                             SDK_E2E_PR_NUMBER="87", SDK_E2E_MERGE_SHA="b" * 40))
        self.run["display_title"] = f"sdk-e2e {self.selected['request_id']}"
        url, request, _ = self.wait([{"workflow_run_id": 123}, self.run])
        self.assertTrue(url.endswith("/123"))
        self.assertEqual(request.call_args_list[0].args[2]["inputs"], self.selected)
        with self.assertRaisesRegex(RuntimeError, "request title"):
            self.wait([{"workflow_run_id": 123}, dict(self.run, display_title=f"sdk-e2e analytics-go-42-2-{SHA}")])
        with self.assertRaisesRegex(RuntimeError, "concluded"):
            self.wait([{"workflow_run_id": 123}, dict(self.run, conclusion="failure")])

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
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, "does not match"):
                self.wait([{"workflow_run_id": 123}, dict(self.run, **{field: value})])

    def test_waits_for_github_to_expand_the_initial_workflow_title(self):
        pending = dict(self.run, status="queued", conclusion=None,
                       display_title="Server SDK validation")
        self.set_report()
        listed, archive = self.evidence()
        url, request, _ = self.wait([{"workflow_run_id": 123}, pending,
                                     dict(self.run, status="in_progress", conclusion=None), self.run,
                                     listed], archive)
        self.assertTrue(url.endswith("/123"))
        self.assertEqual(request.call_count, 5)

    def test_pending_title_still_requires_all_other_identity_fields(self):
        pending = dict(self.run, status="queued", conclusion=None,
                       display_title="Server SDK validation")
        for field, value in (("id", 124), ("repository", {"full_name": "other/repo"}),
                             ("event", "push"), ("head_branch", "other"), ("path", "other.yml")):
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, "identity"):
                self.wait([{"workflow_run_id": 123}, dict(pending, **{field: value})])

    def test_completed_or_unrelated_titles_never_pass(self):
        for status, title in (("completed", "Server SDK validation"),
                              ("queued", "sdk-e2e another-request"), ("queued", "")):
            with self.subTest(status=status, title=title), \
                    self.assertRaisesRegex(RuntimeError, "request title"):
                self.wait([{"workflow_run_id": 123}, dict(self.run, status=status, display_title=title)])

    def test_pending_title_has_a_thirty_second_limit(self):
        pending = dict(self.run, status="queued", conclusion=None,
                       display_title="Server SDK validation")
        with patch.object(dispatch.time, "monotonic", side_effect=[0, 0, 30]), \
                self.assertRaisesRegex(RuntimeError, "request title"):
            self.wait([{"workflow_run_id": 123}, pending])

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

    def test_explicit_candidate_input_failure_does_not_dispatch(self):
        environment = dict(ENVIRONMENT, GH_TOKEN="test-token", SDK_E2E_PR_NUMBER="87")
        with patch.dict(os.environ, environment, clear=True), \
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
