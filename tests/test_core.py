import hashlib
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

from hyprland_copr.build import Builder, check_trace, verify_bundle
from hyprland_copr.config import ROOT, load_config
from hyprland_copr.dependencies import (
    active_declarations,
    commands,
    discover,
    pkg_requirement,
)
from hyprland_copr.fedora import Container, FedoraProviders, REPO_OPTIONS, build_image, offline_run
from hyprland_copr.model import PipelineError, Requirement, topological_order, version_key
from hyprland_copr.packaging import prepare, render_spec
from hyprland_copr.resolver import Resolver
from hyprland_copr.sources import Sources, archive_tree, populate_submodules, stable_releases
from hyprland_copr.util import digest_json, lock, save_json, sha256


class TemporaryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)


class ModelTests(unittest.TestCase):
    def test_numeric_not_lexical_ordering(self):
        self.assertGreater(version_key("0.10.0"), version_key("0.9.9"))
        self.assertEqual(version_key("v5.5"), version_key("5.5.0"))

    def test_prerelease_and_injection_rejected(self):
        for version in ("5.5-rc1", "1.0; id", "../1.0", "master"):
            with self.subTest(version=version), self.assertRaises(PipelineError):
                version_key(version)

    def test_combined_bounds(self):
        value = Requirement("cmake(glaze)", ((">=", "7"), ("<", "8")))
        self.assertTrue(value.accepts("7.2.0"))
        self.assertFalse(value.accepts("8.0.0"))
        self.assertFalse(value.accepts("6.9"))
        self.assertEqual(Requirement.from_dict(value.to_dict()), value)

    def test_graph_order_and_cycles(self):
        self.assertEqual(topological_order({
            "hyprland": {"aquamarine", "lua"},
            "aquamarine": {"hyprutils"},
            "hyprutils": set(),
            "lua": set(),
        }), ["hyprutils", "aquamarine", "lua", "hyprland"])
        with self.assertRaisesRegex(PipelineError, "cycle"):
            topological_order({"a": {"b"}, "b": {"a"}})


