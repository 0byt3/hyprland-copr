import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from hyprland_copr import fedora, runtime
from hyprland_copr.config import ROOT
from hyprland_copr.model import PipelineError


class PodmanRuntimeTests(unittest.TestCase):
    def test_native_local_engine_cannot_follow_remote_environment_defaults(self):
        self.assertEqual(runtime.PODMAN, ("podman", "--remote=false"))

    def test_unprivileged_worker_preserves_invoking_uid_and_gid(self):
        with (
            patch.object(runtime.os, "getuid", return_value=1001),
            patch.object(runtime.os, "getgid", return_value=1002),
        ):
            self.assertEqual(runtime.user_arguments(), [
                "--userns=keep-id", "--user", "1001:1002",
            ])

    def test_uid_zero_cannot_be_an_unprivileged_worker(self):
        with patch.object(runtime.os, "getuid", return_value=0):
            with self.assertRaisesRegex(PipelineError, "non-root"):
                runtime.user_arguments()

    def test_offline_runs_keep_owner_namespace_and_never_pull_an_image(self):
        with (
            patch.object(runtime.os, "getuid", return_value=1001),
            patch.object(runtime.os, "getgid", return_value=1002),
            patch.object(fedora, "run") as run,
        ):
            fedora.offline_run("image", "/temporary/work", ["probe"], log="/temporary/log")
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["podman", "--remote=false", "run"])
        for value in ("--userns=keep-id", "1001:1002", "--network=none", "--pull=never"):
            self.assertIn(value, command)
        self.assertFalse(any(str(value).endswith(":U") for value in command))

    def test_dependency_installers_run_as_namespace_root_without_host_privilege(self):
        with patch.object(fedora, "run") as run:
            with fedora.Container("image"):
                pass
        command = run.call_args_list[0].args[0]
        self.assertEqual(command[:3], ["podman", "--remote=false", "run"])
        self.assertEqual(command[command.index("--user") + 1], "0:0")
        self.assertNotIn("--privileged", command)
        self.assertNotIn("--userns=keep-id", command)

    def test_snapshot_is_committed_quietly_and_cleaned_even_on_failure(self):
        with patch.object(fedora, "run", return_value="sha256:temporary") as run:
            container = fedora.Container("image")
            with self.assertRaisesRegex(RuntimeError, "probe"):
                with container.offline_image() as image:
                    self.assertEqual(image, "sha256:temporary")
                    raise RuntimeError("probe")
        self.assertEqual(run.call_args_list[0].args[0], [
            "podman", "--remote=false", "commit", "--quiet", container.name,
        ])
        self.assertEqual(run.call_args_list[1].args[0], [
            "podman", "--remote=false", "image", "rm", "sha256:temporary",
        ])

    def test_clean_home_preserves_default_podman_store_but_not_credentials(self):
        with patch.dict(os.environ, {
            "HOME": "/home/copr",
            "PATH": "/usr/bin",
            "XDG_RUNTIME_DIR": "/run/user/1001",
            "COPR_CONFIG_FILE": "/protected/credentials",
            "GITHUB_TOKEN": "offline-marker",
            "CONTAINER_HOST": "ssh://remote.invalid",
            "PYTHONPATH": "/untrusted",
        }, clear=True):
            environment = runtime.validation_environment("/temporary/clean-home")
        self.assertEqual(environment["HOME"], "/temporary/clean-home")
        self.assertEqual(environment["XDG_DATA_HOME"], "/home/copr/.local/share")
        self.assertEqual(environment["XDG_CONFIG_HOME"], "/home/copr/.config")
        self.assertEqual(environment["XDG_CACHE_HOME"], "/home/copr/.cache")
        self.assertEqual(environment["XDG_RUNTIME_DIR"], "/run/user/1001")
        for key in ("COPR_CONFIG_FILE", "GITHUB_TOKEN", "CONTAINER_HOST", "PYTHONPATH"):
            self.assertNotIn(key, environment)

    def test_explicit_public_storage_configuration_is_retained(self):
        values = {
            "XDG_DATA_HOME": "/srv/copr/data",
            "XDG_CONFIG_HOME": "/srv/copr/config",
            "XDG_CACHE_HOME": "/srv/copr/cache",
            "CONTAINERS_STORAGE_CONF": "/srv/copr/storage.conf",
        }
        with patch.dict(os.environ, values):
            environment = runtime.validation_environment("/temporary/clean-home")
        for key, value in values.items():
            self.assertEqual(environment[key], value)

    def test_relative_storage_paths_fail_instead_of_selecting_another_store(self):
        with patch.dict(os.environ, {"XDG_DATA_HOME": "relative"}):
            with self.assertRaisesRegex(PipelineError, "absolute path"):
                runtime.validation_environment("/temporary/clean-home")

    def test_service_does_not_block_rootless_uid_mapping_helpers(self):
        service = (ROOT / "deploy/systemd/hyprland-copr.service").read_text()
        self.assertNotIn("\nNoNewPrivileges=yes", service)

    def test_lua_safety_guard_recognizes_podman_and_still_rejects_host_execution(self):
        script = (ROOT / "tests/lua55/validate-installed.sh").read_text()
        self.assertIn("! -e /run/.containerenv", script)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "guard.sh"
            guard = script.split('\nroot="', 1)[0]
            guard = guard.replace("/run/.containerenv", temporary + "/absent-podman")
            guard = guard.replace("/.dockerenv", temporary + "/absent-docker")
            path.write_text(guard)
            result = subprocess.run(["bash", str(path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Refusing", result.stderr)

    def test_native_context_contains_only_container_definitions(self):
        ignore = (ROOT / ".containerignore").read_text()
        self.assertEqual(ignore.splitlines()[0], "**")
        for name in ("Containerfile", "Checksfile", "Publisherfile"):
            self.assertIn("!container/" + name, ignore)
        self.assertFalse((ROOT / ".dockerignore").exists())


if __name__ == "__main__":
    unittest.main()
