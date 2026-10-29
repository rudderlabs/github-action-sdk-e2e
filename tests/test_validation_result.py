"""Verify trusted run identity and opaque SDK bundles without network calls."""
import base64
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import dispatch
import validation_result as result

SHA512 = base64.b64encode(bytes(64)).decode()
H1 = "h1:" + base64.b64encode(bytes(32)).decode()
PUBLISHED_EVIDENCE = {
    "node": {"npm_integrity": "sha512-" + SHA512},
    "go": {"go_sum": H1, "go_mod_sum": H1, "checksum_database": "sum.golang.org"},
    "python": {"wheel_sha256": "c" * 64},
    "ruby": {"gem_sha256": "c" * 64}, "ruby-sync": {"gem_sha256": "c" * 64},
    "rust": {"crate_sha256": "c" * 64},
    "php": {"composer_dist_reference": "a" * 40, "composer_lock_sha256": "c" * 64},
    "dotnet": {"nuget_content_hash": SHA512}, "java": {"maven_jar_sha1": "c" * 40},
}


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.selected = dispatch.inputs({"GITHUB_REPOSITORY": "rudderlabs/analytics-go", "GITHUB_SHA": "a" * 40,
                                         "GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "1", "SDK_E2E_SUITE": "full"})
        self.run = {"id": 123, "run_attempt": 2, "status": "completed", "conclusion": "success"}
        self.report = {"schema_version": 1, "sdk": "go", "selection": "candidate",
                       "repository": self.selected["source_repository"], "commit": self.selected["source_sha"],
                       "request_id": self.selected["request_id"], "central_run_id": 123, "central_run_attempt": 2,
                       "package": "github.com/rudderlabs/analytics-go/v4", "version": "1.2.3",
                       "artifact": {"source_tree": "b" * 40}, "delivery_passed": True,
                       "restoration_verified": True, "lock_released": True}

    def package(self):
        data = b"exact tested native package"
        self.report["artifact"].update(package_file="candidate.tgz", package_sha256=hashlib.sha256(data).hexdigest(),
                                       package_integrity="sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode())
        return data

    def bundle(self, report=None, entries=None):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(result.REPORT_FILE, json.dumps(self.report if report is None else report))
            for name, content in entries or []:
                archive.writestr(name, content)
        data = buffer.getvalue()
        item = {"id": 456, "name": f"sdk-validation-result-{self.report['sdk']}-123-2", "expired": False,
                "size_in_bytes": len(data), "digest": "sha256:" + hashlib.sha256(data).hexdigest()}
        return {"total_count": 1, "artifacts": [item]}, data

    def verify(self, listed, data):
        with patch.object(result, "download", return_value=data) as download, \
                patch.dict(os.environ, RUNNER_TEMP=str(self.folder)):
            api = unittest.mock.Mock(return_value=listed)
            report, package = result.verified("secret-token", self.run, self.selected, api, dispatch.API)
        api.assert_called_once_with("/actions/runs/123/artifacts?per_page=100", "secret-token")
        download.assert_called_once_with(dispatch.API, 456, "secret-token")
        return report, package

    def test_common_source_contract_and_exact_package_bytes(self):
        self.assertEqual(self.verify(*self.bundle()), (self.report, ""))
        self.report.update(sdk="node", package="@rudderstack/rudder-sdk-node")
        self.selected["source_repository"] = self.report["repository"] = "rudderlabs/rudder-sdk-node"
        data = self.package()
        record, path = self.verify(*self.bundle(entries=[("candidate.tgz", data)]))
        self.assertEqual(record, self.report)
        self.assertEqual(Path(path).read_bytes(), data)
        self.assertEqual(Path(path).parent.stat().st_mode & 0o777, 0o700)

    def test_full_pr_requires_matching_pr_identity_and_cannot_authorize_a_release(self):
        self.selected["source_pr"] = "94"
        with self.assertRaises(RuntimeError):
            self.verify(*self.bundle())
        self.report["source_pr"] = "95"
        with self.assertRaises(RuntimeError):
            self.verify(*self.bundle())
        self.report["source_pr"] = "94"
        self.assertEqual(self.verify(*self.bundle()), (self.report, ""))
        del self.selected["source_pr"]
        with self.assertRaises(RuntimeError):
            self.verify(*self.bundle())

    def test_all_published_results_use_the_same_contract_and_exact_version(self):
        self.selected["package_version"] = "v1.2.3"
        self.report.update(selection="published")
        for repository, (sdk, package) in result.IDENTITIES.items():
            self.selected["source_repository"] = repository
            self.report.update(sdk=sdk, package=package, repository=repository, artifact=PUBLISHED_EVIDENCE[sdk])
            with self.subTest(sdk=sdk):
                self.assertEqual(self.verify(*self.bundle()), (self.report, ""))
        self.report["version"] = "1.2.4"
        with self.assertRaises(RuntimeError):
            self.verify(*self.bundle())

    def test_each_published_adapter_requires_its_recognized_integrity_fields(self):
        self.selected["package_version"] = "1.2.3"
        for repository, (sdk, package) in result.IDENTITIES.items():
            self.selected["source_repository"] = repository
            self.report.update(sdk=sdk, package=package, repository=repository, selection="published")
            expected = PUBLISHED_EVIDENCE[sdk]
            invalid = [{}, {"native_checksum": None}, {"unrelated": True}]
            for field in expected:
                invalid.append({key: value for key, value in expected.items() if key != field})
                invalid.extend(dict(expected, **{field: value}) for value in (None, True, "", "malformed", "g" * 64))
            for evidence in invalid:
                self.report["artifact"] = evidence
                with self.subTest(sdk=sdk, evidence=evidence), self.assertRaises(RuntimeError):
                    self.verify(*self.bundle())

    def test_rejects_missing_duplicate_incomplete_expired_or_wrong_attempt_artifacts(self):
        good, data = self.bundle()
        for change in ({"expired": True}, {"id": True}, {"id": -1}, {"size_in_bytes": 0},
                       {"size_in_bytes": result.MAX_ARCHIVE_BYTES + 1}, {"digest": "sha256:" + "0" * 64},
                       {"name": "sdk-validation-result-go-123-1"}, {"name": "unrelated-result-123-2"}):
            listed = copy.deepcopy(good)
            listed["artifacts"][0].update(change)
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                self.verify(listed, data)
        for listed in ({"total_count": 0, "artifacts": []}, {"total_count": 2, "artifacts": good["artifacts"] * 2},
                       {"total_count": 101, "artifacts": good["artifacts"]}):
            with self.subTest(listed=listed), self.assertRaises(RuntimeError):
                self.verify(listed, data)

    def test_identity_results_types_and_source_evidence_fail_closed(self):
        for key, value in (("schema_version", True), ("schema_version", 2), ("central_run_id", 124),
                           ("central_run_attempt", 1), ("request_id", "other"), ("sdk", "node"),
                           ("selection", "published"), ("repository", "other/repo"), ("commit", "c" * 40),
                           ("package", ""), ("version", "latest"), ("artifact", {}),
                           ("delivery_passed", False), ("restoration_verified", 1), ("lock_released", False)):
            record = dict(self.report, **{key: value})
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                self.verify(*self.bundle(report=record))
        for key in self.report:
            record = {name: value for name, value in self.report.items() if name != key}
            with self.subTest(missing=key), self.assertRaises(RuntimeError):
                self.verify(*self.bundle(report=record))

    def test_invalid_archives_and_failed_or_unidentified_runs_are_rejected(self):
        listed, _ = self.bundle()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(result.REPORT_FILE, "not JSON")
        for data in (b"not a zip", buffer.getvalue()):
            listed["artifacts"][0].update(size_in_bytes=len(data), digest="sha256:" + hashlib.sha256(data).hexdigest())
            with self.subTest(data=data[:10]), self.assertRaises(RuntimeError):
                self.verify(listed, data)
        for change in ({"conclusion": "failure"}, {"status": "in_progress"}, {"run_attempt": True}, {"run_attempt": 0}):
            self.run.update(id=123, run_attempt=2, status="completed", conclusion="success")
            self.run.update(change)
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                self.verify(*self.bundle())

    def test_uncompressed_report_limit_is_enforced(self):
        with patch.object(result, "MAX_REPORT_BYTES", 8), self.assertRaises(RuntimeError):
            self.verify(*self.bundle())

    def test_rejects_package_corruption_unsafe_paths_missing_and_unexpected_files(self):
        data = self.package()
        good = copy.deepcopy(self.report)
        for name in ("../candidate.tgz", "/tmp/candidate.tgz", "validation-result.json", "VALIDATION-RESULT.JSON"):
            self.report = copy.deepcopy(good)
            self.report["artifact"]["package_file"] = name
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                self.verify(*self.bundle())
        self.report = good
        for entries in ([], [("candidate.tgz", b"changed")], [("candidate.tgz", data), ("extra", b"unexpected")],
                        [("candidate.tgz", data), ("candidate.tgz", data)]):
            with self.subTest(entries=entries), self.assertRaises(RuntimeError):
                self.verify(*self.bundle(entries=entries))
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_dispatch_exports_only_verified_results_and_retains_run_link_on_failure(self):
        self.run.update(repository={"full_name": dispatch.REPOSITORY}, event="workflow_dispatch", head_branch="main",
                        path=".github/workflows/server-sdk-validation.yml", display_title="sdk-e2e " + self.selected["request_id"])
        output = self.folder / "outputs"
        for failure in (False, True):
            output.unlink(missing_ok=True)
            value = RuntimeError("invalid result") if failure else (self.report, "/verified/candidate.tgz")
            with patch.dict(os.environ, GITHUB_OUTPUT=str(output)), contextlib.redirect_stdout(io.StringIO()), \
                    patch.object(dispatch, "request", side_effect=[{"workflow_run_id": 123}, self.run]), \
                    patch.object(result, "verified", side_effect=[value]):
                if failure:
                    with self.assertRaises(RuntimeError):
                        dispatch.dispatch_and_wait("token", self.selected)
                else:
                    dispatch.dispatch_and_wait("token", self.selected)
            text = output.read_text()
            self.assertIn("run-id=123", text)
            self.assertEqual("validated-sha=" in text, not failure)
            self.assertEqual("report=" in text, not failure)
            self.assertEqual("package-file=" in text, not failure)


class DownloadTests(unittest.TestCase):
    def test_download_size_limit_is_enforced(self):
        redirect = HTTPError(dispatch.API, 302, "Found", {"Location": "https://signed.example/archive"}, io.BytesIO())
        with patch.object(result, "MAX_ARCHIVE_BYTES", 4), patch.object(result, "build_opener") as build:
            build.return_value.open.side_effect = [redirect, contextlib.closing(io.BytesIO(b"12345"))]
            with self.assertRaisesRegex(RuntimeError, "oversized"):
                result.download(dispatch.API, 456, "secret-token")

    def test_authenticated_api_redirect_never_forwards_the_token(self):
        redirect = HTTPError(dispatch.API, 302, "Found", {"Location": "https://signed.example/archive"}, io.BytesIO())
        with patch.object(result, "build_opener") as build:
            build.return_value.open.side_effect = [redirect, contextlib.closing(io.BytesIO(b"archive"))]
            self.assertEqual(result.download(dispatch.API, 456, "secret-token"), b"archive")
        requests = build.return_value.open.call_args_list
        self.assertEqual(requests[0].args[0].get_header("Authorization"), "Bearer secret-token")
        self.assertIsNone(requests[1].args[0].get_header("Authorization"))

    def test_download_rejects_unsafe_redirects_and_sanitizes_transport_errors(self):
        for location in ("http://example/archive", "https://user:pass@example/archive", "", "https://example/a#fragment"):
            with self.subTest(location=location), patch.object(result, "build_opener") as build, self.assertRaises(RuntimeError):
                build.return_value.open.side_effect = HTTPError(dispatch.API, 302, "Found", {"Location": location}, io.BytesIO())
                result.download(dispatch.API, 456, "secret-token")
        with patch.object(result, "build_opener") as build:
            build.return_value.open.side_effect = URLError("secret-token")
            with self.assertRaisesRegex(RuntimeError, "^artifact download failed: transport or redirect error$"):
                result.download(dispatch.API, 456, "secret-token")
