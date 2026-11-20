"""Test fix snapshot policy and its isolated Python runner interface."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError

from resolve_target import NoRedirect, github_commit_sha, resolve_target, write_outputs

SHA = "a" * 40
SCRIPT = Path(__file__).parents[1] / "resolve_target.py"


class ResolveTargetTests(unittest.TestCase):
    """Retain event compatibility without branch movement or output injection."""

    def setUp(self) -> None:
        """Create a workspace and sentinel-only GitHub event fixtures."""
        temporary = tempfile.TemporaryDirectory(prefix="fix-target-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.inputs = {
            "GITHUB_WORKSPACE": str(self.root),
            "GITHUB_REPOSITORY": "Example/target",
            "GITHUB_EVENT_NAME": "schedule",
            "GITHUB_SHA": SHA,
            "GITHUB_REF": "refs/heads/main",
            "GET_PR_REF": "feature",
            "GET_PR_REPO": "Fork/target",
            "GET_PR_SHA": SHA,
        }
        self.payload = {
            "pull_request": {
                "head": {"repo": {"full_name": "Fork/target"}, "ref": "feature", "sha": SHA}
            },
            "issue": {"number": 1, "pull_request": {}},
            "inputs": {"ref": "feature"},
        }
        self.lookup = Mock(return_value=SHA)

    def test_branch_and_sha_remain_separate(self) -> None:
        """Use actual PR heads, event snapshots, and one manual lookup."""
        for event in ["pull_request", "schedule", "workflow_dispatch"]:
            with self.subTest(event=event):
                self.lookup.reset_mock()
                outputs = resolve_target(
                    self.inputs | {"GITHUB_EVENT_NAME": event}, self.payload, self.lookup
                )
                self.assertEqual(outputs["ref"], "main" if event == "schedule" else "feature")
                self.assertEqual(outputs["sha"], SHA)
                self.assertEqual(self.lookup.call_count, int(event == "workflow_dispatch"))

    def test_repository_override_keeps_pull_request_head_branch_default(self) -> None:
        """An explicit repository does not erase the event's omitted branch value."""
        payload = self.payload | {
            "pull_request": {
                "head": {
                    "repo": {"full_name": "Example/target"},
                    "ref": "feature",
                    "sha": SHA,
                }
            }
        }
        outputs = resolve_target(
            self.inputs | {"GITHUB_EVENT_NAME": "pull_request", "INPUT_REPO": "Example/target"},
            payload,
            self.lookup,
        )
        self.assertEqual(
            (outputs["repo"], outputs["ref"], outputs["sha"]),
            ("Example/target", "feature", SHA),
        )
        self.lookup.assert_called_once_with("Example/target", "feature")

    def test_comment_override_cannot_bypass_approval(self) -> None:
        """Validate the exact fork identity even when all overrides are supplied."""
        inputs = self.inputs | {"GITHUB_EVENT_NAME": "issue_comment"}
        for overrides in [
            {},
            {"INPUT_SHA": "c" * 40},
            {"INPUT_SHA": SHA, "INPUT_REPO": "Other/target"},
            {"INPUT_SHA": SHA, "INPUT_REF": "other"},
            {"INPUT_SHA": SHA, "GET_PR_REPO": ""},
        ]:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                resolve_target(inputs | overrides, self.payload, self.lookup)
        outputs = resolve_target(inputs | {"INPUT_SHA": SHA}, self.payload, self.lookup)
        self.assertEqual(
            (outputs["repo"], outputs["ref"], outputs["sha"]), ("Fork/target", "feature", SHA)
        )
        self.lookup.assert_not_called()

    def test_refspecs_tags_shas_and_output_injection_are_rejected(self) -> None:
        """Invalid branch destinations and paths cannot reach network lookups."""
        for overrides in [
            {"INPUT_REF": "x\nrepo=evil"},
            {"INPUT_REF": "x:refs/heads/main"},
            {"INPUT_REF": "refs/tags/v1"},
            {"INPUT_REF": SHA},
            {"INPUT_REPO": "Owner/../target"},
            {"INPUT_CHECKOUT_PATH": "../outside"},
            {"INPUT_CHECKOUT_PATH": "/tmp/outside"},
        ]:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                resolve_target(self.inputs | overrides, self.payload, self.lookup)

    def test_existing_symlink_escape_is_rejected(self) -> None:
        """Do not follow source-controlled ancestors outside the workspace."""
        (self.root / "escape").symlink_to(self.root.parent)
        with self.assertRaises(ValueError):
            resolve_target(
                self.inputs | {"INPUT_CHECKOUT_PATH": "escape/source"}, self.payload, self.lookup
            )

    def test_snapshot_movement_is_rejected_for_every_event(self) -> None:
        """Neither manual resolution nor event metadata can silently replace approval."""
        for event in ["workflow_dispatch", "pull_request", "schedule"]:
            with self.subTest(event=event), self.assertRaises(ValueError):
                resolve_target(
                    self.inputs | {"GITHUB_EVENT_NAME": event, "INPUT_SHA": "c" * 40},
                    self.payload,
                    self.lookup,
                )

    def test_api_branch_names_are_not_silently_normalized(self) -> None:
        """A literal API branch beginning with refs is rejected, not retargeted."""
        inputs = self.inputs | {
            "GITHUB_EVENT_NAME": "issue_comment",
            "INPUT_SHA": SHA,
            "GET_PR_REF": "refs/heads/feature",
        }
        with self.assertRaises(ValueError):
            resolve_target(inputs, self.payload, self.lookup)

    def test_invalid_outputs_are_not_partially_appended(self) -> None:
        """Preserve earlier runner outputs when any new value is invalid."""
        output = self.root / "outputs"
        output.write_text("existing=value\n")
        with self.assertRaises(ValueError):
            write_outputs(output, {"sha": SHA, "ref": "feature\rspoof=true"})
        self.assertEqual(output.read_text(), "existing=value\n")

    def test_cli_does_not_require_node_or_import_target_configuration(self) -> None:
        """Run the isolated script with a poisoned Python path and no real credentials."""
        sentinel = self.root / "executed"
        (self.root / "sitecustomize.py").write_text(f"open({str(sentinel)!r}, 'w').close()\n")
        event = self.root / "event.json"
        event.write_text(json.dumps(self.payload))
        output = self.root / "outputs"
        environment = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.root),
            **self.inputs,
            "GITHUB_EVENT_PATH": str(event),
            "GITHUB_OUTPUT": str(output),
            "PYTHONPATH": str(self.root),
            "GITHUB_TOKEN": "TEST_SENTINEL",
        }
        result = subprocess.run(
            [sys.executable, "-I", str(SCRIPT)],
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(sentinel.exists())
        self.assertIn(f"sha={SHA}\n", output.read_text())
        self.assertIn("ref=main\n", output.read_text())

    def test_transport_is_get_only_and_denial_fails_closed(self) -> None:
        """Exercise API plumbing through mocks without using GitHub secrets."""
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({"object": {"sha": SHA}}).encode()
        with patch("resolve_target.build_opener") as opener:
            opener.return_value.open.return_value = response
            self.assertEqual(
                github_commit_sha(
                    "Example/target", "feature/test", {"GITHUB_TOKEN": "TEST_SENTINEL"}
                ),
                SHA,
            )
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.get_method(), "GET")
        self.assertIn("/git/ref/heads/feature/test", request.full_url)
        self.assertIsInstance(opener.call_args.args[0], NoRedirect)
        with patch("resolve_target.build_opener") as opener:
            opener.return_value.open.side_effect = HTTPError(
                request.full_url, 403, "denied", {}, None
            )
            with self.assertRaisesRegex(ValueError, "metadata lookup failed"):
                github_commit_sha("Example/target", "feature", {"GITHUB_TOKEN": "TEST_SENTINEL"})

    def test_tag_namespace_is_looked_up_as_a_branch(self) -> None:
        """A tags/v1 branch must not resolve to the v1 tag namespace."""
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({"object": {"sha": SHA}}).encode()
        with patch("resolve_target.build_opener") as opener:
            opener.return_value.open.return_value = response
            self.assertEqual(
                github_commit_sha("Example/target", "tags/v1", {"GITHUB_TOKEN": "TEST_SENTINEL"}),
                SHA,
            )
        request = opener.return_value.open.call_args.args[0]
        self.assertIn("/git/ref/heads/tags/v1", request.full_url)


if __name__ == "__main__":
    unittest.main()
