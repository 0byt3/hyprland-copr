"""Run local checks/builds, then publish complete validated stacks to COPR."""

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from hyprland_copr.build import verify_bundle
from hyprland_copr.cli import already_published
from hyprland_copr.config import ROOT, load_config
from hyprland_copr.model import PipelineError, ROOT_COMPONENTS
from hyprland_copr.runtime import PODMAN, user_arguments
from hyprland_copr.util import lock, read_json, save_json

from .copr import Publication, check_credentials, destination


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--config", type=Path)
    result.add_argument(
        "--output", type=Path,
        help="New run directory for logs, result.json and bundle (default: dist/runs/<unique-id>)",
    )
    result.add_argument("--version", help="Select a stable version for the primary component")
    selection = result.add_mutually_exclusive_group()
    selection.add_argument("--component", choices=ROOT_COMPONENTS)
    selection.add_argument("--lua-only", action="store_true")
    result.add_argument("--checks-only", action="store_true", help="Run local QA without RPM builds")
    result.add_argument(
        "--no-publish", action="store_true",
        help="Build and validate locally without submitting or resuming COPR publication",
    )
    result.add_argument(
        "--publication-state", type=Path, default=Path(".state/copr.json"),
        help="Persistent COPR recovery state (must be inside pipeline.state_directory to publish)",
    )
    return result


def project_path(path):
    return (ROOT / path).resolve()


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def run_logged(arguments, path, *, env=None):
    print(f"Running {arguments[0]}; log: {path}", flush=True)
    with path.open("w") as stream:
        subprocess.run(
            [str(value) for value in arguments],
            cwd=ROOT,
            stdout=stream,
            stderr=subprocess.STDOUT,
            check=True,
            env=env,
        )


def execute(arguments):
    if os.getuid() == 0:
        raise PipelineError("Run local automation as a non-root account with rootless Podman")
    config_path = project_path(arguments.config) if arguments.config else None
    config = load_config(config_path)
    state = project_path(config["pipeline"]["state_directory"])
    # The checks mount the checkout read-only. Refuse misplaced credentials
    # before starting even checks-only/component runs.
    check_credentials([ROOT, state, project_path(arguments.output)] if arguments.output else [
        ROOT, state,
    ], required=False)
    # The CLI acquires the main state lock in its child process. This separate
    # lock serializes whole local runs without deadlocking that child.
    with lock(state / "automation"):
        if arguments.output:
            output = project_path(arguments.output)
            if output.exists():
                raise PipelineError("Use a new run directory; existing results are never reused")
            output.mkdir(parents=True)
        else:
            parent = ROOT / "dist/runs"
            parent.mkdir(parents=True, exist_ok=True)
            prefix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-")
            output = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
        logs = output / "logs"
        logs.mkdir()
        bundle = output / "bundle"
        result = {
            "schema_version": 1,
            "status": "running",
            "started_at": timestamp(),
            "component": "lua" if arguments.lua_only else arguments.component or "stack",
            "version": arguments.version,
            "checks_only": arguments.checks_only,
            "publication_attempted": False,
            "publication_status": "not-requested",
            "publication_state": str(project_path(arguments.publication_state)),
            "destination": destination(config),
            "bundle": None,
            "resolution": None,
        }
        save_json(output / "result.json", result)
        print(f"Local run: {output}", flush=True)
        phase = "checks-image"

        def progress(value):
            nonlocal phase
            phase = value
            result.update({
                "phase": phase,
                "publication_attempted": True,
                "publication_status": "running",
            })
            save_json(output / "result.json", result)
        try:
            run_logged([
                *PODMAN, "build", "--layers", "--file", ROOT / "container/Checksfile",
                "--tag", "hyprland-copr-checks", ROOT,
            ], logs / "checks-image.log")
            phase = "checks"
            run_logged([
                *PODMAN, "run", "--rm", "--pull=never", "--network=none", "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                *user_arguments(),
                "--env", "HOME=/tmp", "--volume", f"{ROOT}:/workspace:ro,z",
                "hyprland-copr-checks",
            ], logs / "checks.log")
            if not arguments.checks_only:
                publication = None
                pending = None
                if (
                    config["copr"]["publish"]
                    and not arguments.no_publish
                    and not arguments.component
                    and not arguments.lua_only
                ):
                    phase = "publication-preflight"
                    publication = Publication(
                        config, state, project_path(arguments.publication_state),
                        run_logged, logs, progress,
                    )
                    pending = publication.pending()
                    if pending and arguments.version:
                        raise PipelineError("Resume the pending publication without --version first")
                if pending:
                    bundle = pending
                    result["publication_status"] = "resuming"
                else:
                    phase = "build"
                    command = [sys.executable, "-u", "-m", "hyprland_copr"]
                    if config_path:
                        command.extend(["--config", config_path])
                    command.extend([
                        "run", "--output", bundle,
                        "--resolution-output", output / "resolution.json",
                        "--publication-state", project_path(arguments.publication_state),
                    ])
                    if publication:
                        command.append("--skip-published")
                    if arguments.version:
                        command.extend(["--version", arguments.version])
                    if arguments.lua_only:
                        command.append("--lua-only")
                    elif arguments.component:
                        command.extend(["--component", arguments.component])
                    run_logged(command, logs / "build.log")
                    resolution_path = output / "resolution.json"
                    if publication and not bundle.exists() and resolution_path.is_file():
                        published = read_json(project_path(arguments.publication_state))
                        if not already_published(config, read_json(resolution_path), published):
                            raise PipelineError("Build returned no bundle or matching publication record")
                        result["publication_status"] = "already-published"
                        publication = None
                phase = "verify"
                if result["publication_status"] != "already-published":
                    manifest = verify_bundle(bundle)
                    result["bundle"] = str(bundle)
                    if publication:
                        phase = "publication-preflight"
                        publication.run(bundle, manifest)
                        result["publication_status"] = "promoted"
            result.update({
                "status": "passed",
                "exit_code": 0,
                "finished_at": timestamp(),
                "resolution": (
                    str(output / "resolution.json")
                    if (output / "resolution.json").is_file() else None
                ),
            })
            save_json(output / "result.json", result)
            print(f"Local checks{' and build validation' if not arguments.checks_only else ''} passed")
            return 0
        except (PipelineError, OSError, ValueError, subprocess.SubprocessError) as error:
            code = error.returncode if isinstance(error, subprocess.CalledProcessError) else 1
            if code < 0:
                code = 128 - code
            if result["publication_attempted"] or phase == "publication-preflight":
                result["publication_status"] = "failed"
            result.update({
                "status": "blocked" if phase == "build" and code == 3 else "failed",
                "phase": phase,
                "exit_code": code,
                "finished_at": timestamp(),
                "error": str(error),
                "resolution": (
                    str(output / "resolution.json")
                    if (output / "resolution.json").is_file() else None
                ),
            })
            save_json(output / "result.json", result)
            print(f"Local run {result['status']} during {phase}; see {logs}", file=sys.stderr)
            return code


def main(argv=None):
    arguments = parser().parse_args(argv)
    try:
        return execute(arguments)
    except (PipelineError, OSError, ValueError) as error:
        print(f"Local automation stopped: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