class DiscoveryTests(TemporaryTest):
    def scan(self, text, name="hyprutils"):
        (self.root / "CMakeLists.txt").write_text(text)
        return discover({"name": name, "directory": str(self.root)})

    def test_multiline_variables_optional_and_disabled_tests(self):
        requirements = self.scan("""
cmake_minimum_required(VERSION 3.19)
set(MIN_VERSION 0.14.0)
pkg_check_modules(deps REQUIRED IMPORTED_TARGET
    pixman-1 hyprutils>=${MIN_VERSION})
pkg_check_modules(optional QUIET epoll-shim)
if(BUILD_TESTING)
    find_package(GTest CONFIG REQUIRED)
endif()
if(CMAKE_SYSTEM_NAME STREQUAL "Linux")
    pkg_check_modules(linux REQUIRED libdrm)
else()
    pkg_check_modules(bsd REQUIRED libinotify)
endif()
""")
        capabilities = {value.capability for value in requirements}
        self.assertIn("pkgconfig(libdrm)", capabilities)
        self.assertNotIn("pkgconfig(libinotify)", capabilities)
        self.assertNotIn("pkgconfig(epoll-shim)", capabilities)
        self.assertNotIn("cmake(GTest)", capabilities)
        self.assertIn(
            (">=", "0.14.0"),
            next(value for value in requirements if value.managed == "hyprutils").constraints,
        )

    def test_unknown_branches_are_conservative(self):
        values = list(active_declarations(commands("""
if(SOMETHING_UNKNOWN)
    find_package(first REQUIRED)
else()
    find_package(second REQUIRED)
endif()
""", "fixture")))
        self.assertEqual([value[1][0] for value in values], ["first", "second"])

    def test_debug_tracy_and_non_linux_branches_excluded(self):
        values = list(active_declarations(commands("""
if(CMAKE_BUILD_TYPE MATCHES Debug OR CMAKE_BUILD_TYPE MATCHES DEBUG)
    find_package(debug REQUIRED)
elseif(USE_TRACY)
    find_package(tracy REQUIRED)
elseif(CMAKE_SYSTEM_NAME MATCHES "FreeBSD|NetBSD")
    find_package(bsd REQUIRED)
else()
    find_package(release REQUIRED)
endif()
""", "fixture")))
        self.assertEqual([value[1][0] for value in values], ["release"])

    def test_fetchcontent_promoted_to_repository_dependency(self):
        values = self.scan("""
find_package(glaze 7...<8 QUIET)
FetchContent_Declare(glaze GIT_REPOSITORY https://example.invalid/glaze)
""")
        glaze = next(value for value in values if value.capability == "cmake(glaze)")
        self.assertEqual(glaze.constraints, ((">=", "7"), ("<", "8")))

    def test_unsupported_dynamic_requirement_stops(self):
        with self.assertRaisesRegex(PipelineError, "dynamic"):
            self.scan("pkg_check_modules(deps REQUIRED ${COMPUTED_DEPENDENCIES})")

    def test_required_program_and_lua_adapters(self):
        values = self.scan("""
find_program(SCANNER scanner REQUIRED)
pkg_search_module(LUA REQUIRED IMPORTED_TARGET lua55 lua>=5.5)
""")
        self.assertIn("/usr/bin/scanner", [value.capability for value in values])
        self.assertIn("lua", [value.managed for value in values])
        with self.assertRaises(PipelineError):
            self.scan("pkg_search_module(X REQUIRED one two)")

    def test_balanced_parser_ignores_comments(self):
        values = list(commands("""
# find_package(fake REQUIRED)
#[[ find_package(fake2 REQUIRED) ]]
pkg_check_modules(deps # inner comment
    REQUIRED "pixman-1")
""", "fixture"))
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0][1], ["deps", "REQUIRED", "pixman-1"])

    def test_trace_new_requirement_blocks(self):
        trace = self.root / "trace.jsonl"
        trace.write_text(json.dumps({
            "cmd": "pkg_check_modules",
            "args": ["deps", "REQUIRED", "hyprutils>=0.14.0"],
            "file": "/work/BUILD/project/CMakeLists.txt",
            "line": 42,
        }) + "\n")
        with self.assertRaisesRegex(PipelineError, "undeclared"):
            check_trace(trace, [])
        check_trace(trace, [pkg_requirement("hyprutils>=0.14.0", "fixture").to_dict()])

    def test_trace_new_cmake_requirement_blocks(self):
        trace = self.root / "trace.jsonl"
        trace.write_text(json.dumps({
            "cmd": "find_package",
            "args": ["newlibrary", "2.0", "REQUIRED"],
            "file": "/work/BUILD/project/CMakeLists.txt",
        }) + "\n")
        with self.assertRaises(PipelineError):
            check_trace(trace, [])

    def test_empty_and_malformed_traces_block(self):
        trace = self.root / "trace.jsonl"
        for content in (
            "",
            '{"version": {"major": 1, "minor": 2}}\n',
            "not-json\n",
            "[]\n",
            '{"cmd": "find_package", "args": "not-an-array"}\n',
        ):
            with self.subTest(content=content):
                trace.write_text(content)
                with self.assertRaises(PipelineError):
                    check_trace(trace, [])

    def test_protocol_cmake_adapter_tracks_required_dependencies(self):
        values = self.scan("""
cmake_minimum_required(VERSION 3.20)
project(hyprland-protocols LANGUAGES C)
pkg_check_modules(extra REQUIRED fixture>=1.2.0)
""", name="hyprland-protocols")
        self.assertIn("gcc", {value.capability for value in values})
        self.assertNotIn("meson", {value.capability for value in values})
        self.assertIn(
            pkg_requirement("fixture>=1.2.0", "CMakeLists.txt:4"),
            values,
        )

    def test_protocol_meson_adapter_rejects_new_dependencies(self):
        path = self.root / "meson.build"
        declaration = "project('hyprland-protocols', meson_version: '>=0.58.0')\n"
        path.write_text(declaration)
        source = {
            "name": "hyprland-protocols",
            "directory": str(self.root),
        }
        requirements = discover(source)
        self.assertEqual(requirements[0].capability, "meson")
        self.assertEqual(requirements[0].constraints, ((">=", "0.58.0"),))
        path.write_text(declaration + "dependency('unexpected')\n")
        with self.assertRaisesRegex(PipelineError, "Meson layout changed"):
            discover(source)


