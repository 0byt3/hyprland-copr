"""Local orchestration only; authenticated workers never run package tests."""

import copy
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

from hyprland_copr.build import verify_bundle
from hyprland_copr.config import ROOT
from hyprland_copr.model import COMPANIONS, PipelineError, SOURCE_POLICY
from hyprland_copr.util import digest_json, read_json, save_json


IMAGE = "hyprland-copr-publisher:44"


def destination(config):
    return [config["copr"][key] for key in ("owner", "project", "chroot")]


def complete_stack(manifest):
    primaries = manifest.get("primaries", [])
    if (
        manifest.get("root") != "hyprland"
        or manifest.get("source_policy") != SOURCE_POLICY
        or len(primaries) != 1 + len(COMPANIONS)
        or set(primaries) != {"hyprland", *COMPANIONS}
    ):
        raise PipelineError("Automatic publication requires a complete current-policy stack")


def credential_path():
    # Read metadata only; the controller never reads the credential contents.
    return Path(os.environ.get("COPR_CONFIG_FILE") or Path.home() / ".config/copr").absolute()


def check_credentials(roots, *, required=True):
    path = credential_path()
    resolved = path.resolve()
    if any(resolved.is_relative_to(Path(root).resolve()) for root in roots):
        raise PipelineError("Keep COPR credentials outside source, state, and artifact directories")
    if not required and not path.exists():
        return path
    try:
        metadata = path.lstat()
    except OSError:
        raise PipelineError("Provide a protected COPR client file outside the checkout") from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise PipelineError("COPR client file must be a regular, user-owned file with mode 0600")
    return path


def public_config(config, state):
    value = copy.deepcopy(config)
    value["pipeline"]["state_directory"] = str(state)

    def literal(item):
        return json.dumps(item, ensure_ascii=True)

    lines = [f"schema_version = {value['schema_version']}"]
    for section in ("target", "pipeline", "copr"):
        lines.extend(["", f"[{section}]"])
        for key, item in value[section].items():
            lines.append(f"{key} = {literal(item)}")
    return "\n".join(lines) + "\n"


