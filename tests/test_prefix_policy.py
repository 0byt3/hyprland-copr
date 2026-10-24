import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

from hyprland_copr.build import Builder, check_trace, verify_bundle
from hyprland_copr.config import ROOT, load_config
from hyprland_copr.dependencies import discover, pkg_requirement
from hyprland_copr.model import (
    PipelineError,
    Requirement,
    SOURCE_POLICY,
    managed_project,
    normalize_requirement,
    source_repository,
)
from hyprland_copr.packaging import render_spec
from hyprland_copr.resolver import Resolver
from hyprland_copr.sources import Sources
from hyprland_copr.util import digest_json, save_json, sha256


class PrefixPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_capabilities_packages_and_program_aliases(self):
        for capability, expected in (
            ("pkgconfig(hyprnovel)", "hyprnovel"),
            ("cmake(HyprNovel)", "hyprnovel"),
            ("hyprnovel-devel", "hyprnovel"),
            ("hyprnovel-libs", "hyprnovel"),
            ("/usr/bin/hyprnovel", "hyprnovel"),
            ("/usr/bin/hyprwire-scanner", "hyprwire"),
            ("cmake(hyprwire-scanner)", "hyprwire"),
            ("hyprwire-scanner-devel", "hyprwire"),
            ("cmake(glaze)", "glaze"),
            ("pkgconfig(aquamarine)", "aquamarine"),
            ("pkgconfig(pixman-1)", ""),
        ):
            with self.subTest(capability=capability):
                self.assertEqual(managed_project(capability), expected)
                self.assertEqual(normalize_requirement(Requirement(capability)).managed, expected)

    def test_official_repository_derivation_and_explicit_exceptions(self):
        self.assertEqual(source_repository("hyprnovel"), "hyprwm/hyprnovel")
        self.assertEqual(source_repository("hypr-new"), "hyprwm/hypr-new")
        self.assertEqual(source_repository("hyprland"), "hyprwm/Hyprland")
        self.assertEqual(source_repository("glaze"), "stephenberry/glaze")
        for name in (
            "../hyprnovel",
            "hyprnovel/../../elsewhere",
            "hyprnovel.git",
            "hyprnovel;id",
            "hyprnovel-devel",
            "hyprwire-scanner",
            "unmanaged",
        ):
            with self.subTest(name=name), self.assertRaises(PipelineError):
                source_repository(name)

    def test_unsafe_capabilities_and_conflicting_owners_fail(self):
        for capability in ("pkgconfig(hypr../../evil)", "/usr/bin/hyprnovel;id"):
            with self.subTest(capability=capability), self.assertRaises(PipelineError):
                normalize_requirement(Requirement(capability))
        with self.assertRaisesRegex(PipelineError, "Conflicting"):
            normalize_requirement(Requirement("pkgconfig(hyprnovel)", managed="hyprutils"))

    def test_safe_new_project_reaches_official_release_discovery(self):
        sources = Sources(self.root)
        with patch("hyprland_copr.sources.stable_releases", return_value=[]) as releases:
            self.assertEqual(sources.releases("hyprnovel"), [])
            releases.assert_called_once_with("hyprwm/hyprnovel", 10)

    def test_missing_official_repository_names_the_project(self):
        with patch(
            "hyprland_copr.sources.stable_releases",
            side_effect=urllib.error.HTTPError("https://api.github.com", 404, "Not found", {}, None),
        ):
            with self.assertRaisesRegex(PipelineError, "hyprwm/hyprnovel"):
                Sources(self.root).releases("hyprnovel")

    def test_unsafe_source_identity_rejected_before_paths_or_network(self):
        sources = Sources(self.root)
        with patch("hyprland_copr.sources.git") as git:
            for name, candidate in (
                ("../hyprnovel", {"version": "1.0.0"}),
                ("hyprnovel", {"version": "1.0.0", "tag": "--upload-pack=evil"}),
                ("hyprnovel", {
                    "version": "1.0.0",
                    "tag": "v1.0.0",
                    "repository": "untrusted/hyprnovel",
                }),
            ):
                with self.subTest(name=name, candidate=candidate), self.assertRaises(PipelineError):
                    sources.acquire(name, candidate)
            git.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_spaced_constraints_seed_and_trace(self):
        (self.root / "CMakeLists.txt").write_text("""
set(MINIMUM 0.14.2)
set(DEPS "hyprutils >= ${MINIMUM}" "hyprlang >= 0.6.0")
pkg_check_modules(deps REQUIRED IMPORTED_TARGET ${DEPS})
find_package(hyprnovel 1.2 REQUIRED)
find_program(SCANNER hyprwire-scanner REQUIRED)
""")
        requirements = discover({"name": "hyprtoolkit", "directory": str(self.root)})
        utils = next(value for value in requirements if value.capability == "pkgconfig(hyprutils)")
        self.assertEqual(utils.constraints, ((">=", "0.14.2"),))
        self.assertIn("hyprnovel", {value.managed for value in requirements})
        self.assertIn("hyprwire", {value.managed for value in requirements})
        self.assertIn("pkgconfig(wayland-scanner)", {value.capability for value in requirements})
        trace = self.root / "trace.jsonl"
        trace.write_text(json.dumps({
            "cmd": "pkg_check_modules",
            "args": ["deps", "REQUIRED", "hyprutils >= 0.14.2", "hyprlang >= 0.6.0"],
            "file": "/work/BUILD/hyprtoolkit/CMakeLists.txt",
        }) + "\n")
        check_trace(trace, [value.to_dict() for value in requirements])
        self.assertEqual(pkg_requirement(" hyprutils \t>= 0.14.2 ", "").constraints, utils.constraints)
        with self.assertRaises(PipelineError):
            pkg_requirement("hyprutils >= 0.14.2 unexpected", "")

    def test_optional_hypr_probes_are_managed_but_other_optional_probes_are_not(self):
        (self.root / "CMakeLists.txt").write_text("""
pkg_check_modules(optional QUIET "hyprnovel >= 1.2" epoll-shim)
find_package(hyprnewlib 1.1 QUIET)
find_package(unmanaged QUIET)
find_program(SCANNER hyprwire-scanner)
""")
        requirements = discover({"name": "hyprutils", "directory": str(self.root)})
        self.assertTrue({
            "hyprnovel",
            "hyprnewlib",
            "hyprwire",
        }.issubset({value.managed for value in requirements}))
        self.assertNotIn("pkgconfig(epoll-shim)", {value.capability for value in requirements})
        self.assertNotIn("cmake(unmanaged)", {value.capability for value in requirements})
        for command, arguments in (
            ("pkg_check_modules", ["optional", "QUIET", "hyprnovel >= 1.2"]),
            ("find_package", ["hyprnewlib", "1.1", "QUIET"]),
            ("find_program", ["SCANNER", "hyprwire-scanner"]),
        ):
            trace = self.root / "trace.jsonl"
            trace.write_text(json.dumps({
                "cmd": command,
                "args": arguments,
                "file": "/work/BUILD/project/CMakeLists.txt",
            }) + "\n")
            with self.subTest(command=command):
                with self.assertRaisesRegex(PipelineError, "undeclared"):
                    check_trace(trace, [])
                check_trace(trace, [value.to_dict() for value in requirements])

    def test_hypr_names_alternatives_fail_in_seed_and_expanded_trace(self):
        for command, arguments in (
            ("find_package", ["Generic", "QUIET", "NAMES", "hyprnovel"]),
            ("find_program", ["SCANNER", "NAMES", "generic", "hyprnovel"]),
        ):
            (self.root / "CMakeLists.txt").write_text(
                'set(NAME hyprnovel)\n'
                + f"{command}("
                + " ".join("${NAME}" if value == "hyprnovel" else value for value in arguments)
                + ")\n"
            )
            with self.subTest(command=command):
                with self.assertRaisesRegex(PipelineError, "hypr NAMES alternatives"):
                    discover({"name": "hyprutils", "directory": str(self.root)})
                trace = self.root / "trace.jsonl"
                trace.write_text(json.dumps({
                    "cmd": command,
                    "args": arguments,
                    "file": "/work/BUILD/project/CMakeLists.txt",
                }) + "\n")
                with self.assertRaisesRegex(PipelineError, "hypr NAMES alternatives"):
                    check_trace(trace, [])

    def test_program_probe_uses_expanded_name_not_hint_paths(self):
        (self.root / "CMakeLists.txt").write_text("""
set(TOOL hyprnovel)
find_program(TOOL_PATH ${TOOL})
find_program(OPTIONAL_HELPER helper HINTS /tmp/hypr-tools)
""")
        requirements = discover({"name": "hyprutils", "directory": str(self.root)})
        self.assertIn("/usr/bin/hyprnovel", {value.capability for value in requirements})
        self.assertNotIn("/usr/bin/helper", {value.capability for value in requirements})
        trace = self.root / "trace.jsonl"
        trace.write_text(json.dumps({
            "cmd": "find_program",
            "args": ["OPTIONAL_HELPER", "helper", "HINTS", "/tmp/hypr-tools"],
            "file": "/work/BUILD/project/CMakeLists.txt",
        }) + "\n")
        check_trace(trace, [])

    def resolve(self, requirements, candidates=None):
        sources = Mock()
        sources.acquire.side_effect = lambda name, candidate: {
            "name": name,
            "version": candidate["version"],
        }
        sources.releases.return_value = candidates or [{"version": "2.0.0"}, {"version": "1.5.0"}]
        providers = Mock()
        providers.find.return_value = ["fedora-provider"]
        providers.available.return_value = []
        with patch("hyprland_copr.resolver.discover", side_effect=lambda source: requirements.get(
            source["name"], []
        )):
            return Resolver(sources, providers).resolve({"version": "1.0.0"})

    def test_recursive_unknown_projects_are_source_built_despite_fedora(self):
        result = self.resolve({
            "hyprland": [Requirement("pkgconfig(hyprnovel)")],
            "hyprnovel": [
                Requirement("cmake(hyprnewlib)", ((">=", "1.2"),)),
                Requirement("/usr/bin/hyprwire-scanner"),
                Requirement("hyprnewlib-devel", (("<", "2"),)),
                Requirement("pkgconfig(aquamarine)"),
            ],
        })
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["source_policy"], SOURCE_POLICY)
        self.assertEqual(result["sources"]["hyprnewlib"]["version"], "1.5.0")
        self.assertLess(result["order"].index("hyprnewlib"), result["order"].index("hyprnovel"))
        self.assertLess(result["order"].index("hyprnovel"), result["order"].index("hyprland"))
        self.assertNotIn("aquamarine", result["sources"])
        self.assertTrue(all(
            value["provider"] == ("source" if value["managed"].startswith("hypr") else "fedora")
            for value in result["resolutions"]
        ))

    def test_incompatible_hypr_sources_block_instead_of_using_fedora(self):
        result = self.resolve({
            "hyprland": [
                Requirement("pkgconfig(hyprnovel)", ((">=", "9"),), "first"),
                Requirement("hyprnovel-devel", (("<", "2"),), "second"),
            ],
        })
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(all("No compatible" in value["reason"] for value in result["blockers"]))

    def source(self, name="hyprnovel"):
        directory = self.root / name
        (directory / "include" / name).mkdir(parents=True)
        (directory / "CMakeLists.txt").write_text(f"add_library({name} SHARED src.cpp)\n")
        (directory / f"{name}.pc.in").write_text("Version: @VERSION@\n")
        (directory / "LICENSE").write_text(
            (ROOT / "tests/fixtures/LICENSE.bsd-3-clause").read_text()
        )
        return {
            "name": name,
            "version": "1.0.0",
            "directory": str(directory),
            "sha256": "a" * 64,
            "epoch": 123,
            "commit": "a" * 40,
        }

    def test_new_library_uses_reviewed_layout_and_license(self):
        text = render_spec(self.source(), [], 1)
        self.assertIn("https://github.com/hyprwm/hyprnovel", text)
        self.assertIn("License:        BSD-3-Clause\n", text)
        self.assertIn("%{_libdir}/pkgconfig/*.pc", text)
        self.assertIn("Provides:       %{name}-libs = %{version}-%{release}", text)
        requirements = [Requirement("hyprnovel-libs", ((">=", "1.0.0"),)).to_dict()]
        consumer = render_spec(self.source("hyprconsumer"), requirements, 1)
        self.assertIn("BuildRequires:  hyprnovel-libs >= 1.0.0", consumer)

    def test_unsupported_library_layout_and_license_fail_explicitly(self):
        source = self.source()
        root = Path(source["directory"])
        (root / "CMakeLists.txt").write_text("add_executable(hyprnovel src.cpp)\n")
        with self.assertRaisesRegex(PipelineError, "layout"):
            render_spec(source, [], 1)
        (root / "CMakeLists.txt").write_text("add_library(hyprnovel SHARED src.cpp)\n")
        (root / "LICENSE").write_text("Unreviewed license\n")
        with self.assertRaisesRegex(PipelineError, "license"):
            render_spec(source, [], 1)
        (root / "LICENSE").write_text((ROOT / "tests/fixtures/LICENSE.bsd-3-clause").read_text())
        (root / "embedded.xml").write_text("Additional notice needing review")
        with self.assertRaisesRegex(PipelineError, "notices"):
            render_spec(source, [], 1)

    def test_toolkit_keeps_protocol_notice_and_no_graphical_tests(self):
        text = render_spec(self.source("hyprtoolkit"), [], 1)
        self.assertIn("License:        BSD-3-Clause AND HPND-sell-variant", text)
        self.assertIn("%license LICENSE protocols/wlr-layer-shell-unstable-v1.xml", text)
        self.assertIn("-DBUILD_TESTING=OFF", text)
        self.assertNotIn("simpleWindow", text)

    def test_scanner_rpm_aliases_are_provided_by_the_owning_source(self):
        text = render_spec(self.source("hyprwire"), [], 1)
        for capability in ("hyprwire-scanner", "hyprwire-scanner-libs", "hyprwire-scanner-devel"):
            self.assertIn(f"Provides:       {capability} = %{{version}}-%{{release}}", text)

    def bundle(self):
        source = {
            "name": "hyprtoolkit",
            "version": "1.0.0",
            "sha256": "a" * 64,
        }
        resolution = {
            "source_policy": SOURCE_POLICY,
            "order": ["hyprtoolkit"],
            "sources": {"hyprtoolkit": source},
            "requirements": {"hyprtoolkit": []},
        }
        for relative in ("srpms/hyprtoolkit.src.rpm", "rpms/hyprtoolkit.rpm"):
            path = self.root / relative
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b"fixture")
        manifest = {
            "schema_version": 1,
            "source_policy": SOURCE_POLICY,
            "status": "validated",
            "target": "fedora-44-x86_64",
            "root": "hyprtoolkit",
            "builds": [{
                "component": "hyprtoolkit",
                "srpm": "srpms/hyprtoolkit.src.rpm",
                "packages": ["rpms/hyprtoolkit.rpm"],
                "source": copy.deepcopy(source),
            }],
            "packages": [{
                "name": "hyprtoolkit",
                "epoch": "0",
                "version": "1.0.0",
                "release": "1.fc44",
                "arch": "x86_64",
                "file": "rpms/hyprtoolkit.rpm",
            }],
            "validation": {"hardware_session_tested": False},
        }
        return manifest, resolution

    def save_bundle(self, manifest, resolution):
        save_json(self.root / "resolution.json", resolution)
        manifest["resolution_digest"] = digest_json(resolution)
        manifest["files"] = {
            relative: sha256(self.root / relative)
            for relative in (
                "srpms/hyprtoolkit.src.rpm",
                "rpms/hyprtoolkit.rpm",
                "resolution.json",
            )
        }
        save_json(self.root / "manifest.json", manifest)

    def test_bundle_checks_policy_source_provenance_and_hypr_closure(self):
        manifest, resolution = self.bundle()
        self.save_bundle(manifest, resolution)
        verify_bundle(self.root)
        resolution["requirements"]["hyprtoolkit"] = [Requirement("hyprnovel-devel").to_dict()]
        self.save_bundle(manifest, resolution)
        with self.assertRaisesRegex(PipelineError, "required hypr source"):
            verify_bundle(self.root)
        resolution["requirements"]["hyprtoolkit"] = []
        manifest["builds"][0]["source"]["version"] = "0.9.0"
        self.save_bundle(manifest, resolution)
        with self.assertRaisesRegex(PipelineError, "source provenance"):
            verify_bundle(self.root)
        manifest["source_policy"] = None
        self.save_bundle(manifest, resolution)
        with self.assertRaisesRegex(PipelineError, "source policy"):
            verify_bundle(self.root)

    def test_bundle_accepts_a_new_safe_hypr_component_without_a_name_allowlist(self):
        manifest, resolution = self.bundle()
        source = {
            "name": "hyprnovel",
            "version": "1.5.0",
            "sha256": "b" * 64,
        }
        (self.root / "srpms/hyprnovel.src.rpm").write_bytes(b"source")
        (self.root / "rpms/hyprnovel.rpm").write_bytes(b"binary")
        resolution["order"].insert(0, "hyprnovel")
        resolution["sources"]["hyprnovel"] = source
        resolution["requirements"]["hyprnovel"] = []
        resolution["requirements"]["hyprtoolkit"] = [
            Requirement("pkgconfig(hyprnovel)", ((">=", "1.2"),)).to_dict()
        ]
        manifest["builds"].insert(0, {
            "component": "hyprnovel",
            "srpm": "srpms/hyprnovel.src.rpm",
            "packages": ["rpms/hyprnovel.rpm"],
            "source": source,
        })
        manifest["packages"].append({
            **manifest["packages"][0],
            "name": "hyprnovel",
            "version": "1.5.0",
            "file": "rpms/hyprnovel.rpm",
        })
        self.save_bundle(manifest, resolution)
        for relative in ("srpms/hyprnovel.src.rpm", "rpms/hyprnovel.rpm"):
            manifest["files"][relative] = sha256(self.root / relative)
        save_json(self.root / "manifest.json", manifest)
        verify_bundle(self.root)

    def test_build_rejects_old_saved_resolution_before_creating_output(self):
        with self.assertRaisesRegex(PipelineError, "older source policy"):
            Builder(load_config(), self.root).build(
                {"status": "resolved"}, self.root / "bundle"
            )
        self.assertFalse((self.root / "bundle").exists())


if __name__ == "__main__":
    unittest.main()
