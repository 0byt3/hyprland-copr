"""Offline automation tests; credentials below are empty disposable fixtures."""

import copy
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import Mock, patch

from ci import copr
from hyprland_copr.config import load_config
from hyprland_copr.model import COMPANIONS, PipelineError, SOURCE_POLICY
from hyprland_copr.util import digest_json, read_json, save_json


class PublicationAutomationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        self.state = self.checkout / ".state"
        self.bundle = self.checkout / "dist/bundle"
        self.bundle.mkdir(parents=True)
        self.logs = self.checkout / "logs"
        self.logs.mkdir()
        self.credentials = self.root / "protected-copr"
        self.credentials.touch(mode=0o600)
        self.config = load_config()
        self.manifest = {
            "root": "hyprland",
            "source_policy": SOURCE_POLICY,
            "primaries": ["hyprland", *COMPANIONS],
        }
        self.events = []
        self.commands = []
        self.progress = Mock()
        self.publication = copr.Publication(
            self.config, self.state, self.state / "copr.json",
            self.command, self.logs, self.progress,
        )
        for context in (
            patch.object(copr, "ROOT", self.checkout),
            patch.dict(os.environ, {
                "COPR_CONFIG_FILE": str(self.credentials),
                "GITHUB_TOKEN": "offline-token-marker",
                "PYTHONPATH": "/untrusted",
            }),
            patch.object(copr, "verify_bundle", return_value=self.manifest),
        ):
            context.start()
            self.addCleanup(context.stop)

    def record(self, status, manifest=None):
        save_json(self.publication.state_path, {
            "status": status,
            "manifest_digest": digest_json(manifest or self.manifest),
            "destination": copr.destination(self.config),
        })

    def command(self, command, log, **options):
        self.commands.append((command, options))
        if "copr-build" in command:
            self.assertTrue(self.publication.pointer.exists())
            self.events.append("submit")
            self.record("submitted")
        elif "copr-validate" in command:
            self.events.append("validate")
            self.record("validated")
        elif "copr-promote" in command:
            self.events.append("promote")
            self.record("promoted")
        else:
            self.events.append("image")

    def test_full_success_is_ordered_and_clears_handoff_only_after_promotion(self):
        self.publication.run(self.bundle, self.manifest)
        self.assertEqual(self.events, ["image", "submit", "validate", "promote"])
        self.assertEqual(read_json(self.publication.state_path)["status"], "promoted")
        self.assertFalse(self.publication.pointer.exists())

    def test_authenticated_workers_are_restricted_and_have_no_engine_socket(self):
        self.publication.run(self.bundle, self.manifest)
        for command, options in (self.commands[1], self.commands[3]):
            self.assertEqual(command[:4], ["podman", "--remote=false", "run", "--rm"])
            self.assertIn("--userns=keep-id", command)
            self.assertIn("--read-only", command)
            self.assertIn("--cap-drop=ALL", command)
            self.assertIn(f"{self.credentials}:/run/copr-config:ro,z", command)
            self.assertIn(f"{self.bundle}:{self.bundle}:ro,z", command)
            self.assertIn(f"{self.state}:{self.state}:z", command)
            self.assertFalse(any(".sock" in str(value) for value in command))
            self.assertNotIn(f"{self.checkout}:/workspace:ro,z", command)
            self.assertNotIn("GITHUB_TOKEN", " ".join(str(value) for value in command))
        self.assertEqual(self.commands[3][0][-1], "--yes")

    def test_candidate_validation_has_no_inherited_tokens_or_credential_home(self):
        self.publication.run(self.bundle, self.manifest)
        command, options = self.commands[2]
        self.assertNotEqual(command[0], "podman")
        environment = options["env"]
        self.assertNotIn("COPR_CONFIG_FILE", environment)
        self.assertNotIn("GITHUB_TOKEN", environment)
        self.assertNotIn("PYTHONPATH", environment)
        self.assertNotEqual(environment["HOME"], str(Path.home()))
        self.assertNotIn(str(self.credentials), command)

    def test_public_configuration_roundtrips_without_changing_user_config(self):
        before = copy.deepcopy(self.config)
        serialized = copr.public_config(self.config, self.state)
        value = tomllib.loads(serialized)
        self.assertEqual(value["pipeline"]["state_directory"], str(self.state))
        self.assertEqual(value["copr"], before["copr"])
        self.assertEqual(self.config, before)

    def test_each_failure_prevents_later_phases_and_retains_exact_handoff(self):
        for failing, expected in (
            ("image", []),
            ("copr-build", ["image"]),
            ("copr-validate", ["image", "submit"]),
            ("copr-promote", ["image", "submit", "validate"]),
        ):
            with self.subTest(phase=failing):
                self.events.clear()
                self.publication.state_path.unlink(missing_ok=True)

                def fail(command, log, **options):
                    if failing in command or (failing == "image" and "build" in command):
                        raise subprocess.CalledProcessError(1, ["offline-worker"])
                    self.command(command, log, **options)

                self.publication.run_logged = fail
                with self.assertRaises(subprocess.CalledProcessError):
                    self.publication.run(self.bundle, self.manifest)
                self.assertEqual(self.events, expected)
                handoff = read_json(self.publication.pointer)
                self.assertEqual(handoff["manifest_digest"], digest_json(self.manifest))
                self.assertEqual(self.publication.pending(), self.bundle)

    def test_resumption_preserves_promotion_receipts_and_skips_earlier_phases(self):
        for status, expected in (
            ("building", ["image", "submit", "validate", "promote"]),
            ("submitted", ["image", "submit", "validate", "promote"]),
            ("validated", ["image", "promote"]),
            ("promoting", ["image", "promote"]),
            ("promoted", ["image", "promote"]),
        ):
            with self.subTest(status=status):
                self.events.clear()
                self.record(status)
                self.publication.run(self.bundle, self.manifest)
                self.assertEqual(self.events, expected)

    def test_missing_phase_state_or_receipt_never_advances(self):
        for status, command in ((None, "copr-build"), ("submitted", "copr-validate")):
            with self.subTest(status=status):
                self.publication.state_path.unlink(missing_ok=True)
                if status:
                    self.record(status)

                def no_receipt(arguments, log, **options):
                    if command not in arguments:
                        self.command(arguments, log, **options)

                self.publication.run_logged = no_receipt
                with self.assertRaisesRegex(PipelineError, "recovery state|receipt"):
                    self.publication.run(self.bundle, self.manifest)

    def test_partial_legacy_or_duplicate_primary_bundles_cannot_publish(self):
        for changed in (
            {"root": "lua"},
            {"source_policy": None},
            {"primaries": ["hyprland"]},
            {"primaries": ["hyprland", *COMPANIONS, "hyprland"]},
        ):
            with self.subTest(changed=changed), self.assertRaisesRegex(PipelineError, "complete"):
                self.publication.run(self.bundle, {**self.manifest, **changed})
        self.assertFalse(self.commands)

    def test_unknown_pending_state_fails_without_any_worker(self):
        self.record("unknown")
        with self.assertRaisesRegex(PipelineError, "Unsupported publication"):
            self.publication.run(self.bundle, self.manifest)
        self.assertFalse(self.commands)

    def test_unmanaged_pending_state_blocks_new_release(self):
        self.record("building")
        with self.assertRaisesRegex(PipelineError, "no automation handoff"):
            self.publication.pending()

    def test_pending_destination_or_digest_drift_blocks_resumption(self):
        def fail(*arguments, **options):
            raise PipelineError("offline stop")

        self.publication.run_logged = fail
        with self.assertRaises(PipelineError):
            self.publication.run(self.bundle, self.manifest)
        original = read_json(self.publication.pointer)
        for changed in (
            {"destination": ["another", "project", "fedora-44-x86_64"]},
            {"manifest_digest": "0" * 64},
            {"publication_state": "/elsewhere/state.json"},
        ):
            save_json(self.publication.pointer, {**original, **changed})
            with self.subTest(changed=changed), self.assertRaises(PipelineError):
                self.publication.pending()

    def test_credentials_must_be_protected_regular_owned_files_outside_mounts(self):
        self.credentials.chmod(0o644)
        with self.assertRaisesRegex(PipelineError, "mode 0600"):
            copr.check_credentials([self.checkout])
        self.credentials.chmod(0o600)
        with patch.object(copr.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaisesRegex(PipelineError, "user-owned"):
                copr.check_credentials([self.checkout])
        link = self.root / "credential-link"
        link.symlink_to(self.credentials)
        with patch.dict(os.environ, {"COPR_CONFIG_FILE": str(link)}):
            with self.assertRaisesRegex(PipelineError, "regular"):
                copr.check_credentials([self.checkout])
        with patch.dict(os.environ, {"COPR_CONFIG_FILE": str(self.checkout / "copr")}):
            with self.assertRaisesRegex(PipelineError, "outside"):
                copr.check_credentials([self.checkout], required=False)
        self.credentials.unlink()
        with self.assertRaisesRegex(PipelineError, "protected COPR"):
            copr.check_credentials([self.checkout])

    def test_policy_and_state_path_are_required_before_workers(self):
        for state_path, exclusive in (
            (self.root / "outside.json", True),
            (self.state / "copr.json", False),
        ):
            config = copy.deepcopy(self.config)
            config["copr"]["exclusive_writer"] = exclusive
            with self.subTest(state_path=state_path), self.assertRaises(PipelineError):
                copr.Publication(
                    config, self.state, state_path, self.command, self.logs, self.progress,
                )


if __name__ == "__main__":
    unittest.main()
