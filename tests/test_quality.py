import importlib.util
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from ci import check
from container import build
from hyprland_copr.config import ROOT
from hyprland_copr.packaging import render_spec


class QualityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def file(self, relative, text=""):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_shellcheck_covers_project_scripts_but_not_generated_artifacts(self):
        first = self.file("ci/probe.sh")
        deployment = self.file("deploy/probe.sh")
        second = self.file("tests/nested/probe.bash")
        self.file(".state/upstream/vendor.sh")
        self.file("dist/session-evidence/probe.sh")
        self.file("tests/not-shell.py")
        with patch.object(check.subprocess, "run") as run:
            check.check_shell_scripts(self.root)
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ["bash", "-n", str(first)],
            ["shellcheck", "--external-sources", str(first)],
            ["bash", "-n", str(deployment)],
            ["shellcheck", "--external-sources", str(deployment)],
            ["bash", "-n", str(second)],
            ["shellcheck", "--external-sources", str(second)],
        ])
        self.assertTrue(all(call.kwargs["check"] for call in run.call_args_list))

    def test_shellcheck_failure_is_fatal(self):
        self.file("tests/probe.sh")
        with patch.object(check.subprocess, "run", side_effect=[
            Mock(),
            subprocess.CalledProcessError(1, ["shellcheck"]),
        ]):
            with self.assertRaises(subprocess.CalledProcessError):
                check.check_shell_scripts(self.root)

    def test_repository_runner_invokes_local_checks_without_workflows(self):
        with (
            patch.object(check.subprocess, "run") as run,
            patch.object(check, "check_shell_scripts") as shell,
        ):
            check.main()
        self.assertEqual(run.call_args_list[0].args[0][0], "ruff")
        shell.assert_called_once_with()
        self.assertEqual(run.call_count, 3)

    def build_mocks(self):
        return (
            patch.object(build.os, "geteuid", return_value=1000),
            patch.object(build.sys, "argv", ["build.py", "fixture.spec"]),
        )

    def test_spec_lint_failure_prevents_compilation(self):
        uid, argv = self.build_mocks()
        with uid, argv, patch.object(
            build.subprocess, "run", side_effect=subprocess.CalledProcessError(64, ["rpmlint"]),
        ) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                build.main(self.root)
        run.assert_called_once_with(
            ["rpmlint", str(self.root / "SPECS/fixture.spec")], check=True,
        )
        self.assertFalse((self.root / "packages.json").exists())

    def test_rpm_lint_covers_spec_source_and_all_binary_packages_before_metadata(self):
        source = self.file("SRPMS/fixture-1.0-1.src.rpm")
        binary = self.file("RPMS/x86_64/fixture-1.0-1.x86_64.rpm")
        debug = self.file("RPMS/x86_64/fixture-debuginfo-1.0-1.x86_64.rpm")
        uid, argv = self.build_mocks()
        with (
            uid, argv,
            patch.object(build.subprocess, "run") as run,
            patch.object(build.subprocess, "check_output", return_value=(
                "fixture\t0\t1.0\t1\tx86_64"
            )) as query,
        ):
            build.main(self.root)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual([command[0] for command in commands], ["rpmlint", "rpmbuild", "rpmlint"])
        self.assertEqual(commands[-1], [
            "rpmlint", str(self.root / "SPECS/fixture.spec"),
            str(source), str(binary), str(debug),
        ])
        self.assertTrue(all(call.kwargs["check"] for call in run.call_args_list))
        self.assertEqual(query.call_count, 2)
        self.assertEqual(len(json.loads((self.root / "packages.json").read_text())), 2)

    def test_rpm_lint_failure_prevents_package_metadata(self):
        self.file("SRPMS/fixture-1.0-1.src.rpm")
        self.file("RPMS/x86_64/fixture-1.0-1.x86_64.rpm")
        uid, argv = self.build_mocks()
        with (
            uid, argv,
            patch.object(build.subprocess, "run", side_effect=[
                Mock(), Mock(), subprocess.CalledProcessError(64, ["rpmlint"]),
            ]),
            patch.object(build.subprocess, "check_output") as query,
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                build.main(self.root)
        query.assert_not_called()
        self.assertFalse((self.root / "packages.json").exists())

    def test_missing_artifacts_fail_instead_of_linting_an_empty_set(self):
        uid, argv = self.build_mocks()
        with uid, argv, patch.object(build.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "exactly one source RPM"):
                build.main(self.root)
        self.assertEqual(run.call_count, 2)

    def test_root_cannot_run_builds_or_lint(self):
        with (
            patch.object(build.os, "geteuid", return_value=0),
            patch.object(build.subprocess, "run") as run,
        ):
            with self.assertRaisesRegex(RuntimeError, "as root"):
                build.main(self.root)
        run.assert_not_called()

    def validation_module(self):
        spec = importlib.util.spec_from_file_location(
            "quality_validation", ROOT / "container/validate.py",
        )
        module = importlib.util.module_from_spec(spec)
        with patch.dict("sys.modules", {"rpm": Mock(), "smoke": Mock()}):
            spec.loader.exec_module(module)
        return module

    def test_candidate_validation_lints_source_and_all_binary_rpms(self):
        module = self.validation_module()
        manifest = {
            "builds": [{"srpm": "srpms/fixture.src.rpm"}],
            "packages": [
                {"file": "rpms/fixture.rpm"},
                {"file": "rpms/fixture-debuginfo.rpm"},
            ],
        }
        with patch.object(module.subprocess, "run") as run:
            module.lint_candidates(manifest)
        run.assert_called_once_with([
            "rpmlint", "/work/srpms/fixture.src.rpm",
            "/work/rpms/fixture.rpm", "/work/rpms/fixture-debuginfo.rpm",
        ], check=True)

    def test_candidate_lint_failure_is_fatal(self):
        module = self.validation_module()
        with patch.object(
            module.subprocess, "run", side_effect=subprocess.CalledProcessError(64, ["rpmlint"]),
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                module.lint_candidates({
                    "builds": [{"srpm": "srpms/fixture.src.rpm"}],
                    "packages": [{"file": "rpms/fixture.rpm"}],
                })

    def test_changelogs_are_deterministic_for_every_packaging_adapter(self):
        self.file("subprojects/udis86/configure.ac", "AC_INIT([udis86], [1.7.2])\n")
        self.file("meson.build", "project('hyprland-protocols')\n")
        for name in (
            "lua",
            "hyprland",
            "hyprwayland-scanner",
            "hyprland-protocols",
            "glaze",
            "hyprutils",
            "hypridle",
            "hyprlock",
            "hyprpaper",
            "hyprpicker",
            "hyprsunset",
            "hyprland-guiutils",
            "uwsm",
            "xdg-desktop-portal-hyprland",
        ):
            with self.subTest(component=name):
                source = {
                    "name": name,
                    "version": "1.2.3",
                    "epoch": 1767225600,
                    "commit": "a" * 40,
                    "directory": str(self.root),
                }
                text = render_spec(source, [], "2.stack3")
                self.assertEqual(text, render_spec(source, [], "2.stack3"))
                self.assertEqual(text.count("\n%changelog\n"), 1)
                self.assertIn(
                    "* Thu Jan 01 2026 Hyprland COPR maintainers - 1.2.3-2.stack3\n", text,
                )
                for description in re.findall(
                    r"(?ms)^%description[^\n]*\n(.*?)(?=^%)", text,
                ):
                    for line in description.splitlines():
                        self.assertLessEqual(len(line), 79, line)

    def test_quality_tools_are_repository_installed_in_both_images(self):
        checks = (ROOT / "container/Checksfile").read_text()
        builder = (ROOT / "container/Containerfile").read_text()
        for package in ("ShellCheck", "rpmlint"):
            self.assertIn(f"        {package} \\", checks)
        self.assertNotIn("actionlint", checks)
        self.assertNotIn("alpine", checks)
        self.assertIn("        rpmlint \\", builder)
        self.assertNotIn("curl", "\n".join(
            line for line in checks.splitlines() if not line.startswith("#")
        ))
    def test_undated_lua_source_has_a_valid_fixed_packaging_date(self):
        source = {
            "name": "lua",
            "version": "5.5.1",
            "epoch": 0,
            "commit": "",
            "directory": str(self.root),
        }
        text = render_spec(source, [], 1)
        self.assertIn("* Sat Oct 24 2026 Hyprland COPR maintainers - 5.5.1-1\n", text)
        self.assertNotIn("Jan 01 1970", text)
        self.assertEqual(text, render_spec(source, [], 1))


if __name__ == "__main__":
    unittest.main()
