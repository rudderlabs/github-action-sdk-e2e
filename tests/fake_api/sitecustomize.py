"""Subprocess-only GitHub API fixture. No network requests are allowed."""
import io
import json
import os
from pathlib import Path
from urllib.error import URLError
import urllib.request

selected = None


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
        response = {"id": 123, "repository": {"full_name": "rudderlabs/rudder-sdk-e2e-tests"},
                    "event": "workflow_dispatch", "head_branch": "main",
                    "path": ".github/workflows/server-sdk-validation.yml",
                    "display_title": "sdk-e2e " + selected["request_id"],
                    "status": "completed", "conclusion": conclusion}
    else:
        raise AssertionError("unexpected fixture request")
    return io.BytesIO(json.dumps(response).encode())


urllib.request.urlopen = urlopen
