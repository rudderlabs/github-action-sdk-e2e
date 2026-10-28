#!/usr/bin/env python3
"""Dispatch the server SDK battery and propagate the exact run's conclusion."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
import zipfile

REPOSITORY = "rudderlabs/rudder-sdk-e2e-tests"
WORKFLOW = "server-sdk-validation.yml"
API = f"https://api.github.com/repos/{REPOSITORY}"
REPORT_ARTIFACT = "go-release-result-{run_id}-{run_attempt}"
REPORT_FILE = "release-result.json"
GO_MODULE = "github.com/rudderlabs/analytics-go/v4"
MAX_ARCHIVE_BYTES = 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def request(path, token, payload=None):
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
               "X-GitHub-Api-Version": "2026-03-10", "Content-Type": "application/json"}
    try:
        with urlopen(Request(API + path, body, headers), timeout=30) as response:
            content = response.read()
            return json.loads(content) if content else {}
    except HTTPError as error:
        # Never print an API response that could include credentials or inputs.
        raise RuntimeError(f"GitHub API request failed: HTTP {error.code}") from error


def download_artifact(artifact_id, token):
    headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
               "X-GitHub-Api-Version": "2026-03-10"}
    opener = build_opener(NoRedirect)
    path = f"/actions/artifacts/{artifact_id}/zip"
    try:
        try:
            opener.open(Request(API + path, headers=headers), timeout=30)
        except HTTPError as redirect:
            try:
                if redirect.code != 302:
                    raise RuntimeError(f"GitHub API request failed: HTTP {redirect.code}") from redirect
                location = redirect.headers.get("Location", "")
            finally:
                redirect.close()
            target = urlsplit(location)
            if target.scheme != "https" or not target.netloc or target.username or target.password or target.fragment:
                raise RuntimeError("invalid artifact download redirect")
            # GitHub's short-lived archive URL is outside the API; never send the App token there.
            with opener.open(Request(location), timeout=30) as response:
                content = response.read(MAX_ARCHIVE_BYTES + 1)
                if len(content) > MAX_ARCHIVE_BYTES:
                    raise RuntimeError("candidate report artifact is oversized")
                return content
        raise RuntimeError("artifact download did not redirect")
    except HTTPError as error:
        raise RuntimeError(f"artifact download failed: HTTP {error.code}") from error


def verified_report(token, run, selected):
    run_id = run["id"]
    attempt = run.get("run_attempt")
    if type(attempt) is not int or attempt <= 0:
        raise RuntimeError("invalid central run attempt for candidate report")
    name = REPORT_ARTIFACT.format(run_id=run_id, run_attempt=attempt)
    listed = request(f"/actions/runs/{run_id}/artifacts?per_page=100", token)
    if (type(listed) is not dict or type(listed.get("total_count")) is not int
            or type(listed.get("artifacts")) is not list
            or listed["total_count"] != len(listed["artifacts"])):
        raise RuntimeError("candidate report artifact listing is incomplete")
    matches = [artifact for artifact in listed["artifacts"]
               if type(artifact) is dict and artifact.get("name") == name]
    if len(matches) != 1:
        raise RuntimeError("expected exactly one candidate report artifact on the validated run")
    artifact = matches[0]
    digest = artifact.get("digest")
    if (type(artifact.get("id")) is not int or artifact["id"] <= 0
            or artifact.get("expired") is not False
            or type(artifact.get("size_in_bytes")) is not int
            or not 0 < artifact["size_in_bytes"] <= MAX_ARCHIVE_BYTES
            or type(digest) is not str or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)):
        raise RuntimeError("invalid, expired or oversized candidate report artifact metadata")
    archive = download_artifact(artifact["id"], token)
    if hashlib.sha256(archive).hexdigest() != digest.removeprefix("sha256:"):
        raise RuntimeError("candidate report artifact digest mismatch")
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as files:
            if files.namelist() != [REPORT_FILE] or files.getinfo(REPORT_FILE).file_size > MAX_ARCHIVE_BYTES:
                raise ValueError("invalid report archive contents")
            with files.open(REPORT_FILE) as data:
                content = data.read(MAX_ARCHIVE_BYTES + 1)
                if len(content) > MAX_ARCHIVE_BYTES:
                    raise ValueError("oversized report JSON")
                report = json.loads(content)
    except (ValueError, OSError, zipfile.BadZipFile, RuntimeError) as error:
        raise RuntimeError("malformed candidate report artifact") from error
    if type(report) is not dict or type(report.get("schema_version")) is not int or report["schema_version"] != 1:
        raise RuntimeError("unsupported candidate report schema")
    expected = {"request_id": selected["request_id"], "central_run_id": run_id,
                "central_run_attempt": attempt, "repository": selected["source_repository"],
                "commit": selected["source_sha"], "module": GO_MODULE}
    if (any(type(report.get(key)) is not type(value) or report[key] != value
            for key, value in expected.items())
            or type(report.get("source_tree")) is not str
            or not re.fullmatch(r"[0-9a-f]{40}", report["source_tree"])
            or type(report.get("version")) is not str
            or not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][a-zA-Z0-9.-]+)?", report["version"])
            or any(report.get(key) is not True for key in
                   ("delivery_passed", "restoration_verified", "lock_released"))):
        raise RuntimeError("candidate report identity or required results do not match this run")
    return report


def inputs(environment):
    repository = environment["GITHUB_REPOSITORY"]
    sha = environment["GITHUB_SHA"]
    suite = environment.get("SDK_E2E_SUITE", "reliability")
    # The central runner owns the SDK allowlist and candidate authorization.
    if not re.fullmatch(r"rudderlabs/[A-Za-z0-9_.-]+", repository):
        raise ValueError("the caller must be a rudderlabs repository")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("GITHUB_SHA must be a full lowercase commit SHA")
    if suite not in ("reliability", "full"):
        raise ValueError("SDK_E2E_SUITE must be reliability or full")
    run_id, attempt = environment["GITHUB_RUN_ID"], environment["GITHUB_RUN_ATTEMPT"]
    if not run_id.isdigit() or not attempt.isdigit():
        raise ValueError("invalid caller run identity")
    correlation = f"{repository.split('/')[1]}-{run_id}-{attempt}-{sha}"
    selected = {"selection": "auto", "source_repository": repository, "source_sha": sha,
                "suite": suite, "request_id": correlation}
    if environment.get("GITHUB_EVENT_NAME") == "pull_request":
        match = re.fullmatch(r"refs/pull/([1-9][0-9]*)/merge", environment.get("GITHUB_REF", ""))
        if not match:
            raise ValueError("pull_request validation requires a PR merge ref")
        selected["source_pr"] = match[1]
    return selected


def validate_run(run, run_id, correlation, allow_pending_title=False):
    if (run.get("id") != run_id or run.get("repository", {}).get("full_name") != REPOSITORY
            or run.get("event") != "workflow_dispatch" or run.get("head_branch") != "main"
            or run.get("path") != f".github/workflows/{WORKFLOW}"):
        raise RuntimeError("central run identity does not match this dispatch")
    if run.get("display_title") != f"sdk-e2e {correlation}":
        # GitHub initially returns the workflow name before expanding run-name.
        if (allow_pending_title and run.get("display_title") == "Server SDK validation"
                and run.get("status") in ("queued", "in_progress", "waiting", "pending", "requested")):
            return False
        raise RuntimeError("central run request title does not match this dispatch")
    return True


def dispatch_and_wait(token, selected, timeout=3000, poll=15):
    started = time.monotonic()
    dispatched = request(f"/actions/workflows/{WORKFLOW}/dispatches", token,
                         {"ref": "main", "inputs": selected})
    run_id = dispatched.get("workflow_run_id")
    if type(run_id) is not int or run_id <= 0:
        raise RuntimeError("dispatch did not return a workflow_run_id; refusing to guess a matching run")
    url = f"https://github.com/{REPOSITORY}/actions/runs/{run_id}"
    print(f"Server SDK validation: {url}", flush=True)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write(f"run-id={run_id}\nrun-url={url}\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
            summary.write(f"[Server SDK validation]({url})\n\nCommit: `{selected['source_sha']}`\n")
    while time.monotonic() - started < timeout:
        run = request(f"/actions/runs/{run_id}", token)
        verified = validate_run(run, run_id, selected["request_id"],
                                allow_pending_title=time.monotonic() - started < 30)
        if verified and run.get("status") == "completed":
            if run.get("conclusion") != "success":
                raise RuntimeError(f"server SDK validation concluded {run.get('conclusion')}; see {url}")
            if (selected["source_repository"] == "rudderlabs/analytics-go"
                    and selected["suite"] == "full" and not selected.get("source_pr")):
                report = verified_report(token, run, selected)
                if os.environ.get("GITHUB_OUTPUT"):
                    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
                        output.write(f"report={json.dumps(report, separators=(',', ':'))}\n")
                        output.write(f"validated-sha={report['commit']}\n")
            return url
        time.sleep(min(poll, max(0, timeout - (time.monotonic() - started))))
    raise RuntimeError(f"timed out waiting for server SDK validation; central run may still be active: {url}")


def main():
    try:
        token = os.environ.get("GH_TOKEN")
        if not token:
            raise ValueError("the token input is required for dispatch and polling")
        timeout = os.environ.get("SDK_E2E_TIMEOUT_SECONDS", "3000")
        if not re.fullmatch(r"[1-9][0-9]{0,3}", timeout) or int(timeout) > 3000:
            raise ValueError("timeout-seconds must be an integer from 1 to 3000")
        dispatch_and_wait(token, inputs(os.environ), timeout=int(timeout))
        return 0
    except (ValueError, KeyError, OSError, RuntimeError) as error:
        print(f"Server SDK validation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