class SourceTests(TemporaryTest):
    def test_release_filtering_and_numeric_order(self):
        values = [
            {"tag_name": tag, "draft": draft, "prerelease": prerelease}
            for tag, draft, prerelease in (
                ("v0.9.9", False, False),
                ("v0.10.0", False, False),
                ("v0.11.0-rc1", False, False),
                ("v0.12.0", True, False),
                ("v0.13.0", False, True),
            )
        ]
        with patch("hyprland_copr.sources.download", return_value=json.dumps(values).encode()):
            self.assertEqual(
                [value["version"] for value in stable_releases("org/repo")],
                ["0.10.0", "0.9.9"],
            )

    def test_reproducible_archive_excludes_git_in_submodules(self):
        source = self.root / "source"
        (source / "subprojects/child").mkdir(parents=True)
        (source / ".git").mkdir()
        (source / ".git/secret").write_text("not exported")
        (source / "subprojects/child/.git").write_text("gitdir: elsewhere")
        (source / "subprojects/child/code").write_text("included")
        first, second = self.root / "a.tar.gz", self.root / "b.tar.gz"
        archive_tree(source, first, "component-1.0", 123)
        archive_tree(source, second, "component-1.0", 123)
        self.assertEqual(sha256(first), sha256(second))
        with tarfile.open(first) as archive:
            self.assertTrue(any(name.endswith("/code") for name in archive.getnames()))
            self.assertFalse(any(".git" in name for name in archive.getnames()))
            self.assertTrue(all(value.mtime == 123 for value in archive.getmembers()))

    def test_submodule_url_rejected_before_update(self):
        (self.root / ".gitmodules").write_text("placeholder")
        with patch("hyprland_copr.sources.git", return_value=(
            "submodule.bad.path=subprojects/bad\nsubmodule.bad.url=file:///etc"
        )) as git:
            with self.assertRaises(PipelineError):
                populate_submodules(self.root)
            self.assertEqual(git.call_count, 1)

    def test_lua_checksum_mismatch_stops(self):
        with patch("hyprland_copr.sources.download", return_value=b"tampered"):
            with self.assertRaisesRegex(PipelineError, "checksum"):
                Sources(self.root).acquire("lua", {
                    "version": "5.5.1",
                    "url": "https://www.lua.org/ftp/lua-5.5.1.tar.gz",
                    "sha256": "0" * 64,
                })


class PolicyTests(TemporaryTest):
    def test_default_config_targets_guarded_byte0_publication(self):
        config = load_config()
        self.assertEqual(config["copr"]["owner"], "byte0")
        self.assertEqual(config["copr"]["project"], "hyprland")
        self.assertTrue(config["copr"]["publish"])
        self.assertTrue(config["copr"]["exclusive_writer"])

    def test_extra_repo_rejected(self):
        path = self.root / "bad.toml"
        path.write_text((ROOT / "config/project.toml").read_text().replace(
            '"updates",', '"updates",\n    "untrusted",'
        ))
        with self.assertRaisesRegex(PipelineError, "policy"):
            load_config(path)

    def test_provider_intersects_all_version_bounds(self):
        container = Mock()
        container.exec.side_effect = ["one\ntwo", "two\nthree"]
        providers = FedoraProviders(container)
        value = Requirement("cmake(glaze)", ((">=", "7"), ("<", "8")))
        self.assertEqual(providers.find(value), ["two"])
        self.assertEqual(providers.find(value), ["two"])
        self.assertEqual(container.exec.call_count, 2)
        self.assertIn("--repo=fedora", REPO_OPTIONS)
        self.assertIn("--repo=updates", REPO_OPTIONS)
        self.assertNotIn(
            "--setopt=optional_metadata_types=filelists",
            container.exec.call_args.args[0],
        )
        container.exec.side_effect = ["hyprland-protocols-0:0.7.0-1.fc44.noarch"]
        self.assertEqual(
            providers.query("/usr/share/hyprland-protocols/protocols/hyprland-input-capture-v1.xml"),
            ["hyprland-protocols-0:0.7.0-1.fc44.noarch"],
        )
        self.assertIn(
            "--setopt=optional_metadata_types=filelists",
            container.exec.call_args.args[0],
        )

    def test_builder_image_uses_native_podman_layer_caching(self):
        with patch("hyprland_copr.fedora.run", side_effect=["", "sha256:fixed-image"]) as run:
            image = build_image(load_config(), self.root / "image.log")
        self.assertEqual(image, "sha256:fixed-image")
        command = run.call_args_list[0].args[0]
        self.assertEqual(command[:3], ["podman", "--remote=false", "build"])
        self.assertIn("--layers", command)
        self.assertNotIn("--provenance=false", command)

    def test_state_lock_excludes_concurrent_runs(self):
        with lock(self.root):
            with self.assertRaisesRegex(PipelineError, "Another"):
                with lock(self.root):
                    self.fail("Second writer acquired the lock")

    def test_offline_validation_mounts_are_read_only_and_networkless(self):
        with patch("hyprland_copr.fedora.run") as run:
            offline_run(
                "test-image",
                self.root,
                ["python3", "/pipeline/validate.py"],
                log=self.root / "validation.log",
                readonly_mounts=[(self.root / "tests", "/tests")],
                user=False,
                readonly_work=True,
            )
        arguments = run.call_args.args[0]
        self.assertIn("--network=none", arguments)
        self.assertIn("--cap-drop=ALL", arguments)
        self.assertIn("--security-opt=no-new-privileges", arguments)
        self.assertIn(f"{self.root}:/work:ro,z", arguments)
        self.assertIn(f"{self.root / 'tests'}:/tests:ro,z", arguments)
        self.assertIn(f"{ROOT / 'container'}:/pipeline:ro,z", arguments)

    def test_container_mounts_keep_selinux_enforcing_with_shared_project_labels(self):
        with patch("hyprland_copr.fedora.run") as run:
            with Container("image", [
                (self.root, "/work", False),
                (self.root / "rpms", "/repo", True),
            ]):
                pass
        arguments = run.call_args_list[0].args[0]
        self.assertIn(f"{self.root}:/work:z", arguments)
        self.assertIn(f"{self.root / 'rpms'}:/repo:ro,z", arguments)
        self.assertIn("--security-opt=no-new-privileges", arguments)

    def test_root_build_is_rejected_before_container_launch(self):
        with (
            patch("hyprland_copr.fedora.os.getuid", return_value=0),
            patch("hyprland_copr.fedora.run") as run,
            self.assertRaisesRegex(PipelineError, "non-root"),
        ):
            offline_run("image", self.root, [], log=self.root / "build.log")
        run.assert_not_called()


