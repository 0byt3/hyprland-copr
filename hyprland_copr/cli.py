"""Local command-line entry points for builds, validation and explicit publication."""

import argparse
import json
from pathlib import Path
import sys
import subprocess
import urllib.error

from .build import Builder, verify_bundle
from .config import load_config
from .fedora import Container, FedoraProviders
from .model import (
    PipelineError,
    REPOSITORIES,
    ROOT_COMPONENTS,
    SOURCE_POLICY,
    rpm_release,
    source_versions,
)
from .resolver import Resolver
from .sources import Sources
from .util import lock, read_json, save_json


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--config", type=Path)
    commands = result.add_subparsers(dest="command", required=True)
    discover = commands.add_parser("discover", help="Report upstream stable releases")
    discover.add_argument("--all", action="store_true", help="Include every managed component")
    for name in ("resolve", "run"):
        command = commands.add_parser(name)
        command.add_argument("--version", help="Select a specific stable release version")
        selection = command.add_mutually_exclusive_group()
        selection.add_argument(
            "--lua-only", action="store_true", help="Exercise the Lua55 pipeline alone"
        )
        selection.add_argument(
            "--component", choices=ROOT_COMPONENTS,
            help="Build one component and its dependencies instead of the coordinated stack",
        )
        command.add_argument("--output", type=Path, default=Path("dist"))
        command.add_argument(
            "--resolution-output", type=Path,
            help="Also save the complete resolution locally before building",
        )
        command.add_argument("--skip-published", action="store_true")
        command.add_argument(
            "--publication-state", type=Path, default=Path(".state/copr.json")
        )
    build = commands.add_parser("build", help="Build and validate a saved resolution")
    build.add_argument("resolution", type=Path)
    build.add_argument("--output", type=Path, default=Path("dist"))
    verify = commands.add_parser("verify", help="Verify a locally validated bundle")
    verify.add_argument("bundle", type=Path)
    for name in ("copr-build", "copr-validate", "copr-promote"):
        command = commands.add_parser(name)
        command.add_argument("bundle", type=Path)
        command.add_argument("--state", type=Path, default=Path(".state/copr.json"))
        if name == "copr-promote":
            command.add_argument("--yes", action="store_true", help="Explicitly allow publication")
    return result


def already_published(config, resolution, published):
    generation = published.get("generation", 1)
    if type(generation) is not int or generation < 1:
        return False
    release = rpm_release(
        {**resolution, "generation": generation}, config["pipeline"]["release"]
    )
    return (
        published.get("status") == "promoted"
        and resolution.get("source_policy") == SOURCE_POLICY
        and published.get("source_policy") == SOURCE_POLICY
        and published.get("destination") == [
            config["copr"]["owner"],
            config["copr"]["project"],
            config["copr"]["chroot"],
        ]
        and published.get("root") == resolution["root"]
        and published.get("source_versions") == source_versions(resolution)
        and published.get("primaries") == resolution.get("primaries", [resolution["root"]])
        and published.get("root_release") == release + ".fc44"
    )


def execute(arguments):
    config = load_config(arguments.config)
    state = Path(config["pipeline"]["state_directory"]).resolve()
    if arguments.command == "verify":
        verify_bundle(arguments.bundle)
        print("Bundle integrity and local validation status verified", flush=True)
        return 0
    if arguments.command.startswith("copr-"):
        from .publish import Publisher

        with lock(state):
            publisher = Publisher(config, arguments.bundle, arguments.state)
            if arguments.command == "copr-build":
                publisher.submit()
            elif arguments.command == "copr-validate":
                publisher.validate()
            else:
                publisher.promote(arguments.yes)
        return 0
    with lock(state):
        sources = Sources(state / "sources")
        if arguments.command == "discover":
            names = ROOT_COMPONENTS
            if arguments.all:
                names = [*REPOSITORIES, "lua"]
            print(json.dumps({
                name: sources.releases(name, config["pipeline"]["max_candidates"])
                for name in names
            }, indent=4))
            return 0
        if arguments.command == "build":
            resolution = read_json(arguments.resolution)
        else:
            root = "lua" if arguments.lua_only else arguments.component or "hyprland"
            releases = sources.releases(root, config["pipeline"]["max_candidates"])
            if arguments.version:
                releases = [
                    value for value in releases
                    if value["version"] == arguments.version.removeprefix("v")
                ]
            if not releases:
                raise PipelineError("Requested stable release was not found in the candidate window")
            print(f"Resolving {root} {releases[0]['version']} for Fedora 44", flush=True)
            with Container(config["target"]["image"]) as container:
                resolution = Resolver(
                    sources, FedoraProviders(container), config["pipeline"]["max_candidates"]
                ).resolve(
                    releases[0],
                    root=root,
                    include_companions=not arguments.component and not arguments.lua_only,
                )
            published = (
                read_json(arguments.publication_state)
                if arguments.publication_state.exists() else {}
            )
            generation = published.get("generation", 0)
            if type(generation) is not int or generation < 0:
                raise PipelineError("Invalid stack generation in publication recovery state")
            resolution["generation"] = generation + 1
            selection = "stack" if len(resolution["primaries"]) > 1 else root
            save_json(state / f"resolution-{selection}.json", resolution)
            save_json(state / "resolution.json", resolution)
            if arguments.resolution_output:
                save_json(arguments.resolution_output, resolution)
            print(f"Resolution: {state / 'resolution.json'}", flush=True)
            if resolution["blockers"]:
                for blocker in resolution["blockers"]:
                    print(
                        f"BLOCKED {blocker['consumer']}: {blocker['capability']} "
                        f"{blocker['constraints']} ({blocker['origin']}): {blocker['reason']}",
                        file=sys.stderr,
                        flush=True,
                    )
                return 3
            if arguments.skip_published and already_published(config, resolution, published):
                print("The complete selected source set is already recorded as published", flush=True)
                return 0
            print("Build order: " + " -> ".join(resolution["order"]), flush=True)
            if arguments.command == "resolve":
                return 0
        manifest = Builder(config, state).build(resolution, arguments.output)
        print(f"Validated bundle: {arguments.output.resolve()}", flush=True)
        print(f"Components built: {len(manifest['builds'])}", flush=True)
        return 0


def main():
    try:
        return execute(parser().parse_args())
    except (
        PipelineError, OSError, ValueError, urllib.error.URLError, subprocess.TimeoutExpired
    ) as error:
        print(f"Pipeline stopped: {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
