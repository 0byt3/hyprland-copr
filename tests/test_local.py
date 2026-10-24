import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from ci import release
from hyprland_copr import cli
from hyprland_copr.config import ROOT, load_config
from hyprland_copr.model import PipelineError
from hyprland_copr.util import lock


class LocalAutomationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = load_config()
        self.config["pipeline"]["state_directory"] = ".state"
        self.config["copr"]["publish"] = False
        for context in (
            patch.object(release, "ROOT", self.root),
            patch.object(release, "load_config", return_value=self.config),
            patch.object(release.os, "getuid", return_value=1000),
        ):
            context.start()
            self.addCleanup(context.stop)

    def arguments(self, *extra):
        return release.parser().parse_args(["--output", "dist/run", *extra])

    def result(self, directory="dist/run"):
        return json.loads((self.root / directory / "result.json").read_text())

    def test_checks_precede_build_and_disabled_publication_is_not_invoked(self):
        with (
            patch.object(release, "run_logged") as run,
            patch.object(release, "verify_bundle") as verify,
        ):
            self.assertEqual(release.execute(self.arguments()), 0)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0][:2], ["docker", "build"])
        self.assertEqual(commands[1][:3], ["docker", "run", "--rm"])
        self.assertIn("--network=none", commands[1])
        self.assertIn("--cap-drop=ALL", commands[1])
        self.assertIn(f"{self.root}:/workspace:ro,z", commands[1])
        self.assertEqual(commands[2][:5], [
            sys.executable, "-u", "-m", "hyprland_copr", "run",
        ])
        self.assertFalse(any(
            str(argument).startswith("copr-") for command in commands for argument in command
        ))
        verify.assert_called_once_with(self.root / "dist/run/bundle")
        self.assertEqual(self.result()["status"], "passed")
        self.assertFalse(self.result()["publication_attempted"])

    def test_full_stack_is_verified_before_automatic_publication(self):
        self.config["copr"]["publish"] = True
        events = []
        manifest = {"checked": True}
        with (
            patch.object(release, "run_logged", side_effect=lambda *args: events.append("command")),
            patch.object(release, "verify_bundle", side_effect=lambda path: (
                events.append("verify") or manifest
            )),
            patch.object(release, "Publication") as publisher,
        ):
            publisher.return_value.pending.return_value = None
            publisher.return_value.run.side_effect = lambda *args: events.append("publish")
            self.assertEqual(release.execute(self.arguments()), 0)
        self.assertEqual(events, ["command", "command", "command", "verify", "publish"])
        publisher.return_value.run.assert_called_once_with(
            self.root / "dist/run/bundle", manifest,
        )
        self.assertEqual(self.result()["publication_status"], "promoted")

    def test_partial_checks_and_explicit_local_runs_never_publish(self):
        self.config["copr"]["publish"] = True
        for index, flags in enumerate((
            ["--component", "hyprland"],
            ["--component", "hyprpaper"],
            ["--lua-only"],
            ["--checks-only"],
            ["--no-publish"],
        )):
            with (
                self.subTest(flags=flags),
                patch.object(release, "run_logged"),
                patch.object(release, "verify_bundle"),
                patch.object(release, "Publication") as publisher,
            ):
                args = release.parser().parse_args(["--output", f"run-{index}", *flags])
                self.assertEqual(release.execute(args), 0)
                publisher.assert_not_called()
                self.assertFalse(self.result(f"run-{index}")["publication_attempted"])

    def test_failed_integrity_prevents_automatic_publication(self):
        self.config["copr"]["publish"] = True
        with (
            patch.object(release, "run_logged"),
            patch.object(release, "verify_bundle", side_effect=PipelineError("bad bundle")),
            patch.object(release, "Publication") as publisher,
        ):
            publisher.return_value.pending.return_value = None
            self.assertEqual(release.execute(self.arguments()), 1)
            publisher.return_value.run.assert_not_called()

    def test_pending_publication_skips_rebuild_but_not_qa_or_integrity(self):
        self.config["copr"]["publish"] = True
        bundle = self.root / "previous/bundle"
        with (
            patch.object(release, "run_logged") as run,
            patch.object(release, "verify_bundle") as verify,
            patch.object(release, "Publication") as publisher,
        ):
            publisher.return_value.pending.return_value = bundle
            self.assertEqual(release.execute(self.arguments()), 0)
        self.assertEqual(run.call_count, 2)
        verify.assert_called_once_with(bundle)
        self.assertEqual(self.result()["bundle"], str(bundle))

    def test_publication_failure_is_recorded_as_failed_not_passed(self):
        self.config["copr"]["publish"] = True

        def fail(*arguments):
            publisher.call_args.args[-1]("copr-validate")
            raise PipelineError("signed candidate validation failed")

        with (
            patch.object(release, "run_logged"),
            patch.object(release, "verify_bundle"),
            patch.object(release, "Publication") as publisher,
        ):
            publisher.return_value.pending.return_value = None
            publisher.return_value.run.side_effect = fail
            self.assertEqual(release.execute(self.arguments()), 1)
        self.assertEqual(self.result()["status"], "failed")
        self.assertEqual(self.result()["phase"], "copr-validate")
        self.assertTrue(self.result()["publication_attempted"])

    def test_matching_published_source_set_does_not_verify_a_missing_bundle(self):
        self.config["copr"]["publish"] = True

        def commands(command, log):
            if "run" in command and "--skip-published" in command:
                path = self.root / "dist/run/resolution.json"
                path.write_text("{}")
                (self.root / ".state/copr.json").write_text("{}")

        with (
            patch.object(release, "run_logged", side_effect=commands),
            patch.object(release, "already_published", return_value=True),
            patch.object(release, "verify_bundle") as verify,
            patch.object(release, "Publication") as publisher,
        ):
            publisher.return_value.pending.return_value = None
            self.assertEqual(release.execute(self.arguments()), 0)
            verify.assert_not_called()
            publisher.return_value.run.assert_not_called()
        self.assertEqual(self.result()["publication_status"], "already-published")

    def test_checks_only_does_not_build_or_verify_rpms(self):
        with (
            patch.object(release, "run_logged") as run,
            patch.object(release, "verify_bundle") as verify,
        ):
            self.assertEqual(release.execute(self.arguments("--checks-only")), 0)
        self.assertEqual(run.call_count, 2)
        verify.assert_not_called()
        self.assertIsNone(self.result()["bundle"])

    def test_complete_resolution_is_saved_before_a_failed_build(self):
        config = load_config()
        config["pipeline"]["state_directory"] = str(self.root / ".state")
        snapshot = self.root / "run/resolution.json"
        args = cli.parser().parse_args([
            "run", "--component", "lua", "--resolution-output", str(snapshot),
            "--output", str(self.root / "bundle"),
            "--publication-state", str(self.root / "publication.json"),
        ])
        resolution = {
            "status": "resolved",
            "root": "lua",
            "primaries": ["lua"],
            "order": ["lua"],
            "blockers": [],
        }

        def fail_build(actual_resolution, destination):
            self.assertEqual(json.loads(snapshot.read_text()), actual_resolution)
            raise PipelineError("build stopped")

        with (
            patch.object(cli, "load_config", return_value=config),
            patch.object(cli, "Sources") as sources,
            patch.object(cli, "Container"),
            patch.object(cli, "Resolver") as resolver,
            patch.object(cli, "Builder") as builder,
        ):
            sources.return_value.releases.return_value = [{"version": "5.5.1"}]
            resolver.return_value.resolve.return_value = resolution
            builder.return_value.build.side_effect = fail_build
            with self.assertRaisesRegex(PipelineError, "build stopped"):
                cli.execute(args)
        self.assertEqual(json.loads(snapshot.read_text())["generation"], 1)

    def test_component_and_version_selection_are_forwarded(self):
        for selection in (["--component", "hyprpaper"], ["--lua-only"]):
            with self.subTest(selection=selection):
                directory = f"dist/{selection[-1].removeprefix('--')}"
                args = release.parser().parse_args([
                    "--output", directory, "--version", "1.2.3", *selection,
                ])
                with (
                    patch.object(release, "run_logged") as run,
                    patch.object(release, "verify_bundle"),
                ):
                    self.assertEqual(release.execute(args), 0)
                command = run.call_args.args[0]
                self.assertEqual(command[-len(selection):], selection)
                self.assertEqual(command[command.index("--version") + 1], "1.2.3")

    def test_custom_config_and_publication_state_are_forwarded_as_local_paths(self):
        with (
            patch.object(release, "run_logged") as run,
            patch.object(release, "verify_bundle"),
        ):
            release.execute(self.arguments(
                "--config", "config/server.toml", "--publication-state", "state/publication.json",
            ))
        command = run.call_args.args[0]
        self.assertEqual(
            command[command.index("--config") + 1], self.root / "config/server.toml",
        )
        self.assertEqual(
            command[command.index("--publication-state") + 1],
            self.root / "state/publication.json",
        )

    def test_failure_status_is_saved_and_later_phases_do_not_run(self):
        for phase, index, code, status in (
            ("checks-image", 0, 1, "failed"),
            ("checks", 1, 1, "failed"),
            ("build", 2, 3, "blocked"),
            ("build", 2, 1, "failed"),
            ("build", 2, -9, "failed"),
        ):
            with self.subTest(phase=phase, code=code):
                directory = f"dist/{phase}-{code}"
                args = release.parser().parse_args(["--output", directory])
                with (
                    patch.object(release, "run_logged", side_effect=[
                        *[Mock() for _ in range(index)],
                        subprocess.CalledProcessError(code, ["probe"]),
                    ]) as run,
                    patch.object(release, "verify_bundle") as verify,
                ):
                    expected_code = code if code > 0 else 128 - code
                    self.assertEqual(release.execute(args), expected_code)
                self.assertEqual(run.call_count, index + 1)
                verify.assert_not_called()
                result = self.result(directory)
                self.assertEqual(result["status"], status)
                self.assertEqual(result["phase"], phase)
                self.assertEqual(result["exit_code"], expected_code)
                self.assertIsNone(result["bundle"])

    def test_failed_integrity_check_cannot_mark_a_run_successful(self):
        with (
            patch.object(release, "run_logged"),
            patch.object(release, "verify_bundle", side_effect=PipelineError("bad bundle")),
        ):
            self.assertEqual(release.execute(self.arguments()), 1)
        self.assertEqual(self.result()["status"], "failed")
        self.assertEqual(self.result()["phase"], "verify")
        self.assertIsNone(self.result()["bundle"])

    def test_existing_output_is_never_reused_or_overwritten(self):
        output = self.root / "dist/run"
        output.mkdir(parents=True)
        marker = output / "result.json"
        marker.write_text("previous result")
        with patch.object(release, "run_logged") as run:
            with self.assertRaisesRegex(PipelineError, "new run directory"):
                release.execute(self.arguments())
        self.assertEqual(marker.read_text(), "previous result")
        run.assert_not_called()

    def test_default_runs_get_distinct_local_directories(self):
        args = release.parser().parse_args(["--checks-only"])
        with patch.object(release, "run_logged"):
            self.assertEqual(release.execute(args), 0)
            self.assertEqual(release.execute(args), 0)
        paths = list((self.root / "dist/runs").glob("*/result.json"))
        self.assertEqual(len(paths), 2)
        self.assertTrue(all(json.loads(path.read_text())["status"] == "passed" for path in paths))

    def test_automation_lock_rejects_overlapping_runs(self):
        with lock(self.root / ".state/automation"), patch.object(release, "run_logged") as run:
            with self.assertRaisesRegex(PipelineError, "Another pipeline"):
                release.execute(self.arguments())
        run.assert_not_called()
        self.assertFalse((self.root / "dist/run").exists())

    def test_runner_does_not_hold_the_child_clis_state_lock(self):
        def child(command, log):
            with lock(self.root / ".state"):
                pass

        with (
            patch.object(release, "run_logged", side_effect=child),
            patch.object(release, "verify_bundle"),
        ):
            self.assertEqual(release.execute(self.arguments()), 0)

    def test_root_is_rejected_before_running_commands(self):
        with (
            patch.object(release.os, "getuid", return_value=0),
            patch.object(release, "run_logged") as run,
        ):
            with self.assertRaisesRegex(PipelineError, "non-root"):
                release.execute(self.arguments())
        run.assert_not_called()

    def test_logged_commands_run_locally_without_a_shell(self):
        path = self.root / "command.log"
        with patch.object(release.subprocess, "run") as run:
            release.run_logged(["docker", "probe"], path)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["docker", "probe"])
        self.assertEqual(run.call_args.kwargs["cwd"], self.root)
        self.assertTrue(run.call_args.kwargs["check"])
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertTrue(path.is_file())

    def test_hosted_workflows_and_credential_helpers_are_absent(self):
        workflows = ROOT / ".github/workflows"
        self.assertFalse(list(workflows.glob("*.yml")) + list(workflows.glob("*.yaml")))
        self.assertFalse((ROOT / "ci/credentials.py").exists())
        self.assertFalse((ROOT / "ci/publish_phase.py").exists())

    def test_timer_uses_only_the_local_runner(self):
        service = (ROOT / "deploy/systemd/hyprland-copr.service").read_text()
        timer = (ROOT / "deploy/systemd/hyprland-copr.timer").read_text()
        self.assertIn("WorkingDirectory=%h/hyprland-copr", service)
        self.assertIn("ExecStart=/usr/bin/python3 -m ci.release", service)
        self.assertIn("Unit=hyprland-copr.service", timer)
        self.assertIn("Persistent=true", timer)
        self.assertNotIn("--publish", service)


if __name__ == "__main__":
    unittest.main()
