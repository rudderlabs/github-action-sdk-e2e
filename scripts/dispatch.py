#!/usr/bin/env python3
"""Dispatch the server SDK battery and propagate the exact run's conclusion."""
from __future__ import annotations

import json
import os
import re
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

REPOSITORY = "rudderlabs/rudder-sdk-e2e-tests"
WORKFLOW = "server-sdk-validation.yml"
API = f"https://api.github.com/repos/{REPOSITORY}"


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


def inputs(environment):
    repository = environment["GITHUB_REPOSITORY"]
    version = environment.get("SDK_E2E_PACKAGE_VERSION", "")
    override = environment.get("SDK_E2E_SOURCE_SHA", "")
    suite = environment.get("SDK_E2E_SUITE", "reliability")
    # The central runner owns the SDK allowlist and candidate authorization.
    if not re.fullmatch(r"rudderlabs/[A-Za-z0-9_.-]+", repository):
        raise ValueError("the caller must be a rudderlabs repository")
    if version:
        if repository != "rudderlabs/rudder-sdk-node" or environment.get("GITHUB_EVENT_NAME") == "pull_request":
            raise ValueError("published package validation requires a non-PR Node caller")
        if not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?", version):
            raise ValueError("package-version must be an exact npm semantic version")
        if not re.fullmatch(r"[0-9a-f]{40}", override):
            raise ValueError("published package validation requires a full lowercase source-sha")
        sha = override
    else:
        if override:
            raise ValueError("source-sha override requires package-version")
        sha = environment["GITHUB_SHA"]
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("GITHUB_SHA must be a full lowercase commit SHA")
    if suite not in ("reliability", "full"):
        raise ValueError("SDK_E2E_SUITE must be reliability or full")
    run_id, attempt = environment["GITHUB_RUN_ID"], environment["GITHUB_RUN_ATTEMPT"]
    if not run_id.isdigit() or not attempt.isdigit():
        raise ValueError("invalid caller run identity")
    correlation = f"{repository.split('/')[1]}-{run_id}-{attempt}-{sha}"
    if version:
        correlation += f"-npm-{version}"
    selected = {"selection": "auto", "source_repository": repository, "source_sha": sha,
                "suite": suite, "request_id": correlation}
    if version:
        selected["package_version"] = version
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
