"""Run the shipped CLI with a deterministic network adapter."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CLITests(unittest.TestCase):
    def test_success_failure_and_transport_failure_have_safe_cli_results(self):
        for conclusion, status, calls in (("success", 0, 3), ("failure", 1, 2), ("transport-error", 1, 1)):
            with self.subTest(conclusion=conclusion), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                environment = dict(os.environ, PYTHONPATH=str(ROOT / "tests/fake_api"), GH_TOKEN="fixture-token",
                                   SDK_E2E_FIXTURE_CONCLUSION=conclusion, SDK_E2E_FIXTURE_CALLS=str(root / "calls"),
                                   GITHUB_REPOSITORY="rudderlabs/rudder-sdk-node", GITHUB_SHA="a" * 40,
                                   GITHUB_RUN_ID="1", GITHUB_RUN_ATTEMPT="1", GITHUB_EVENT_NAME="release",
                                   GITHUB_OUTPUT=str(root / "outputs"), GITHUB_STEP_SUMMARY=str(root / "summary"))
                result = subprocess.run([sys.executable, str(ROOT / "scripts/dispatch.py")], env=environment,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertEqual(len((root / "calls").read_text().splitlines()), calls)
                self.assertNotIn("fixture-token", result.stdout + result.stderr)
                if calls >= 2:
                    self.assertIn("run-id=123", (root / "outputs").read_text())
                    self.assertIn("/actions/runs/123", (root / "summary").read_text())
                else:
                    self.assertFalse((root / "outputs").exists())