class Publication:
    def __init__(self, config, state, state_path, run_logged, logs, progress):
        self.config = config
        self.state = state
        self.state_path = state_path
        if (
            not state_path.is_relative_to(state)
            or state_path == state
            or ROOT.is_relative_to(state)
        ):
            raise PipelineError("Automatic publication state must be inside pipeline.state_directory")
        if config["copr"]["exclusive_writer"] is not True:
            raise PipelineError("Automatic publication requires copr.exclusive_writer=true")
        if not all(destination(config)):
            raise PipelineError("Configure the COPR publication destination")
        self.pointer = state / "automation/publication.json"
        self.run_logged = run_logged
        self.logs = logs
        self.progress = progress

    def pending(self):
        if not self.pointer.exists():
            if self.state_path.exists():
                state = read_json(self.state_path)
                if not isinstance(state, dict) or state.get("status") != "promoted":
                    raise PipelineError("Pending COPR state has no automation handoff; recover it explicitly")
            return None
        handoff = read_json(self.pointer)
        if (
            not isinstance(handoff, dict)
            or handoff.get("schema_version") != 1
            or handoff.get("destination") != destination(self.config)
            or handoff.get("publication_state") != str(self.state_path)
            or not isinstance(handoff.get("bundle"), str)
            or not Path(handoff["bundle"]).is_absolute()
        ):
            raise PipelineError("Pending publication handoff has a different destination or state path")
        bundle = Path(handoff["bundle"]).resolve()
        manifest = verify_bundle(bundle)
        complete_stack(manifest)
        if digest_json(manifest) != handoff.get("manifest_digest"):
            raise PipelineError("Pending publication bundle changed; preserve and reconcile its state")
        return bundle

    def status(self, manifest):
        if not self.state_path.exists():
            return None
        state = read_json(self.state_path)
        if not isinstance(state, dict):
            raise PipelineError("Malformed COPR publication recovery state")
        if state.get("manifest_digest") != digest_json(manifest):
            if state.get("status") != "promoted":
                raise PipelineError("Another manifest is pending; recover it before publication")
            return None
        if state.get("destination") != destination(self.config):
            raise PipelineError("Publication recovery state has a different destination")
        return state.get("status")

    def authenticated(self, command, bundle, config_path, credentials):
        # Mount only trusted Python code, public configuration, the immutable
        # bundle and persistent state. Never mount the checkout or Docker socket.
        return [
            "docker", "run", "--rm", "--platform=linux/amd64",
            "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev",
            "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=128",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--env", "HOME=/tmp", "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--env", "COPR_CONFIG_FILE=/run/copr-config",
            "--volume", f"{ROOT / 'hyprland_copr'}:/workspace/hyprland_copr:ro,z",
            "--volume", f"{config_path}:/run/pipeline.toml:ro,z",
            "--volume", f"{credentials}:/run/copr-config:ro,z",
            "--volume", f"{bundle}:{bundle}:ro,z",
            "--volume", f"{self.state}:{self.state}:z",
            IMAGE, "--config", "/run/pipeline.toml",
            command, bundle, "--state", self.state_path,
            *(["--yes"] if command == "copr-promote" else []),
        ]

    def run(self, bundle, manifest):
        complete_stack(manifest)
        credentials = check_credentials([ROOT, self.state, bundle])
        if self.state.is_relative_to(bundle) or bundle.is_relative_to(self.state):
            raise PipelineError("Keep publication bundles separate from writable pipeline state")
        status = self.status(manifest)
        if status not in (None, "building", "submitted", "validated", "promoting", "promoted"):
            raise PipelineError("Unsupported publication state; explicit recovery required")
        # The handoff precedes every possible remote mutation. Retain it on any
        # failure, including an interrupted upload or promotion observation.
        save_json(self.pointer, {
            "schema_version": 1,
            "destination": destination(self.config),
            "publication_state": str(self.state_path),
            "bundle": str(bundle),
            "manifest_digest": digest_json(manifest),
        })
        self.progress("publisher-image")
        self.run_logged([
            "docker", "build", "--platform=linux/amd64",
            "--file", ROOT / "container/Publisherfile", "--tag", IMAGE, ROOT,
        ], self.logs / "publisher-image.log")
        with tempfile.TemporaryDirectory(prefix="hyprland-copr-publication-") as temporary:
            config_path = Path(temporary) / "project.toml"
            config_path.write_text(public_config(self.config, self.state))
            if status in (None, "building", "submitted"):
                self.progress("copr-build")
                self.run_logged(
                    self.authenticated("copr-build", bundle, config_path, credentials),
                    self.logs / "copr-build.log",
                )
                status = self.status(manifest)
                if status not in ("submitted", "validated"):
                    raise PipelineError("COPR submission did not produce the expected recovery state")
            if status == "submitted":
                self.progress("copr-validate")
                # Do not inherit tokens, credential paths, PYTHONPATH, or a
                # credential-bearing HOME into the candidate validation process.
                environment = {
                    key: os.environ[key]
                    for key in (
                        "PATH", "LANG", "LC_ALL", "XDG_RUNTIME_DIR",
                        "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG",
                        "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH",
                    )
                    if key in os.environ
                }
                environment["HOME"] = temporary
                self.run_logged([
                    sys.executable, "-u", "-m", "hyprland_copr",
                    "--config", config_path, "copr-validate", bundle,
                    "--state", self.state_path,
                ], self.logs / "copr-validate.log", env=environment)
                if self.status(manifest) != "validated":
                    raise PipelineError("Signed-candidate validation did not produce a receipt")
            # Never repeat submission/validation once promotion intent exists.
            self.progress("copr-promote")
            self.run_logged(
                self.authenticated("copr-promote", bundle, config_path, credentials),
                self.logs / "copr-promote.log",
            )
            if self.status(manifest) != "promoted":
                raise PipelineError("COPR public repository has not converged")
        self.pointer.unlink()
