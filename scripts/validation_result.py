"""Authenticate the shared SDK validation bundle without executing its contents."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile

REPORT_FILE = "validation-result.json"
MAX_PACKAGE_BYTES = 32 * 1024 * 1024
MAX_REPORT_BYTES = 1024 * 1024
MAX_ARCHIVE_BYTES = MAX_PACKAGE_BYTES + MAX_REPORT_BYTES
# Identity data only. Native installation remains in the central adapters.
IDENTITIES = {
    "rudderlabs/analytics-go": ("go", "github.com/rudderlabs/analytics-go/v4"),
    "rudderlabs/rudder-sdk-node": ("node", "@rudderstack/rudder-sdk-node"),
    "rudderlabs/rudder-sdk-python": ("python", "rudder-sdk-python"),
    "rudderlabs/rudder-sdk-ruby": ("ruby", "rudder-sdk-ruby"),
    "rudderlabs/rudder-sdk-ruby-sync": ("ruby-sync", "rudder_analytics_sync"),
    "rudderlabs/rudder-php-sdk": ("php", "rudderstack/rudder-php-sdk"),
    "rudderlabs/rudder-sdk-rust": ("rust", "rudderanalytics"),
    "rudderlabs/rudder-sdk-.net": ("dotnet", "RudderAnalytics"),
    "rudderlabs/rudder-sdk-java": ("java", "com.rudderstack.sdk.java.analytics:analytics"),
}
PUBLISHED_EVIDENCE = {
    "node": {"npm_integrity": "sri512"},
    "go": {"go_sum": "h1", "go_mod_sum": "h1", "checksum_database": "sumdb"},
    "python": {"wheel_sha256": "sha256"},
    "ruby": {"gem_sha256": "sha256"},
    "ruby-sync": {"gem_sha256": "sha256"},
    "rust": {"crate_sha256": "sha256"},
    "php": {"composer_dist_reference": "commit", "composer_lock_sha256": "sha256"},
    "dotnet": {"nuget_content_hash": "sha512"},
    "java": {"maven_jar_sha1": "sha1"},
}


def valid_integrity(value, kind, commit):
    if not isinstance(value, str):
        return False
    if kind in ("sha1", "sha256"):
        return bool(re.fullmatch(r"[0-9a-f]{" + ("40" if kind == "sha1" else "64") + "}", value))
    if kind == "commit":
        return value == commit
    if kind == "sumdb":
        return value == "sum.golang.org"
    prefix, length = {"h1": ("h1:", 32), "sri512": ("sha512-", 64), "sha512": ("", 64)}[kind]
    if not value.startswith(prefix):
        return False
    encoded = value[len(prefix):]
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except ValueError:
        return False
    return len(decoded) == length and base64.b64encode(decoded).decode() == encoded


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def download(api, artifact_id, token):
    headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
               "X-GitHub-Api-Version": "2026-03-10"}
    opener = build_opener(NoRedirect)
    try:
        try:
            with opener.open(Request(f"{api}/actions/artifacts/{artifact_id}/zip", headers=headers), timeout=30):
                raise RuntimeError("artifact download did not redirect")
        except HTTPError as redirect:
            try:
                if redirect.code != 302:
                    raise RuntimeError(f"artifact API request failed: HTTP {redirect.code}") from None
                location = redirect.headers.get("Location", "")
            finally:
                redirect.close()
        target = urlsplit(location)
        if target.scheme != "https" or not target.netloc or target.username or target.password or target.fragment:
            raise RuntimeError("invalid artifact download redirect")
        # Follow the API's signed URL without forwarding the GitHub credential.
        with opener.open(Request(location), timeout=30) as response:
            content = response.read(MAX_ARCHIVE_BYTES + 1)
            if len(content) > MAX_ARCHIVE_BYTES:
                raise RuntimeError("SDK validation artifact is oversized")
            return content
    except HTTPError as error:
        code = error.code
        error.close()
        raise RuntimeError(f"artifact download failed: HTTP {code}") from None
    except (URLError, TimeoutError, ConnectionError, ValueError):
        raise RuntimeError("artifact download failed: transport or redirect error") from None


def read_entry(files, name, limit):
    if files.getinfo(name).file_size > limit:
        raise ValueError("artifact entry is oversized")
    with files.open(name) as source:
        content = source.read(limit + 1)
    if len(content) > limit:
        raise ValueError("artifact entry is oversized")
    return content


def identity(report, run, selected, sdk):
    mode = "published" if selected.get("package_version") else "candidate"
    expected_sdk, package = IDENTITIES[selected["source_repository"]]
    if sdk != expected_sdk:
        raise ValueError("SDK artifact name does not match the caller repository")
    expected = {"schema_version": 2, "pipeline_mode": "ephemeral", "request_id": selected["request_id"], "sdk": sdk,
                "package": package,
                "selection": mode, "repository": selected["source_repository"], "commit": selected["source_sha"],
                "central_run_id": run["id"], "central_run_attempt": run["run_attempt"]}
    if selected.get("source_pr"):
        expected["source_pr"] = selected["source_pr"]
    elif isinstance(report, dict) and report.get("source_pr"):
        raise ValueError("PR evidence cannot authorize non-PR validation")
    if (type(report) is not dict or any(type(report.get(key)) is not type(value) or report[key] != value
                                      for key, value in expected.items())
            or any(report.get(key) is not True for key in
                   ("delivery_passed", "resources_destroyed", "cleanup_verified"))):
        raise ValueError("SDK result identity or required results do not match")
    if not isinstance(report.get("package"), str) or not report["package"]:
        raise ValueError("missing SDK package identity")
    version = report.get("version")
    artifact = report.get("artifact")
    if (not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][a-zA-Z0-9.-]+)?", version)
            or type(artifact) is not dict):
        raise ValueError("invalid SDK version or artifact evidence")
    if mode == "published":
        if (version != selected["package_version"].removeprefix("v")
                or not all(valid_integrity(artifact.get(field), kind, selected["source_sha"])
                           for field, kind in PUBLISHED_EVIDENCE[sdk].items())):
            raise ValueError("published version or integrity mismatch")
    elif not isinstance(artifact.get("source_tree"), str) or not re.fullmatch(r"[0-9a-f]{40}", artifact["source_tree"]):
        raise ValueError("missing candidate source tree")


def verified(token, run, selected, request, api):
    """Trust only the exact successful run/attempt and its authenticated bundle."""
    if selected.get("suite") != "full":
        raise ValueError("SDK results require full validation")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise RuntimeError("SDK results require a successful central run")
    run_id, attempt = run["id"], run.get("run_attempt")
    if type(attempt) is not int or attempt <= 0:
        raise RuntimeError("invalid central run attempt")
    listed = request(f"/actions/runs/{run_id}/artifacts?per_page=100", token)
    if (type(listed) is not dict or type(listed.get("total_count")) is not int
            or type(listed.get("artifacts")) is not list or listed["total_count"] != len(listed["artifacts"])):
        raise RuntimeError("SDK validation artifact listing is incomplete")
    pattern = re.compile(r"sdk-validation-result-([a-z][a-z0-9-]*)-" + str(run_id) + "-" + str(attempt))
    matches = [(item, pattern.fullmatch(item.get("name", ""))) for item in listed["artifacts"]
               if isinstance(item, dict) and isinstance(item.get("name"), str)]
    matches = [(item, match) for item, match in matches if match]
    if len(matches) != 1:
        raise RuntimeError("expected exactly one shared SDK validation artifact")
    item, match = matches[0]
    digest = item.get("digest")
    if (type(item.get("id")) is not int or item["id"] <= 0 or item.get("expired") is not False
            or type(item.get("size_in_bytes")) is not int or not 0 < item["size_in_bytes"] <= MAX_ARCHIVE_BYTES
            or not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)):
        raise RuntimeError("invalid, expired or oversized SDK validation artifact metadata")
    content = download(api, item["id"], token)
    if len(content) > MAX_ARCHIVE_BYTES or hashlib.sha256(content).hexdigest() != digest.removeprefix("sha256:"):
        raise RuntimeError("SDK validation artifact digest mismatch")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as files:
            names = files.namelist()
            if names.count(REPORT_FILE) != 1:
                raise ValueError("expected one validation result")
            report = json.loads(read_entry(files, REPORT_FILE, MAX_REPORT_BYTES))
            identity(report, run, selected, match[1])
            artifact = report["artifact"]
            package_fields = ("package_file", "package_sha256", "package_integrity")
            package = None
            if any(key in artifact for key in package_fields):
                name, checksum, integrity = (artifact.get(key) for key in package_fields)
                if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name)
                        or name.casefold() == REPORT_FILE
                        or not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum)
                        or not isinstance(integrity, str)):
                    raise ValueError("invalid tested package evidence")
                package = read_entry(files, name, MAX_PACKAGE_BYTES)
                actual_integrity = "sha512-" + base64.b64encode(hashlib.sha512(package).digest()).decode()
                if hashlib.sha256(package).hexdigest() != checksum or integrity != actual_integrity:
                    raise ValueError("tested package checksum mismatch")
            expected_names = [REPORT_FILE] + ([artifact["package_file"]] if package is not None else [])
            if sorted(names) != sorted(expected_names):
                raise ValueError("unexpected SDK validation archive contents")
    except (ValueError, KeyError, TypeError, OSError, RuntimeError, zipfile.BadZipFile, NotImplementedError):
        raise RuntimeError("malformed or mismatched SDK validation artifact") from None
    # Retain verified opaque bytes. Never extract or execute the native package.
    package_path = ""
    if package is not None:
        folder = Path(tempfile.mkdtemp(prefix="sdk-tested-package-", dir=os.environ.get("RUNNER_TEMP")))
        path = folder / artifact["package_file"]
        path.write_bytes(package)
        package_path = str(path)
    return report, package_path
