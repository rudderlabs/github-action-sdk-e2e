"""Subprocess-only GitHub API fixture. No network requests are allowed."""
import io
import hashlib
import zipfile
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
import urllib.request

selected = None
# Composite smoke tests run inside the action repository. Model a supported caller.
if os.environ.get("GITHUB_REPOSITORY") == "rudderlabs/github-action-sdk-e2e":
    os.environ["GITHUB_REPOSITORY"] = "rudderlabs/analytics-go"
    os.environ["GITHUB_EVENT_NAME"] = "workflow_dispatch"


def bundle():
    sdk, package = (("go", "github.com/rudderlabs/analytics-go/v4")
                    if selected["source_repository"] == "rudderlabs/analytics-go"
                    else ("node", "@rudderstack/rudder-sdk-node"))
    report = {"schema_version": 2, "pipeline_mode": "ephemeral", "sdk": sdk, "package": package,
              "request_id": selected["request_id"], "selection": "candidate",
              "repository": selected["source_repository"], "commit": selected["source_sha"],
              "central_run_id": 123, "central_run_attempt": 1, "version": "1.2.3",
              "artifact": {"source_tree": "b" * 40}, "delivery_passed": True,
              "resources_destroyed": True, "cleanup_verified": True}
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        entry = zipfile.ZipInfo("validation-result.json", (2026, 1, 1, 0, 0, 0))
        archive.writestr(entry, json.dumps(report))
    return sdk, data.getvalue()


class ArtifactOpener:
    def open(self, request, timeout):
        if request.full_url.endswith("/actions/artifacts/1/zip"):
            assert request.get_header("Authorization") == "Bearer fixture-token"
            raise HTTPError(request.full_url, 302, "redirect", {"Location": "https://fixture.invalid/bundle"}, None)
        assert request.full_url == "https://fixture.invalid/bundle"
        assert request.get_header("Authorization") is None
        return io.BytesIO(bundle()[1])



def urlopen(request, timeout):
    global selected
    if request.get_header("Authorization") != "Bearer fixture-token":
        raise AssertionError("unexpected fixture credential")
    endpoint = request.full_url
    root = "https://api.github.com/repos/rudderlabs/rudder-sdk-e2e-tests/actions/"
    if os.environ.get("SDK_E2E_FIXTURE_CALLS"):
        with Path(os.environ["SDK_E2E_FIXTURE_CALLS"]).open("a") as log:
            log.write(request.get_method() + " " + endpoint + "\n")
    conclusion = os.environ.get("SDK_E2E_FIXTURE_CONCLUSION", "success")
    if conclusion == "transport-error":
        raise URLError("fixture-token")
    if endpoint == root + "workflows/server-sdk-validation.yml/dispatches":
        if request.get_method() != "POST" or selected is not None:
            raise AssertionError("unexpected repeated dispatch")
        selected = json.loads(request.data)["inputs"]
        response = {"workflow_run_id": 123}
    elif endpoint == root + "runs/123" and selected:
        response = {"id": 123, "run_attempt": 1, "repository": {"full_name": "rudderlabs/rudder-sdk-e2e-tests"},
                    "event": "workflow_dispatch", "head_branch": "main",
                    "path": ".github/workflows/server-sdk-validation.yml",
                    "display_title": "sdk-e2e " + selected["request_id"],
                    "status": "completed", "conclusion": conclusion}
    elif endpoint == root + "runs/123/artifacts?per_page=100":
        sdk, data = bundle()
        response = {"total_count": 1, "artifacts": [{"id": 1, "name": f"sdk-validation-result-{sdk}-123-1",
                    "expired": False, "size_in_bytes": len(data), "digest": "sha256:" + hashlib.sha256(data).hexdigest()}]}
    else:
        raise AssertionError("unexpected fixture request")
    return io.BytesIO(json.dumps(response).encode())


urllib.request.urlopen = urlopen

urllib.request.build_opener = lambda *args: ArtifactOpener()