class ResolverTests(unittest.TestCase):
    def resolve(self, requirements, available):
        sources = Mock()
        sources.acquire.side_effect = lambda name, candidate: {
            "name": name,
            "version": candidate["version"],
        }
        sources.releases.return_value = [
            {"version": "0.14.1"},
            {"version": "0.13.0"},
        ]
        providers = Mock()
        providers.find.side_effect = lambda value: (
            ["fedora-provider"] if value.capability in available else []
        )
        providers.available.return_value = []
        with patch("hyprland_copr.resolver.discover", side_effect=lambda source: requirements.get(
            source["name"], []
        )):
            return Resolver(sources, providers).resolve({"version": "0.56.2"})

    def test_hypr_sources_even_when_fedora_has_a_provider(self):
        result = self.resolve({
            "hyprland": [Requirement("pkgconfig(hyprutils)", managed="hyprutils")],
        }, {"pkgconfig(hyprutils)"})
        self.assertEqual(result["order"], ["hyprutils", "hyprland"])
        self.assertEqual(result["resolutions"][0]["provider"], "source")

    def test_non_hypr_dependencies_remain_fedora_first(self):
        result = self.resolve({
            "hyprland": [
                Requirement("pkgconfig(aquamarine)", managed="aquamarine"),
                Requirement("pkgconfig(pixman-1)"),
            ],
        }, {
            "pkgconfig(aquamarine)",
            "pkgconfig(pixman-1)",
        })
        self.assertEqual(result["order"], ["hyprland"])
        self.assertTrue(all(value["provider"] == "fedora" for value in result["resolutions"]))

    def test_missing_managed_library_built_first(self):
        result = self.resolve({
            "hyprland": [Requirement(
                "pkgconfig(hyprutils)", ((">=", "0.14.0"),), managed="hyprutils"
            )],
        }, set())
        self.assertEqual(result["order"], ["hyprutils", "hyprland"])
        self.assertEqual(result["status"], "resolved")

    def test_missing_unmanaged_dependency_blocks(self):
        result = self.resolve({
            "hyprland": [Requirement("cmake(unmanaged)", origin="CMakeLists.txt:42")],
        }, set())
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["blockers"][0]["origin"], "CMakeLists.txt:42")

    def test_incompatible_managed_constraints_block(self):
        result = self.resolve({
            "hyprland": [Requirement(
                "pkgconfig(hyprutils)", ((">=", "99.0"),), managed="hyprutils"
            )],
        }, set())
        self.assertEqual(result["status"], "blocked")


