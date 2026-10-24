import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from deploy import install as installer
from hyprland_copr.config import ROOT


class TimerInstallerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "checkout with % and $ spaces"
        self.home = self.root / "home"
        self.home.mkdir()
        for relative in (
            "ci/release.py",
            "hyprland_copr/__main__.py",
            "config/project.toml",
            "container/Checksfile",
        ):
            path = self.project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("")
        templates = self.project / "deploy/systemd"
        templates.mkdir(parents=True)
        for name in (installer.SERVICE, installer.TIMER):
            shutil.copyfile(ROOT / "deploy/systemd" / name, templates / name)
        self.commands = []
        for context in (
            patch.dict(os.environ, {"HOME": str(self.home), "XDG_CONFIG_HOME": ""}),
            patch.object(installer.os, "getuid", return_value=1000),
            patch.object(installer.shutil, "which", side_effect=lambda tool: "/usr/bin/" + tool),
            patch.object(installer.subprocess, "run", side_effect=self.command),
        ):
            context.start()
            self.addCleanup(context.stop)
        self.directory = self.home / ".config/systemd/user"

    def command(self, arguments, **options):
        self.commands.append(arguments)
        if "is-active" in arguments:
            return subprocess.CompletedProcess(arguments, 3, stdout="inactive\n", stderr="")
        return subprocess.CompletedProcess(arguments, 0, stdout=b"", stderr=b"")

    def arguments(self, *extra):
        return installer.parser().parse_args(extra)

    def test_default_installs_real_paths_and_enables_only_the_timer(self):
        result = installer.install(self.arguments(), self.project)
        self.assertEqual(result, self.directory)
        service = (result / installer.SERVICE).read_text()
        self.assertTrue(service.startswith(installer.MARKER))
        self.assertIn(
            f'WorkingDirectory={str(self.project).replace("%", "%%")}\n', service,
        )
        self.assertNotIn("WorkingDirectory=%h/hyprland-copr", service)
        self.assertIn("-m ci.release\n", service)
        timer = (result / installer.TIMER).read_text()
        self.assertIn("OnCalendar=*-*-* 00,06,12,18:19:00\n", timer)
        self.assertIn("Persistent=true\n", timer)
        self.assertEqual((result / installer.SERVICE).stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.commands[0][:3], ["systemd-analyze", "--user", "verify"])
        self.assertEqual(self.commands[-3:], [
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", installer.TIMER],
            ["systemctl", "--user", "restart", installer.TIMER],
        ])
        self.assertIn(["docker", "info"], self.commands)
        self.assertFalse(any(
            "start" in command or "show-environment" in command for command in self.commands
        ))

    def test_no_enable_installs_without_manager_or_docker_daemon_calls(self):
        installer.install(self.arguments("--no-enable"), self.project)
        self.assertTrue((self.directory / installer.TIMER).is_file())
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(self.commands[0][0], "systemd-analyze")

    def test_custom_calendar_and_xdg_configuration_directory(self):
        config = self.root / "custom config"
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config)}):
            result = installer.install(
                self.arguments("--no-enable", "--calendar", "daily"), self.project,
            )
        self.assertEqual(result, config / "systemd/user")
        self.assertIn("OnCalendar=daily\n", (result / installer.TIMER).read_text())

    def test_managed_install_can_be_repeated_and_schedule_updated(self):
        installer.install(self.arguments("--no-enable"), self.project)
        original = (self.directory / installer.SERVICE).read_bytes()
        with patch.object(installer, "atomic_write", wraps=installer.atomic_write) as write:
            installer.install(self.arguments("--no-enable"), self.project)
        write.assert_not_called()
        installer.install(self.arguments("--no-enable", "--calendar", "weekly"), self.project)
        self.assertEqual((self.directory / installer.SERVICE).read_bytes(), original)
        self.assertIn("OnCalendar=weekly\n", (self.directory / installer.TIMER).read_text())

    def test_root_is_rejected_without_writing_or_running_commands(self):
        with patch.object(installer.os, "getuid", return_value=0):
            with self.assertRaisesRegex(RuntimeError, "without sudo"):
                installer.install(self.arguments(), self.project)
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.commands, [])

    def test_old_python_is_rejected(self):
        with patch.object(installer.sys, "version_info", (3, 10)):
            with self.assertRaisesRegex(RuntimeError, "3.11"):
                installer.install(self.arguments(), self.project)
        self.assertFalse(self.directory.exists())

    def test_missing_prerequisites_are_not_installed_automatically(self):
        with patch.object(
            installer.shutil, "which", side_effect=lambda tool: None if tool == "docker" else tool,
        ):
            with self.assertRaisesRegex(RuntimeError, "Missing docker"):
                installer.install(self.arguments(), self.project)
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.commands, [])

    def test_incomplete_checkout_is_rejected(self):
        (self.project / "ci/release.py").unlink()
        with self.assertRaisesRegex(RuntimeError, "Missing project file"):
            installer.install(self.arguments(), self.project)
        self.assertFalse(self.directory.exists())

    def test_relative_xdg_directory_is_rejected(self):
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "relative"}):
            with self.assertRaisesRegex(ValueError, "absolute path"):
                installer.install(self.arguments("--no-enable"), self.project)
        self.assertFalse(self.directory.exists())

    def test_unmanaged_units_are_not_overwritten(self):
        self.directory.mkdir(parents=True)
        path = self.directory / installer.SERVICE
        path.write_text("[Service]\nExecStart=/bin/true\n")
        original = path.read_text()
        with self.assertRaisesRegex(RuntimeError, "unmanaged unit"):
            installer.install(self.arguments(), self.project)
        self.assertEqual(path.read_text(), original)
        self.assertFalse((self.directory / installer.TIMER).exists())
        self.assertEqual(self.commands, [])

    def test_symlinked_units_are_not_followed_or_replaced(self):
        self.directory.mkdir(parents=True)
        target = self.root / "original.service"
        target.write_text(installer.MARKER + "original")
        link = self.directory / installer.SERVICE
        link.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, "symlink"):
            installer.install(self.arguments("--no-enable"), self.project)
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_text(), installer.MARKER + "original")

    def test_control_characters_cannot_inject_unit_directives(self):
        for calendar in ("daily\nUnit=unexpected.service", "daily\r", "\tdaily", ""):
            with self.subTest(calendar=calendar), self.assertRaises(ValueError):
                installer.install(
                    self.arguments("--no-enable", "--calendar", calendar), self.project,
                )
        self.assertFalse(self.directory.exists())

    def test_unit_paths_escape_spaces_specifiers_quotes_and_backslashes(self):
        text = installer.unit_quote('/tmp/a b/%h/"quoted"\\path/$value')
        self.assertIn("a b", text)
        self.assertIn("%%h", text)
        self.assertIn('\\"quoted\\"', text)
        self.assertIn("\\\\path", text)
        self.assertIn("/$value", text)
        self.assertIn("/$value", installer.unit_quote("/tmp/$value"))
        for path in ("/tmp/path\nExecStart=/bin/true", "/tmp/path\x00"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                installer.unit_quote(path)

    def test_executable_path_dollars_are_literal_not_doubled(self):
        service = installer.render_units(self.project, "/tmp/python $literal")[installer.SERVICE]
        self.assertIn('ExecStart="/tmp/python $literal" -m ci.release\n', service)
        self.assertNotIn("$$literal", service)

    def test_working_directory_is_not_quoted_or_silently_trimmed(self):
        value = '/tmp/checkout with spaces/%h/"quotes"'
        self.assertEqual(installer.unit_path(value), value.replace("%", "%%"))
        for path in ("/tmp/trailing ", "/tmp/trailing\\", "/tmp/newline\n"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                installer.unit_path(path)

    def test_changed_template_layout_requires_review(self):
        template = self.project / "deploy/systemd" / installer.SERVICE
        template.write_text(template.read_text().replace("ExecStart=", "OtherSetting="))
        with self.assertRaisesRegex(ValueError, "exactly one ExecStart"):
            installer.install(self.arguments("--no-enable"), self.project)
        self.assertFalse(self.directory.exists())

    def test_invalid_unit_validation_does_not_write_files(self):
        with patch.object(
            installer, "run", side_effect=subprocess.CalledProcessError(1, ["systemd-analyze"]),
        ):
            with self.assertRaisesRegex(RuntimeError, "unit validation failed"):
                installer.install(self.arguments("--no-enable"), self.project)
        self.assertFalse(self.directory.exists())

    def test_unavailable_manager_or_docker_prevents_activation_and_writes(self):
        original = installer.run
        for tool in ("systemctl", "docker"):
            def failing(arguments, **options):
                if arguments[0] == tool:
                    raise subprocess.CalledProcessError(1, arguments)
                return original(arguments, **options)

            with self.subTest(tool=tool), patch.object(installer, "run", side_effect=failing):
                with self.assertRaises(subprocess.CalledProcessError):
                    installer.install(self.arguments(), self.project)
            self.assertFalse(self.directory.exists())

    def test_running_or_uncertain_service_state_prevents_changes(self):
        original = self.command
        for state, code in (("active", 0), ("activating", 3), ("reloading", 0), ("unknown", 1)):
            def command(arguments, **options):
                if "is-active" in arguments:
                    return subprocess.CompletedProcess(arguments, code, stdout=state, stderr="")
                return original(arguments, **options)

            with self.subTest(state=state), patch.object(
                installer.subprocess, "run", side_effect=command,
            ):
                with self.assertRaisesRegex(RuntimeError, "service is active"):
                    installer.install(self.arguments(), self.project)
            self.assertFalse(self.directory.exists())

    def test_partial_unit_write_failure_restores_previous_files(self):
        installer.install(self.arguments("--no-enable"), self.project)
        original_contents = {
            name: (self.directory / name).read_text()
            for name in (installer.SERVICE, installer.TIMER)
        }
        template = self.project / "deploy/systemd" / installer.SERVICE
        template.write_text(template.read_text().replace("UMask=0077", "UMask=0027"))
        original = installer.atomic_write

        def failing(path, text):
            if path.name == installer.TIMER:
                raise OSError("simulated write failure")
            original(path, text)

        with patch.object(installer, "atomic_write", side_effect=failing):
            with self.assertRaisesRegex(OSError, "simulated"):
                installer.install(self.arguments("--no-enable", "--calendar", "daily"), self.project)
        for name, content in original_contents.items():
            self.assertEqual((self.directory / name).read_text(), content)

    def test_activation_failure_leaves_valid_files_for_retry_and_reports_failure(self):
        original = installer.run

        def failing(arguments, **options):
            if "enable" in arguments:
                raise subprocess.CalledProcessError(1, arguments)
            return original(arguments, **options)

        with (
            patch.object(installer, "PROJECT", self.project),
            patch.object(installer, "run", side_effect=failing),
        ):
            self.assertEqual(installer.main([]), 1)
        self.assertTrue((self.directory / installer.SERVICE).is_file())
        self.assertTrue((self.directory / installer.TIMER).is_file())


if __name__ == "__main__":
    unittest.main()
