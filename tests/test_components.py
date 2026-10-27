import copy
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from container import smoke
from hyprland_copr.build import verify_bundle
from hyprland_copr.cli import already_published, execute, parser
from hyprland_copr.components import APPLICATIONS, PORTAL_PREP, protocol_build_system
from hyprland_copr.config import ROOT, load_config
from hyprland_copr.dependencies import discover
from hyprland_copr.model import (
    COMPANIONS,
    MANAGED_CAPABILITIES,
    PipelineError,
    REPOSITORIES,
    Requirement,
    SOURCE_POLICY,
    root_package,
    rpm_release,
    source_versions,
)
from hyprland_copr.packaging import prepare, render_spec
from hyprland_copr.publish import Publisher
from hyprland_copr.resolver import Resolver
from hyprland_copr.sources import Sources, validate_source_version
from hyprland_copr.util import digest_json, read_json, save_json, sha256


class ComponentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def source(self, name, version="1.0.0"):
        directory = self.root / name
        directory.mkdir(exist_ok=True)
        archive = self.root / f"{name}-{version}.tar.gz"
        archive.write_bytes(b"unit-test-fixture")
        return {
            "name": name,
            "version": version,
            "directory": str(directory),
            "archive": str(archive),
            "sha256": sha256(archive),
            "epoch": 123,
            "commit": "a" * 40,
        }

    def test_new_sources_and_component_selection(self):
        for name in (*COMPANIONS, "glaze", "hyprtoolkit"):
            with self.subTest(name=name):
                self.assertIn(name, REPOSITORIES)
                self.assertEqual(parser().parse_args(["run", "--component", name]).component, name)
        self.assertEqual(MANAGED_CAPABILITIES["cmake(glaze)"], "glaze")
        self.assertEqual(root_package("glaze"), "glaze-devel")
        self.assertIsNone(parser().parse_args(["run"]).component)

    def test_glaze_uses_literal_cmake_project_version(self):
        (self.root / "CMakeLists.txt").write_text(
            "project(glaze\n    VERSION 7.9.1\n    LANGUAGES CXX)\n"
        )
        validate_source_version("glaze", self.root, "7.9.1")
        with self.assertRaises(PipelineError):
            validate_source_version("glaze", self.root, "9.0.0")
        (self.root / "CMakeLists.txt").write_text("project(glaze VERSION ${COMPUTED})")
        with self.assertRaises(PipelineError):
            validate_source_version("glaze", self.root, "7.9.1")

    def test_uwsm_uses_version_sh_and_meson_adapter(self):
        (self.root / "version.sh").write_text("#!/bin/sh\nVERSION=0.27.1\necho \"$VERSION\"\n")
        validate_source_version("uwsm", self.root, "0.27.1")
        with self.assertRaises(PipelineError):
            validate_source_version("uwsm", self.root, "0.26.0")
        (self.root / "meson.build").write_text(
            "project('uwsm', version: '0.27.1', license: 'MIT', meson_version: '>=1.3.0')\n"
        )
        requirements = discover({
            "name": "uwsm",
            "directory": str(self.root),
        })
        capabilities = {value.capability for value in requirements}
        self.assertIn("meson", capabilities)
        self.assertIn("pkgconfig(scdoc)", capabilities)
        self.assertIn("python3-dbus", capabilities)
        self.assertIn("python3-pyxdg", capabilities)
        spec = render_spec(
            self.source("uwsm", "0.27.1"),
            [value.to_dict() for value in requirements],
            1,
        )
        self.assertIn("BuildArch:      noarch", spec)
        self.assertIn("URL:            https://github.com/Vladimir-csp/uwsm", spec)
        self.assertIn("-Duwsm-app=enabled", spec)
        self.assertIn("export PYTHONDONTWRITEBYTECODE=1", spec)

    def test_glaze_disabled_developer_features_are_not_dependencies(self):
        for relative in (
            "cmake/prelude.cmake",
            "cmake/project-is-top-level.cmake",
            "cmake/variables.cmake",
            "cmake/install-rules.cmake",
            "cmake/FindAsio.cmake",
        ):
            path = self.root / relative
            path.parent.mkdir(exist_ok=True)
            path.write_text("")
        (self.root / "cmake/FindAsio.cmake").write_text("find_package(Boost REQUIRED)")
        (self.root / "CMakeLists.txt").write_text("""
cmake_minimum_required(VERSION 3.21)
if(glaze_EETF_FORMAT)
    find_package(Erlang REQUIRED)
endif()
if(glaze_ENABLE_SSL)
    find_package(OpenSSL REQUIRED)
endif()
""")
        requirements = discover({
            "name": "glaze",
            "directory": str(self.root),
        })
        capabilities = {value.capability for value in requirements}
        self.assertNotIn("cmake(Boost)", capabilities)
        self.assertNotIn("cmake(Erlang)", capabilities)
        self.assertNotIn("libglvnd-devel", capabilities)
        self.assertIn("cmake-filesystem", capabilities)

    def test_portal_qt_widgets_and_system_fallback_requirements(self):
        (self.root / "CMakeLists.txt").write_text("""
find_package(QT NAMES Qt6 REQUIRED COMPONENTS Widgets)
find_package(Qt6 REQUIRED COMPONENTS Widgets)
""")
        requirements = discover({
            "name": "xdg-desktop-portal-hyprland",
            "directory": str(self.root),
        })
        capabilities = {value.capability for value in requirements}
        self.assertIn("cmake(Qt6Widgets)", capabilities)
        self.assertNotIn("cmake(QT)", capabilities)
        self.assertIn("pkgconfig(sdbus-c++)", capabilities)
        self.assertIn("pkgconfig(hyprland-protocols)", capabilities)
        protocols = next(
            value for value in requirements if value.capability == "pkgconfig(hyprland-protocols)"
        )
        self.assertFalse(protocols.accepts("0.6.0"))
        self.assertTrue(protocols.accepts("0.7.0"))
        self.assertIn(
            "/usr/share/hyprland-protocols/protocols/hyprland-input-capture-v1.xml",
            capabilities,
        )
        self.assertIn(
            "/usr/share/hyprland-protocols/protocols/hyprland-toplevel-mapping-v1.xml",
            capabilities,
        )
        self.assertIn("qt6-qtwayland", capabilities)
        self.assertIn("grim", capabilities)
        self.assertIn("slurp", capabilities)
        spec = render_spec(
            self.source("xdg-desktop-portal-hyprland", "1.4.1"),
            [value.to_dict() for value in requirements],
            1,
        )
        self.assertIn("BuildRequires:  pkgconfig(hyprland-protocols) >= 0.7.0", spec)
        self.assertNotIn("BuildRequires:  /usr/share/hyprland-protocols/protocols/", spec)
        self.assertIn(
            "test -s %{_datadir}/hyprland-protocols/protocols/hyprland-toplevel-mapping-v1.xml",
            spec,
        )

    def test_hidden_paper_and_pam_dependencies_are_checked(self):
        (self.root / "CMakeLists.txt").write_text("find_package(Threads REQUIRED)")
        for component, expected in (
            ("hyprpaper", {
                "/usr/bin/hyprwire-scanner",
                "pkgconfig(opengl)",
                "pkgconfig(glesv2)",
                "pkgconfig(libmagic)",
                "pkgconfig(wayland-cursor)",
            }),
            ("hyprlock", {
                "pam-devel",
                "pam",
                "pkgconfig(wayland-scanner)",
            }),
            ("hyprpicker", {
                "pkgconfig(wayland-cursor)",
                "pkgconfig(wayland-scanner)",
            }),
            ("hyprsunset", {
                "pkgconfig(wayland-cursor)",
                "pkgconfig(wayland-scanner)",
            }),
        ):
            with self.subTest(component=component):
                requirements = discover({
                    "name": component,
                    "directory": str(self.root),
                })
                self.assertTrue(expected.issubset({value.capability for value in requirements}))

    def test_application_specs_have_reviewed_files_and_no_activation(self):
        self.assertEqual(set(APPLICATIONS), set(COMPANIONS))
        for name in COMPANIONS:
            with self.subTest(name=name):
                text = render_spec(self.source(name), [], 1)
                self.assertIn("Name:           " + name, text)
                for asset in APPLICATIONS[name]["files"]:
                    self.assertIn(asset, text)
                self.assertNotIn("systemctl", text)
                self.assertNotIn("%systemd_user_post", text)
                self.assertIn("timeout 10", text)
                self.assertNotIn("%{_libdir}/lib*.so", text)

    def test_hyprlock_policy_is_staged_and_noreplace(self):
        source = self.source("hyprlock")
        spec = prepare(source, [], 1, self.root / "work")
        policy = self.root / "work/SOURCES/hyprlock.pam"
        self.assertIn("auth       include      system-auth", policy.read_text())
        self.assertNotIn("include login", policy.read_text())
        self.assertIn("%config(noreplace) %{_sysconfdir}/pam.d/hyprlock", spec.read_text())
        self.assertIn("-DHYPRLOCK_COMMIT=" + source["commit"], spec.read_text())

    def test_glaze_header_only_spec_and_staged_consumer(self):
        spec = prepare(self.source("glaze", "7.9.1"), [], 1, self.root / "work")
        text = spec.read_text()
        self.assertIn("Name:           glaze", text)
        self.assertIn("BuildArch:      noarch", text)
        self.assertIn("%files devel", text)
        self.assertIn("-Dglaze_INSTALL_CMAKEDIR:STRING=share/cmake/glaze", text)
        self.assertNotIn("lib*.so", text)
        self.assertNotIn("Requires:       %{name}", text)
        self.assertTrue((self.root / "work/SOURCES/glaze-smoke.cpp").is_file())
        self.assertTrue((self.root / "work/SOURCES/glaze-smoke.cmake").is_file())

    def test_protocol_specs_select_matching_meson_and_cmake_commands(self):
        for version, filename, content, expected, excluded in (
            (
                "0.7.0",
                "meson.build",
                "project('hyprland-protocols', meson_version: '>=0.58.0')\n",
                "%meson --wrap-mode=nodownload",
                "%cmake_build",
            ),
            (
                "0.8.0",
                "CMakeLists.txt",
                "cmake_minimum_required(VERSION 3.20)\nproject(hyprland-protocols LANGUAGES C)\n",
                "%cmake_build",
                "%meson_build",
            ),
        ):
            with self.subTest(version=version):
                source = self.source("hyprland-protocols", version)
                directory = self.root / version
                directory.mkdir()
                source["directory"] = str(directory)
                (directory / filename).write_text(content)
                requirements = discover(source)
                text = render_spec(source, [value.to_dict() for value in requirements], 1)
                self.assertIn(expected, text)
                self.assertNotIn(excluded, text)
                self.assertIn("pkg-config --variable=pkgdatadir hyprland-protocols", text)
                if filename == "CMakeLists.txt":
                    self.assertIn("cmake-rpm-macros", text)
                    self.assertIn("-DCMAKE_INSTALL_DATADIR:STRING=share", text)
                    self.assertIn("--trace-redirect=%{_topdir}/trace.jsonl", text)
                    self.assertNotIn("meson", {value.capability for value in requirements})

    def test_protocol_adapter_rejects_missing_or_ambiguous_build_layouts(self):
        with self.assertRaisesRegex(PipelineError, "missing or ambiguous"):
            protocol_build_system(self.root)
        (self.root / "CMakeLists.txt").write_text("project(hyprland-protocols)\n")
        (self.root / "meson.build").write_text("project('hyprland-protocols')\n")
        with self.assertRaisesRegex(PipelineError, "missing or ambiguous"):
            discover({"name": "hyprland-protocols", "directory": str(self.root)})

    def test_portal_prep_requires_system_dependencies_and_fails_on_layout_changes(self):
        path = self.root / "CMakeLists.txt"
        path.write_text(
            "pkg_check_modules(SDBUS IMPORTED_TARGET sdbus-c++>=2.0.0)\n"
            "pkg_check_modules(HYPRLAND_PROTOS IMPORTED_TARGET hyprland-protocols)\n"
        )
        program = PORTAL_PREP.split("\n", 1)[1].rsplit("\n", 1)[0]
        subprocess.run(["python3", "-c", program], cwd=self.root, check=True)
        self.assertIn("SDBUS REQUIRED IMPORTED_TARGET", path.read_text())
        self.assertIn("HYPRLAND_PROTOS REQUIRED IMPORTED_TARGET", path.read_text())
        self.assertIn("hyprland-protocols>=0.7.0", path.read_text())
        result = subprocess.run(
            ["python3", "-c", program], cwd=self.root, capture_output=True, text=True
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("review packaging", result.stderr)

    def test_shared_glaze_major_bound_and_all_primary_roots(self):
        sources = Mock()
        sources.acquire.side_effect = lambda name, candidate: self.source(name, candidate["version"])
        sources.releases.side_effect = lambda name, limit: (
            [
                {"version": "9.0.0"},
                {"version": "8.4.0"},
                {"version": "7.9.1"},
            ] if name == "glaze" else [{"version": "1.0.0"}]
        )
        providers = Mock()
        providers.find.return_value = []
        providers.available.return_value = []
        requirements = {
            "hyprland": [Requirement(
                "cmake(glaze)", ((">=", "7"), ("<", "8")), managed="glaze"
            )],
            "hyprlock": [Requirement(
                "cmake(glaze)", ((">=", "7.9.0"),), managed="glaze"
            )],
        }
        with patch("hyprland_copr.resolver.discover", side_effect=lambda source: requirements.get(
            source["name"], []
        )):
            result = Resolver(sources, providers).resolve(
                {"version": "0.56.2"}, include_companions=True
            )
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["sources"]["glaze"]["version"], "7.9.1")
        self.assertEqual(result["primaries"], ["hyprland", *COMPANIONS])
        self.assertLess(result["order"].index("glaze"), result["order"].index("hyprland"))
        self.assertLess(result["order"].index("glaze"), result["order"].index("hyprlock"))

    def test_toolkit_is_managed_without_an_explicit_requirement_mapping(self):
        sources = Mock()
        sources.acquire.side_effect = lambda name, candidate: self.source(name, candidate["version"])
        sources.releases.return_value = [{"version": "0.6.0"}]
        providers = Mock()
        providers.find.return_value = ["fedora-provider"]
        providers.available.return_value = []
        with patch(
            "hyprland_copr.resolver.discover",
            side_effect=lambda source: [
                Requirement("pkgconfig(hyprtoolkit)", ((">=", "0.4.1"),))
            ] if source["name"] == "hyprpaper" else [],
        ):
            result = Resolver(sources, providers).resolve(
                {"version": "0.8.4"}, root="hyprpaper"
            )
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["order"], ["hyprtoolkit", "hyprpaper"])
        sources.releases.assert_called_once_with("hyprtoolkit", 10)

    def test_companion_protocol_source_fallback_and_pinning(self):
        for pinned in (False, True):
            with self.subTest(pinned=pinned):
                sources = Mock()
                application = self.source("hypridle", "0.1.8")
                if pinned:
                    (Path(application["directory"]) / "subprojects/hyprland-protocols").mkdir(
                        parents=True
                    )
                protocols = self.source("hyprland-protocols", "0.7.0")
                sources.acquire.side_effect = lambda name, candidate: (
                    application if name == "hypridle" else protocols
                )
                sources.releases.return_value = [{"version": "0.7.0"}]
                sources.protocols.return_value = protocols
                providers = Mock()
                providers.find.return_value = []
                providers.available.return_value = []
                with patch(
                    "hyprland_copr.resolver.discover",
                    side_effect=lambda source: [Requirement(
                        "pkgconfig(hyprland-protocols)", ((">=", "0.6.0"),),
                        managed="hyprland-protocols",
                    )] if source["name"] == "hypridle" else [],
                ):
                    result = Resolver(sources, providers).resolve(
                        {"version": "0.1.8"}, root="hypridle"
                    )
                self.assertEqual(result["order"], ["hyprland-protocols", "hypridle"])
                if pinned:
                    sources.protocols.assert_called_once_with(application)
                    sources.releases.assert_not_called()
                else:
                    sources.releases.assert_called_once_with("hyprland-protocols", 10)

    def test_portal_missing_xml_rebuilds_protocols_even_with_a_versioned_provider(self):
        sources = Mock()
        name = "xdg-desktop-portal-hyprland"
        application = self.source(name, "1.4.1")
        (Path(application["directory"]) / "subprojects/hyprland-protocols").mkdir(
            parents=True
        )
        protocols = self.source("hyprland-protocols", "0.7.0")
        sources.acquire.return_value = application
        sources.protocols.return_value = protocols
        missing_xml = "/usr/share/hyprland-protocols/protocols/hyprland-input-capture-v1.xml"
        requirements = [
            Requirement(
                "pkgconfig(hyprland-protocols)", ((">=", "0.7.0"),),
                managed="hyprland-protocols",
            ),
            Requirement(missing_xml, managed="hyprland-protocols"),
        ]
        providers = Mock()
        providers.find.side_effect = lambda requirement: (
            [] if requirement.capability == missing_xml else ["fedora-provider"]
        )
        with patch(
            "hyprland_copr.resolver.discover",
            side_effect=lambda source: requirements if source["name"] == name else [],
        ):
            result = Resolver(sources, providers).resolve(
                {"version": "1.4.1"}, root=name
            )
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["order"], ["hyprland-protocols", name])
        sources.protocols.assert_called_once_with(application)
        sources.releases.assert_not_called()

    def test_incompatible_protocol_pin_falls_back_to_compatible_stable_release(self):
        sources = Mock()
        application = self.source("hypridle", "0.1.8")
        (Path(application["directory"]) / "subprojects/hyprland-protocols").mkdir(
            parents=True
        )
        pinned = self.source("hyprland-protocols", "0.5.0")
        stable = self.source("hyprland-protocols", "0.7.0")
        sources.protocols.return_value = pinned
        sources.releases.return_value = [{"version": "0.7.0"}]
        sources.acquire.side_effect = lambda name, candidate: (
            application if name == "hypridle" else stable
        )
        providers = Mock()
        providers.find.return_value = []
        providers.available.return_value = []
        with patch(
            "hyprland_copr.resolver.discover",
            side_effect=lambda source: [Requirement(
                "pkgconfig(hyprland-protocols)", ((">=", "0.6.0"),),
                managed="hyprland-protocols",
            )] if source["name"] == "hypridle" else [],
        ):
            result = Resolver(sources, providers).resolve(
                {"version": "0.1.8"}, root="hypridle"
            )
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["sources"]["hyprland-protocols"]["version"], "0.7.0")
        sources.protocols.assert_called_once_with(application)
        sources.releases.assert_called_once_with("hyprland-protocols", 10)
        sources.acquire.assert_any_call("hyprland-protocols", {"version": "0.7.0"})

    def stack(self):
        return {
            "source_policy": SOURCE_POLICY,
            "root": "hyprland",
            "primaries": ["hyprland", *COMPANIONS],
            "generation": 9,
            "sources": {
                name: {
                    "version": "0.56.2" if name == "hyprland" else "1.0.0",
                }
                for name in ("hyprland", *COMPANIONS, "glaze")
            },
        }

    def test_cli_retains_stack_and_standalone_resolutions(self):
        config = load_config()
        state = self.root / "state"
        config["pipeline"]["state_directory"] = str(state)
        stack = {
            **self.stack(),
            "status": "blocked",
            "blockers": [{
                "consumer": "hyprpaper",
                "capability": "pkgconfig(hyprtoolkit)",
                "constraints": [[">=", "0.4.1"]],
                "origin": "CMakeLists.txt:53",
                "reason": "Missing unmanaged dependency",
            }],
        }
        standalone = {
            "root": "hypridle",
            "primaries": ["hypridle"],
            "status": "resolved",
            "sources": {"hypridle": {"version": "0.1.8"}},
            "order": ["hypridle"],
            "blockers": [],
        }
        with (
            patch("hyprland_copr.cli.load_config", return_value=config),
            patch("hyprland_copr.cli.Container"),
            patch("hyprland_copr.cli.Sources") as sources,
            patch("hyprland_copr.cli.Resolver") as resolver,
        ):
            sources.return_value.releases.return_value = [{"version": "0.56.2"}]
            resolver.return_value.resolve.return_value = stack
            self.assertEqual(execute(parser().parse_args([
                "resolve", "--publication-state", str(self.root / "publication.json"),
            ])), 3)
            sources.return_value.releases.return_value = [{"version": "0.1.8"}]
            resolver.return_value.resolve.return_value = standalone
            self.assertEqual(execute(parser().parse_args([
                "resolve", "--component", "hypridle",
                "--publication-state", str(self.root / "publication.json"),
            ])), 0)
        self.assertEqual(read_json(state / "resolution-stack.json"), stack)
        self.assertEqual(read_json(state / "resolution-hypridle.json"), standalone)
        self.assertEqual(read_json(state / "resolution.json"), standalone)

    def test_skip_requires_complete_source_versions_and_release_generation(self):
        resolution = self.stack()
        config = load_config()
        published = {
            "source_policy": SOURCE_POLICY,
            "status": "promoted",
            "destination": [
                config["copr"]["owner"],
                config["copr"]["project"],
                config["copr"]["chroot"],
            ],
            "root": resolution["root"],
            "source_versions": source_versions(resolution),
            "primaries": resolution["primaries"],
            "generation": 9,
            "root_release": rpm_release(resolution, config["pipeline"]["release"]) + ".fc44",
        }
        self.assertTrue(already_published(config, resolution, published))
        self.assertFalse(already_published(config, resolution, {
            **published,
            "source_policy": None,
        }))
        self.assertFalse(already_published(config, resolution, {
            **published,
            "source_policy": "fedora-first-v1",
        }))
        self.assertFalse(already_published(config, resolution, {
            **published,
            "destination": ["a-different-owner", "a-different-project", "fedora-44-x86_64"],
        }))
        for name in (*COMPANIONS, "glaze"):
            with self.subTest(name=name):
                changed = copy.deepcopy(resolution)
                changed["sources"][name]["version"] = "1.0.1"
                self.assertFalse(already_published(config, changed, published))
        self.assertFalse(already_published(config, resolution, {
            **published,
            "source_versions": None,
        }))
        config["pipeline"]["release"] += 1
        self.assertFalse(already_published(config, resolution, published))

    def test_stack_generation_changes_releases_of_unchanged_libraries(self):
        resolution = self.stack()
        self.assertEqual(rpm_release(resolution, 1), "1.hypr0.56.2.stack9")
        resolution["generation"] = 10
        self.assertEqual(rpm_release(resolution, 1), "1.hypr0.56.2.stack10")
        resolution["generation"] = True
        with self.assertRaises(PipelineError):
            rpm_release(resolution, 1)

    def test_glaze_bundle_root_has_devel_binary_and_glaze_srpm(self):
        source = self.source("glaze", "7.9.1")
        bundle = self.root / "bundle"
        for directory in ("rpms", "srpms"):
            (bundle / directory).mkdir(parents=True)
        binary = "rpms/glaze-devel-7.9.1-1.fc44.noarch.rpm"
        srpm = "srpms/glaze-7.9.1-1.fc44.src.rpm"
        for relative in (binary, srpm):
            (bundle / relative).write_bytes(b"unit-test-fixture")
        resolution = {
            "root": "glaze",
            "order": ["glaze"],
            "primaries": ["glaze"],
            "generation": 1,
        }
        save_json(bundle / "resolution.json", resolution)
        manifest = {
            "schema_version": 1,
            "status": "validated",
            "target": "fedora-44-x86_64",
            "root": "glaze",
            "primaries": ["glaze"],
            "generation": 1,
            "resolution_digest": digest_json(resolution),
            "builds": [{
                "component": "glaze",
                "source": source,
                "srpm": srpm,
                "packages": [binary],
            }],
            "packages": [{
                "file": binary,
                "name": "glaze-devel",
                "epoch": "0",
                "version": "7.9.1",
                "release": "1.fc44",
                "arch": "noarch",
            }],
            "files": {
                relative: sha256(bundle / relative)
                for relative in (binary, srpm, "resolution.json")
            },
            "validation": {
                "hardware_session_tested": False,
            },
        }
        save_json(bundle / "manifest.json", manifest)
        verify_bundle(bundle)
        config = load_config()
        config["copr"]["owner"] = "operator"
        config["copr"]["project"] = "hyprland-stack"
        publisher = Publisher(config, bundle, self.root / "publication.json")
        self.assertEqual(publisher.components["glaze"], "glaze")
        self.assertEqual(publisher.root_release, "1.fc44")
        self.assertEqual(publisher.source_versions, {"glaze": "7.9.1"})
        manifest["generation"] = 2
        save_json(bundle / "manifest.json", manifest)
        with self.assertRaisesRegex(PipelineError, "generation"):
            verify_bundle(bundle)

    def test_guiutils_is_a_required_source_built_hyprland_runtime(self):
        (self.root / "CMakeLists.txt").write_text("find_package(Threads REQUIRED)")
        requirements = discover({"name": "hyprland", "directory": str(self.root)})
        dialog = next(value for value in requirements if value.capability == "/usr/bin/hyprland-dialog")
        self.assertEqual(dialog.managed, "hyprland-guiutils")
        source = self.source("hyprland-guiutils", "0.2.2")
        spec = render_spec(source, [], 1)
        self.assertIn("hyprland-dialog --help", spec)
        self.assertNotIn("--version", spec)
        self.assertIn("Requires:       hyprland-guiutils", (ROOT / "packaging/hyprland.spec.in").read_text())

    def test_guiutils_smoke_checks_version_from_help_without_opening_windows(self):
        with (
            patch.object(smoke, "check_assets"),
            patch.object(smoke.subprocess, "run", return_value=Mock(
                stdout="hyprland-dialog, part of hyprland-guiutils v0.2.2", stderr="",
            )) as run,
        ):
            smoke.check_applications([{"name": "hyprland-guiutils", "version": "0.2.2"}])
            run.assert_called_once()
            self.assertEqual(run.call_args.args[0], ["/usr/bin/hyprland-dialog", "--help"])
            run.return_value.stdout = "hyprland-guiutils v0.1.0"
            with self.assertRaisesRegex(RuntimeError, "unexpected version"):
                smoke.check_applications([{"name": "hyprland-guiutils", "version": "0.2.2"}])

    def test_smoke_never_runs_picker_or_connects_a_real_session(self):
        with (
            patch.object(smoke, "check_assets"),
            patch.dict(os.environ, {
                "WAYLAND_DISPLAY": "do-not-connect",
                "DISPLAY": "do-not-connect",
                "DBUS_SESSION_BUS_ADDRESS": "do-not-connect",
            }),
            patch.object(smoke.subprocess, "run", return_value=Mock(
                stdout="xdg-desktop-portal-hyprland v1.4.1",
                stderr="",
            )) as run,
        ):
            smoke.check_applications([{
                "name": "xdg-desktop-portal-hyprland",
                "version": "1.4.1",
            }])
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                ["/usr/libexec/xdg-desktop-portal-hyprland", "--help"],
                ["/usr/libexec/xdg-desktop-portal-hyprland", "--version"],
            ],
        )
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["timeout"], 10)
            self.assertNotIn("WAYLAND_DISPLAY", call.kwargs["env"])
            self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", call.kwargs["env"])

    def test_hyprland_spec_has_private_runtime_for_its_version_check(self):
        source = self.source("hyprland", "0.56.2")
        udis = Path(source["directory"]) / "subprojects/udis86"
        udis.mkdir(parents=True)
        (udis / "configure.ac").write_text("AC_INIT([udis86], [1.7.2])\n")
        text = render_spec(source, [], 1)
        check = text.split("%check\n", 1)[1].split("%files", 1)[0]
        self.assertIn("chmod 700 .smoke-home/runtime", check)
        self.assertIn('export XDG_RUNTIME_DIR="$HOME/runtime"', check)
        self.assertIn("unset WAYLAND_DISPLAY DISPLAY DBUS_SESSION_BUS_ADDRESS", check)
        self.assertIn("HYPRLAND_INSTANCE_SIGNATURE", check)
        self.assertIn("timeout 10 %{buildroot}%{_bindir}/Hyprland --version", check)

    def test_hyprland_smoke_uses_private_runtime_and_never_connects_a_session(self):
        def verify(arguments, **options):
            self.assertEqual(arguments, ["Hyprland", "--version"])
            environment = options["env"]
            runtime = Path(environment["XDG_RUNTIME_DIR"])
            self.assertTrue(runtime.is_dir())
            self.assertEqual(runtime.stat().st_mode & 0o777, 0o700)
            self.assertNotEqual(environment["HOME"], "/ambient-home")
            self.assertNotEqual(environment["XDG_CONFIG_HOME"], "/ambient-config")
            for variable in (
                "WAYLAND_DISPLAY",
                "DISPLAY",
                "DBUS_SESSION_BUS_ADDRESS",
                "HYPRLAND_INSTANCE_SIGNATURE",
            ):
                self.assertNotIn(variable, environment)
            self.assertTrue(options["check"])
            self.assertEqual(options["timeout"], 10)
            return Mock(stdout="Hyprland v0.56.2", stderr="")

        with (
            patch.dict(os.environ, {
                "HOME": "/ambient-home",
                "XDG_CONFIG_HOME": "/ambient-config",
                "XDG_RUNTIME_DIR": "/ambient-runtime",
                "WAYLAND_DISPLAY": "do-not-connect",
                "DISPLAY": "do-not-connect",
                "DBUS_SESSION_BUS_ADDRESS": "do-not-connect",
                "HYPRLAND_INSTANCE_SIGNATURE": "do-not-connect",
            }),
            patch.object(smoke.subprocess, "run", side_effect=verify) as run,
        ):
            smoke.check_hyprland("0.56.2")
        runtime = Path(run.call_args.kwargs["env"]["XDG_RUNTIME_DIR"])
        self.assertFalse(runtime.exists())

    def test_hyprland_smoke_rejects_wrong_version_and_failed_command(self):
        with patch.object(smoke.subprocess, "run", return_value=Mock(
            stdout="Hyprland v0.55.0",
            stderr="",
        )):
            with self.assertRaisesRegex(RuntimeError, "unexpected version"):
                smoke.check_hyprland("0.56.2")
        with patch.object(smoke.subprocess, "run", side_effect=subprocess.CalledProcessError(
            1, ["Hyprland", "--version"]
        )):
            with self.assertRaises(subprocess.CalledProcessError):
                smoke.check_hyprland("0.56.2")

    def test_glaze_smoke_reads_versioned_provide_from_the_installed_package(self):
        with (
            patch.object(smoke.shutil, "copytree"),
            patch.object(smoke.subprocess, "run", return_value=Mock(
                stdout="cmake(glaze) = 7.9.1\nglaze-devel = 7.9.1-1.fc44\n"
            )) as run,
        ):
            smoke.check_glaze([{"name": "glaze-devel", "version": "7.9.1"}])
        self.assertEqual(run.call_args_list[0].args[0], [
            "rpm", "-q", "--provides", "glaze-devel",
        ])
        self.assertEqual(len(run.call_args_list), 4)

    def test_glaze_smoke_rejects_missing_or_wrong_versioned_provide(self):
        for metadata in ("cmake(glaze)\n", "cmake(glaze) = 9.0.0\n", ""):
            with (
                self.subTest(metadata=metadata),
                patch.object(smoke.subprocess, "run", return_value=Mock(
                    stdout=metadata
                )) as run,
            ):
                with self.assertRaisesRegex(RuntimeError, "versioned CMake provide"):
                    smoke.check_glaze([{"name": "glaze-devel", "version": "7.9.1"}])
                run.assert_called_once()

    def test_smoke_assets_cover_the_reviewed_rpm_file_lists(self):
        macros = {
            "%{_bindir}": "/usr/bin",
            "%{_libexecdir}": "/usr/libexec",
            "%{_userunitdir}": "/usr/lib/systemd/user",
            "%{_datadir}": "/usr/share",
            "%{_mandir}": "/usr/share/man",
            "%{_sysconfdir}": "/etc",
            "%config(noreplace) ": "",
            "hyprpicker.1*": "hyprpicker.1.gz",
        }
        for name in COMPANIONS:
            if name == "uwsm":
                self.assertIn("/usr/bin/uwsm-app", smoke.APPLICATION_ASSETS[name])
                self.assertIn("/usr/lib/systemd/user/wayland-wm@.service", smoke.APPLICATION_ASSETS[name])
                self.assertIn("/usr/share/uwsm/plugins/hyprland.sh", smoke.APPLICATION_ASSETS[name])
                continue
            expected = set()
            for entry in APPLICATIONS[name]["files"]:
                for macro, value in macros.items():
                    entry = entry.replace(macro, value)
                expected.add(entry)
            self.assertEqual(
                expected, {smoke.APPLICATION_BINARIES[name], *smoke.APPLICATION_ASSETS[name]}
            )

    def test_pinned_protocol_archives_are_isolated_and_canonically_staged(self):
        sources = Sources(self.root / "sources")
        records = []
        for index, commit in enumerate(("a" * 40, "b" * 40)):
            directory = self.root / f"primary-{index}"
            protocols = directory / "subprojects/hyprland-protocols"
            protocols.mkdir(parents=True)
            (protocols / "VERSION").write_text("0.7.0\n")
            (protocols / "meson.build").write_text(
                "project('hyprland-protocols', meson_version: '>=0.58.0')\n"
            )
            (protocols / "data").write_text(str(index))
            with patch("hyprland_copr.sources.git", side_effect=[commit, "123"]):
                records.append(sources.protocols({
                    "directory": str(directory),
                    "epoch": index,
                }))
        first, second = records
        self.assertNotEqual(first["archive"], second["archive"])
        self.assertEqual(sha256(first["archive"]), first["sha256"])
        prepare(first, [], 1, self.root / "work")
        self.assertTrue((self.root / "work/SOURCES/hyprland-protocols-0.7.0.tar.gz").is_file())

    def test_service_asset_requires_exact_activation_binary(self):
        binary = self.root / "usr/bin/hypridle"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"fixture")
        binary.chmod(0o755)
        service = self.root / "usr/lib/systemd/user/hypridle.service"
        service.parent.mkdir(parents=True)
        example = self.root / "usr/share/hypr/hypridle.conf"
        example.parent.mkdir(parents=True)
        example.write_text("fixture")
        for command in (
            "ExecStart=/usr/bin/hypridle",
            "ExecStart=/usr/bin/hypridle --quiet",
        ):
            service.write_text("[Service]\n" + command + "\n")
            smoke.check_assets("hypridle", self.root)
        for command in (
            "ExecStart=/usr/bin/hypridle-incorrect",
            "NotExecStart=/usr/bin/hypridle",
            "# ExecStart=/usr/bin/hypridle",
        ):
            service.write_text("[Service]\n" + command + "\n")
            with self.assertRaisesRegex(RuntimeError, "unexpected binary"):
                smoke.check_assets("hypridle", self.root)

    def test_pam_asset_rejects_unsafe_modes_and_wrong_includes(self):
        binary = self.root / "usr/bin/hyprlock"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"fixture")
        binary.chmod(0o755)
        policy = self.root / "etc/pam.d/hyprlock"
        policy.parent.mkdir(parents=True)
        policy.write_text((ROOT / "packaging/hyprlock.pam").read_text())
        policy.chmod(0o644)
        example = self.root / "usr/share/hypr/hyprlock.conf"
        example.parent.mkdir(parents=True)
        example.write_text("fixture")
        smoke.check_assets("hyprlock", self.root)
        policy.write_text("auth include login\n")
        with self.assertRaises(RuntimeError):
            smoke.check_assets("hyprlock", self.root)
        binary.chmod(0o4755)
        with self.assertRaisesRegex(RuntimeError, "permissions"):
            smoke.check_assets("hyprlock", self.root)


if __name__ == "__main__":
    unittest.main()