class BundleTests(TemporaryTest):
    def fixture(self):
        for folder in ("srpms", "rpms"):
            (self.root / folder).mkdir()
        source = {
            "name": "lua",
            "version": "5.5.1",
            "sha256": "a" * 64,
        }
        files = {
            "srpms/lua55.src.rpm": b"source",
            "rpms/lua55.rpm": b"binary",
        }
        for relative, content in files.items():
            (self.root / relative).write_bytes(content)
        resolution = {
            "order": ["lua"],
        }
        save_json(self.root / "resolution.json", resolution)
        manifest = {
            "schema_version": 1,
            "status": "validated",
            "target": "fedora-44-x86_64",
            "root": "lua",
            "files": {
                path: sha256(self.root / path)
                for path in (*files, "resolution.json")
            },
            "builds": [{
                "component": "lua",
                "srpm": "srpms/lua55.src.rpm",
                "packages": ["rpms/lua55.rpm"],
                "source": source,
            }],
            "packages": [{
                "name": "lua55",
                "epoch": "0",
                "version": "5.5.1",
                "release": "1.fc44",
                "arch": "x86_64",
                "file": "rpms/lua55.rpm",
            }],
            "resolution_digest": digest_json(resolution),
            "validation": {
                "hardware_session_tested": False,
            },
        }
        save_json(self.root / "manifest.json", manifest)
        return manifest

    def test_valid_bundle(self):
        self.fixture()
        self.assertEqual(verify_bundle(self.root)["root"], "lua")

    def test_tampered_binary_blocks(self):
        self.fixture()
        (self.root / "rpms/lua55.rpm").write_bytes(b"changed")
        with self.assertRaisesRegex(PipelineError, "integrity"):
            verify_bundle(self.root)

    def test_missing_checksum_reference_blocks(self):
        manifest = self.fixture()
        del manifest["files"]["srpms/lua55.src.rpm"]
        save_json(self.root / "manifest.json", manifest)
        with self.assertRaisesRegex(PipelineError, "checksummed"):
            verify_bundle(self.root)

    def test_traversal_and_symlink_escape_block(self):
        manifest = self.fixture()
        manifest["files"]["../outside.rpm"] = "0" * 64
        save_json(self.root / "manifest.json", manifest)
        with self.assertRaises(PipelineError):
            verify_bundle(self.root)

    def test_incomplete_validation_blocks(self):
        manifest = self.fixture()
        manifest["status"] = "built"
        save_json(self.root / "manifest.json", manifest)
        with self.assertRaises(PipelineError):
            verify_bundle(self.root)

    def test_blocked_resolution_never_builds_image(self):
        with patch("hyprland_copr.build.build_image") as build:
            with self.assertRaises(PipelineError):
                Builder(load_config(), self.root).build({"status": "blocked"}, self.root / "out")
            build.assert_not_called()

    def test_validate_imports_pinned_pubkey_when_present(self):
        manifest = self.fixture()
        (self.root / "logs").mkdir(exist_ok=True)
        (self.root / "pubkey.gpg").write_bytes(b"public-key")
        with (
            patch("hyprland_copr.build.Container") as container_cls,
            patch("hyprland_copr.build.offline_run"),
        ):
            container = container_cls.return_value.__enter__.return_value
            container.exec.return_value = ""
            Builder(load_config(), self.root).validate(self.root, manifest, "sha256:image")
        self.assertEqual(
            container.exec.call_args_list[0].args[0],
            ["rpm", "--import", "/work/pubkey.gpg"],
        )


class PackagingTests(TemporaryTest):
    def source(self):
        archive = self.root / "lua-5.5.1.tar.gz"
        archive.write_bytes(b"fixture")
        return {
            "name": "lua",
            "version": "5.5.1",
            "epoch": 0,
            "commit": "",
            "directory": str(self.root),
            "archive": str(archive),
            "sha256": hashlib.sha256(b"fixture").hexdigest(),
        }

    def test_lua_uses_versioned_interfaces(self):
        text = render_spec(self.source(), [], 1)
        for required in (
            "Name:           lua55",
            "liblua5.5.so.0",
            "lua55.pc",
            "%{_includedir}/lua%{lua_abi}",
        ):
            self.assertIn(required, text)
        self.assertNotIn("@BUILD_REQUIRES@", text)
        self.assertNotIn("Obsoletes:", text)
        self.assertNotIn("Provides:       lua ", text)

    def test_archive_checksum_verified_before_staging(self):
        source = self.source()
        source["sha256"] = "0" * 64
        with self.assertRaisesRegex(PipelineError, "integrity"):
            prepare(source, [], 1, self.root / "work")

    def test_installed_lua_module_probes_write_only_to_temporary_work(self):
        script = (ROOT / "tests/lua55/validate-installed.sh").read_text()
        self.assertIn('-o "$work/packaging_probe.so"', script)
        self.assertIn('> "$work/packaging_lua_probe.lua"', script)
        self.assertNotIn('-o "$cmod/', script)
        self.assertNotIn('> "$lmod/', script)
        self.assertIn('assert(package.path:find(arg[2] .. "/?.lua", 1, true))', script)
        self.assertIn('assert(package.cpath:find(arg[3] .. "/?.so", 1, true))', script)
        self.assertIn('package.cpath = arg[1] .. "/?.so;" .. package.cpath', script)
        self.assertIn("rpm -V lua lua-libs lua-devel lua55 lua55-libs lua55-devel", script)


if __name__ == "__main__":
    unittest.main()
