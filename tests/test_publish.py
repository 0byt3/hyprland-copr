"""Offline publisher tests: no COPR requests, credentials, or Docker required."""

import copy
import gzip
import hashlib
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from hyprland_copr import publish
from hyprland_copr.model import PipelineError, SOURCE_POLICY
from hyprland_copr.util import digest_json, read_json, save_json, sha256


def checksum(data):
    return hashlib.sha256(data).hexdigest()


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bundle = self.root / "bundle"
        (self.bundle / "rpms").mkdir(parents=True)
        (self.bundle / "srpms").mkdir()
        self.package = {
            "name": "lua55",
            "epoch": "0",
            "version": "5.5.1",
            "release": "1.fc44",
            "arch": "x86_64",
            "file": "rpms/lua55-5.5.1-1.fc44.x86_64.rpm",
        }
        self.srpm = "srpms/lua55-5.5.1-1.fc44.src.rpm"
        (self.bundle / self.package["file"]).write_bytes(b"local-rpm")
        (self.bundle / self.srpm).write_bytes(b"checked-srpm")
        self.manifest = {
            "schema_version": 1,
            "source_policy": SOURCE_POLICY,
            "status": "validated",
            "root": "lua",
            "builder_image": "sha256:builder",
            "packages": [self.package],
            "builds": [{
                "component": "lua",
                "source": {"version": "5.5.1"},
                "srpm": self.srpm,
                "packages": [self.package["file"]],
            }],
            "files": {
                self.package["file"]: sha256(self.bundle / self.package["file"]),
                self.srpm: sha256(self.bundle / self.srpm),
            },
        }
        self.config = {
            "copr": {
                "owner": "operator",
                "project": "hyprland-stack",
                "chroot": "fedora-44-x86_64",
                "publish": True,
                "exclusive_writer": True,
                "poll_seconds": 1,
                "timeout_seconds": 2,
            },
        }
        with patch.object(publish, "verify_bundle", return_value=self.manifest):
            self.publisher = publish.Publisher(
                self.config,
                self.bundle,
                self.root / "state.json",
            )
        self.network = patch.object(
            self.publisher, "_get", side_effect=AssertionError("Unexpected network access")
        )
        self.network.start()
        self.addCleanup(self.network.stop)
        self.artifact = {
            field: self.package[field] for field in publish.RPM_FIELDS
        }
        self.artifact.update({
            "url": self.publisher.repo + "00000007-lua55/" + publish.filename(self.package),
            "sha256": checksum(b"signed-rpm"),
        })
        self.initial = {
            "policy": {"manual": True},
            "inventory": {},
            "public": {"repomd_sha256": "0" * 64, "packages": {}},
            "devel": {"repomd_sha256": "1" * 64, "packages": {}},
            "key_sha256": checksum(b"project-public-key"),
        }
        self.inventory = {
            "id": 7,
            "state": "succeeded",
            "ownername": "operator",
            "projectname": "hyprland-stack",
            "project_dirname": "hyprland-stack",
            "chroots": ["fedora-44-x86_64"],
            "source_package": {"name": "lua55", "version": "5.5.1-1.fc44"},
        }
        self.snapshot = copy.deepcopy(self.initial)
        self.snapshot["inventory"] = {"7": self.inventory}
        self.snapshot["devel"] = {
            "repomd_sha256": "2" * 64,
            "packages": {publish.identity(self.artifact): self.artifact},
        }

    def state(self, status="submitted"):
        result = {
            "schema_version": 1,
            "source_policy": SOURCE_POLICY,
            "manifest_digest": self.publisher.digest,
            "root": "lua",
            "root_version": "5.5.1",
            "root_release": "1.fc44",
            "destination": ["operator", "hyprland-stack", "fedora-44-x86_64"],
            "status": status,
            "initial": self.initial,
            "checkpoint": self.snapshot,
            "submitted": self.snapshot,
            "builds": [{
                "component": "lua",
                "srpm": self.srpm,
                "srpm_sha256": self.manifest["files"][self.srpm],
                "id": 7,
                "status": "succeeded",
                "inventory": self.inventory,
                "artifacts": [self.artifact],
            }],
        }
        save_json(self.publisher.state_path, result)
        return result

    def validated_state(self):
        state = self.state("validated")
        destination = self.publisher.candidate_root
        (destination / "rpms").mkdir(parents=True)
        (destination / self.package["file"]).write_bytes(b"signed-rpm")
        save_json(destination / "manifest.json", {"status": "validated"})
        state["validated"] = {
            "snapshot_digest": digest_json(self.snapshot),
            "artifacts_digest": digest_json([self.artifact]),
            "candidate_manifest_sha256": sha256(destination / "manifest.json"),
        }
        save_json(self.publisher.state_path, state)
        return state

    def fake_download(self, url, destination, digest):
        data = b"project-public-key" if url == self.publisher.key_url else b"signed-rpm"
        self.assertEqual(checksum(data), digest)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)

    def test_mutations_require_explicit_single_writer_policy(self):
        for field in ("publish", "exclusive_writer"):
            with self.subTest(field=field):
                self.config["copr"][field] = False
                with self.assertRaises(PipelineError):
                    self.publisher.submit()
                with self.assertRaises(PipelineError):
                    self.publisher.promote(True)
                self.config["copr"][field] = True

    def test_recovery_state_must_match_the_bundle_source_policy(self):
        state = self.state()
        state.pop("source_policy")
        save_json(self.publisher.state_path, state)
        with self.assertRaisesRegex(PipelineError, "Recovery state"):
            self.publisher._read_state()
        state["source_policy"] = SOURCE_POLICY
        save_json(self.publisher.state_path, state)
        self.assertEqual(self.publisher._read_state()["source_policy"], SOURCE_POLICY)

    def test_legacy_bundles_cannot_be_submitted_or_promoted_directly(self):
        self.publisher.source_policy = None
        with patch.object(self.publisher, "_client") as client:
            with self.assertRaisesRegex(PipelineError, "current hypr source policy"):
                self.publisher.submit()
            with self.assertRaisesRegex(PipelineError, "current hypr source policy"):
                self.publisher.promote(True)
            client.assert_not_called()

    def test_unsafe_artifact_destinations_rejected(self):
        for url in (
            "http://download.copr.fedorainfracloud.org/results/operator/hyprland-stack/a.rpm",
            self.artifact["url"] + "?token=untrusted",
            self.publisher.base + "../another/a.rpm",
            self.publisher.base + "%2e%2e/another/a.rpm",
            self.artifact["url"].replace("download.", "evil.download.", 1),
            self.artifact["url"].replace("/hyprland-stack/", "/hyprland-stack-other/"),
        ):
            with self.subTest(url=url), self.assertRaises(PipelineError):
                self.publisher._safe_url(url)

    def test_workspace_symlinks_are_rejected_before_writes(self):
        self.publisher.candidate_root.mkdir(parents=True)
        (self.publisher.candidate_root / "rpms").symlink_to(self.bundle / "rpms", target_is_directory=True)
        with self.assertRaisesRegex(PipelineError, "Symlinks"):
            self.publisher._output(self.publisher.candidate_root / self.package["file"])

    def test_actual_copr_manual_mode_repository_projection(self):
        # Shapes from official apiv3_project_chroots.to_build_config_dict and
        # BuildConfigLogic.generate_{build_config,additional_repos}.
        project = {
            "ownername": "operator",
            "name": "hyprland-stack",
            "devel_mode": True,
            "enable_net": False,
            "additional_repos": [],
            "chroot_repos": {"fedora-44-x86_64": self.publisher.repo},
            "follow_fedora_branching": False,
            "packit_forge_projects_allowed": [],
            "storage": "backend",
            "bootstrap": None,
        }
        chroot = {
            "ownername": "operator",
            "projectname": "hyprland-stack",
            "mock_chroot": "fedora-44-x86_64",
            "comps_name": None,
            "additional_repos": [],
            "additional_packages": [],
            "additional_modules": [],
            "with_opts": [],
            "without_opts": [],
        }
        effective = {
            "chroot": "fedora-44-x86_64",
            "enable_net": False,
            "additional_repos": [
                "copr://operator/hyprland-stack",
                "copr://operator/hyprland-stack/devel",
            ],
            "additional_packages": [],
            "additional_modules": [],
            "with_opts": [],
            "without_opts": [],
            "repos": [
                {
                    "id": "copr_base",
                    "baseurl": self.publisher.repo,
                },
                {
                    "id": "copr_base_devel",
                    "baseurl": self.publisher.repo + "devel/",
                },
            ],
        }
        with patch.object(self.publisher, "_api", side_effect=[project, chroot, effective]):
            self.publisher._project_policy()
        effective["repos"].append({
            "id": "unexpected",
            "baseurl": "https://example.com/repository/",
        })
        with (
            patch.object(self.publisher, "_api", side_effect=[project, chroot, effective]),
            self.assertRaises(PipelineError),
        ):
            self.publisher._project_policy()

    def test_missing_build_directory_requires_matching_result_evidence(self):
        build = copy.deepcopy(self.inventory)
        del build["project_dirname"]
        result = {
            "result_url": self.publisher.repo + "00000007-lua55/",
            "state": "succeeded",
        }
        with patch.object(
            self.publisher, "_api", side_effect=[{"items": [build]}, result]
        ):
            self.assertEqual(self.publisher._inventory()["7"]["project_dirname"], self.publisher.project)
        result["result_url"] = self.publisher.repo.replace(
            "/hyprland-stack/", "/hyprland-stack:pr:1/"
        ) + "00000007-lua55/"
        with (
            patch.object(self.publisher, "_api", side_effect=[{"items": [build]}, result]),
            self.assertRaises(PipelineError),
        ):
            self.publisher._inventory()

    def test_recovery_state_binds_manifest_and_source_checksum(self):
        state = self.state()
        state["builds"][0]["srpm_sha256"] = "f" * 64
        save_json(self.publisher.state_path, state)
        with self.assertRaises(PipelineError):
            self.publisher._read_state()

    def test_unrelated_inventory_and_metadata_changes_block(self):
        state = self.state()
        changed = copy.deepcopy(self.snapshot)
        changed["inventory"]["8"] = {**self.inventory, "id": 8}
        with self.assertRaises(PipelineError):
            self.publisher._assert_progress(state, changed)
        changed = copy.deepcopy(self.snapshot)
        changed["devel"]["repomd_sha256"] = "f" * 64
        with self.assertRaises(PipelineError):
            self.publisher._assert_progress(state, changed)

    def test_submit_persists_id_before_wait_and_is_idempotent(self):
        client = Mock()
        client.build_proxy.create_from_file.return_value = {"id": 7}

        def waiting(actual_client, entry):
            self.assertIs(actual_client, client)
            saved = read_json(self.publisher.state_path)
            self.assertEqual(saved["builds"][0]["id"], 7)
            self.assertEqual(saved["builds"][0]["status"], "waiting")

        with (
            patch.object(self.publisher, "_client", return_value=client),
            patch.object(
                self.publisher, "_snapshot",
                side_effect=[self.initial, self.initial, self.snapshot, self.snapshot],
            ),
            patch.object(self.publisher, "_wait", side_effect=waiting),
            patch.object(
                self.publisher, "_completed_artifacts",
                return_value=([self.artifact], self.snapshot),
            ),
        ):
            result = self.publisher.submit()
            self.assertEqual(result["status"], "submitted")
            self.assertEqual(result["root_version"], "5.5.1")
            self.publisher.submit()
        client.build_proxy.create_from_file.assert_called_once_with(
            "operator",
            "hyprland-stack",
            str(self.bundle / self.srpm),
            buildopts={"chroots": ["fedora-44-x86_64"], "enable_net": False},
        )

    def test_uncertain_upload_never_resubmits_or_logs_client_exception(self):
        client = Mock()
        client.build_proxy.create_from_file.side_effect = RuntimeError("do-not-log-this")
        with (
            patch.object(self.publisher, "_client", return_value=client),
            patch.object(self.publisher, "_snapshot", return_value=self.initial),
        ):
            with self.assertRaises(PipelineError) as caught:
                self.publisher.submit()
            self.assertNotIn("do-not-log-this", str(caught.exception))
            with self.assertRaisesRegex(PipelineError, "ambiguous"):
                self.publisher.submit()
        self.assertEqual(client.build_proxy.create_from_file.call_count, 1)

    def test_explicit_credential_path_and_sanitized_initialization_errors(self):
        client = Mock()
        client.create_from_config_file.return_value.config = {
            "copr_url": publish.API,
        }
        modules = {
            "copr": SimpleNamespace(),
            "copr.v3": SimpleNamespace(Client=client),
        }
        with (
            patch.dict(sys.modules, modules),
            patch.dict(os.environ, {"COPR_CONFIG_FILE": "/protected/copr"}),
        ):
            self.publisher._client()
            client.create_from_config_file.assert_called_once_with(path="/protected/copr")
            self.assertEqual(client.call_args.args[0]["connection_attempts"], 1)
            client.create_from_config_file.side_effect = RuntimeError("private-config-content")
            with self.assertRaises(PipelineError) as caught:
                self.publisher._client()
        self.assertNotIn("private-config-content", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_authenticated_reads_do_not_expose_client_errors(self):
        method = Mock(side_effect=RuntimeError("private-response-content"))
        with self.assertRaises(PipelineError) as caught:
            self.publisher._authenticated_read(method, 7)
        method.assert_called_once_with(7)
        self.assertNotIn("private-response-content", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_validate_has_no_credentials_or_copr_import_and_calls_builder(self):
        self.state()
        self.config["copr"]["publish"] = False
        self.config["copr"]["exclusive_writer"] = False
        with (
            patch.dict(sys.modules, {"copr": None, "copr.v3": None}),
            patch.object(self.publisher, "_client", side_effect=AssertionError("Credentials accessed")),
            patch.object(self.publisher, "_snapshot", return_value=self.snapshot),
            patch.object(self.publisher, "_download", side_effect=self.fake_download),
            patch.object(self.publisher, "_check_signatures") as signatures,
            patch.object(publish, "build_image", return_value="sha256:test"),
            patch.object(publish, "Builder") as builder,
        ):
            result = self.publisher.validate()
        self.assertEqual(result["status"], "validated")
        signatures.assert_called_once()
        builder.return_value.validate.assert_called_once()
        self.assertEqual(builder.return_value.validate.call_args.kwargs["baseline_rpms"], [])
        self.assertEqual(
            (self.publisher.candidate_root / self.srpm).read_bytes(),
            b"checked-srpm",
        )

    def test_validate_rejects_state_change_during_execution(self):
        self.state()
        changed = copy.deepcopy(self.snapshot)
        changed["key_sha256"] = "f" * 64
        with (
            patch.object(self.publisher, "_snapshot", side_effect=[self.snapshot, changed]),
            patch.object(self.publisher, "_download", side_effect=self.fake_download),
            patch.object(self.publisher, "_check_signatures"),
            patch.object(publish, "build_image", return_value="sha256:test"),
            patch.object(publish, "Builder"),
            self.assertRaises(PipelineError),
        ):
            self.publisher.validate()
        self.assertEqual(read_json(self.publisher.state_path)["status"], "submitted")

    def test_signature_checks_reject_unsigned_and_unknown_keys(self):
        for response in (
            "Header SHA256 digest: OK\nPayload SHA256 digest: OK\n",
            "Header V4 RSA/SHA256 Signature, key ID deadbeef: NOKEY\n",
        ):
            with self.subTest(response=response), patch.object(publish, "Container") as container:
                container.return_value.__enter__.return_value.exec.return_value = response
                with self.assertRaises(PipelineError):
                    self.publisher._check_signatures(self.root, [self.artifact], "image")

    def test_signature_uses_isolated_trust_database_and_exact_header(self):
        def output(arguments):
            if arguments[0] == "rpmkeys":
                return "Header V4 RSA/SHA256 Signature, key ID 12345678: OK\n"
            if "-qp" in arguments:
                return "\t".join(self.package[field] for field in publish.RPM_FIELDS)
            return ""

        with patch.object(publish, "Container") as container:
            executor = container.return_value.__enter__.return_value.exec
            executor.side_effect = output
            self.publisher._check_signatures(self.root, [self.artifact], "image")
            commands = [call.args[0] for call in executor.call_args_list]
        self.assertEqual(commands[0], ["rpm", "--dbpath", "/tmp/copr-trust", "--initdb"])
        self.assertIn("_pkgverify_level signature", commands[2])

    def test_promote_requires_yes_and_unchanged_signed_bytes(self):
        self.validated_state()
        with self.assertRaisesRegex(PipelineError, "--yes"):
            self.publisher.promote()
        (self.publisher.candidate_root / self.package["file"]).write_bytes(b"tampered")
        with self.assertRaisesRegex(PipelineError, "bytes changed"):
            self.publisher.promote(True)

    def test_promote_and_retry_generate_metadata_only_once(self):
        self.validated_state()
        promoted = copy.deepcopy(self.snapshot)
        promoted["public"] = copy.deepcopy(promoted["devel"])
        promoted["public"]["repomd_sha256"] = "3" * 64
        client = Mock()
        with (
            patch.object(self.publisher, "_client", return_value=client),
            patch.object(
                self.publisher, "_snapshot",
                side_effect=[self.snapshot, self.snapshot, promoted, promoted],
            ),
        ):
            self.assertEqual(self.publisher.promote(True)["status"], "promoted")
            self.assertEqual(self.publisher.promote(True)["status"], "promoted")
        client.project_proxy.regenerate_repos.assert_called_once_with("operator", "hyprland-stack")

    def test_promote_refuses_last_moment_inventory_change(self):
        self.validated_state()
        changed = copy.deepcopy(self.snapshot)
        changed["inventory"]["8"] = {**self.inventory, "id": 8}
        client = Mock()
        with (
            patch.object(self.publisher, "_client", return_value=client),
            patch.object(self.publisher, "_snapshot", side_effect=[self.snapshot, changed]),
            self.assertRaises(PipelineError),
        ):
            self.publisher.promote(True)
        client.project_proxy.regenerate_repos.assert_not_called()

    def test_metadata_parser_checks_hashes_and_build_specific_locations(self):
        primary = (
            '<metadata xmlns="http://linux.duke.edu/metadata/common">'
            '<package><name>lua55</name><arch>x86_64</arch>'
            '<version epoch="0" ver="5.5.1" rel="1.fc44"/>'
            f'<checksum type="sha256">{self.artifact["sha256"]}</checksum>'
            f'<location href="../00000007-lua55/{publish.filename(self.package)}"/>'
            '</package></metadata>'
        ).encode()
        compressed = gzip.compress(primary)
        repomd = (
            '<repomd xmlns="http://linux.duke.edu/metadata/repo">'
            '<data type="primary"><location href="repodata/primary.xml.gz"/>'
            f'<checksum type="sha256">{checksum(compressed)}</checksum>'
            '</data></repomd>'
        ).encode()
        base = self.publisher.repo + "devel/"
        responses = {
            base + "repodata/repomd.xml": repomd,
            base + "repodata/primary.xml.gz": compressed,
        }
        with patch.object(self.publisher, "_get", side_effect=lambda url, **kwargs: responses[url]):
            result = self.publisher._repository(base)
        self.assertEqual(result["packages"][publish.identity(self.package)], self.artifact)

    def test_copr_xml_base_without_trailing_slash_and_unexpected_base(self):
        base = self.publisher.repo + "devel/"
        for location_base in (
            self.publisher.repo.rstrip("/"),
            self.publisher.repo,
            self.publisher.repo.replace("/hyprland-stack/", "/other-project/"),
        ):
            with self.subTest(location_base=location_base):
                primary = (
                    '<metadata xmlns="http://linux.duke.edu/metadata/common">'
                    '<package><name>lua55</name><arch>x86_64</arch>'
                    '<version epoch="0" ver="5.5.1" rel="1.fc44"/>'
                    f'<checksum type="sha256">{self.artifact["sha256"]}</checksum>'
                    f'<location xml:base="{location_base}" '
                    f'href="00000007-lua55/{publish.filename(self.package)}"/>'
                    '</package></metadata>'
                ).encode()
                repomd = (
                    '<repomd xmlns="http://linux.duke.edu/metadata/repo">'
                    '<data type="primary"><location href="repodata/primary.xml"/>'
                    f'<checksum type="sha256">{checksum(primary)}</checksum>'
                    '</data></repomd>'
                ).encode()
                responses = {
                    base + "repodata/repomd.xml": repomd,
                    base + "repodata/primary.xml": primary,
                }
                with patch.object(
                    self.publisher, "_get", side_effect=lambda url, **kwargs: responses[url]
                ):
                    if location_base.startswith(self.publisher.base):
                        result = self.publisher._repository(base)
                        self.assertEqual(
                            result["packages"][publish.identity(self.package)], self.artifact
                        )
                    else:
                        with self.assertRaisesRegex(PipelineError, "base URL"):
                            self.publisher._repository(base)

    def test_xml_entities_are_rejected(self):
        with self.assertRaises(PipelineError):
            publish.xml(b'<!DOCTYPE a [<!ENTITY b "x">]><a>&b;</a>')


if __name__ == "__main__":
    unittest.main()
