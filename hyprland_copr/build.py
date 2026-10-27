"""Build the resolved graph and produce a checksummed publication bundle."""

from pathlib import Path
import re
import shutil
import tempfile

from .config import ROOT
from .components import protocol_build_system
from .dependencies import trace_requirements
from .fedora import Container, REPO_OPTIONS, build_image, offline_run
from .model import (
    PipelineError,
    Requirement,
    ROOT_COMPONENTS,
    SOURCE_POLICY,
    hypr_project,
    normalize_requirement,
    rpm_release,
    source_repository,
    version_key,
)
from .packaging import prepare
from .util import digest_json, read_json, save_json, sha256


def check_trace(path, requirements):
    declared = [Requirement.from_dict(value) for value in requirements]
    for actual in trace_requirements(path):
        if not any(
            expected.capability == actual.capability
            and set(actual.constraints).issubset(expected.constraints)
            for expected in declared
        ):
            raise PipelineError(
                f"Configure discovered an undeclared requirement: "
                f"{actual.expressions()} at {actual.origin}; update adapter before publishing"
            )


def verify_bundle(directory):
    directory = Path(directory).resolve()
    manifest = read_json(directory / "manifest.json")
    if not isinstance(manifest, dict) or manifest.get("status") != "validated":
        raise PipelineError("Publication requires a locally validated bundle")
    if (
        manifest.get("schema_version") != 1
        or manifest.get("target") != "fedora-44-x86_64"
        or manifest.get("root") not in ROOT_COMPONENTS
        or not isinstance(manifest.get("files"), dict)
        or not manifest["files"]
        or not isinstance(manifest.get("builds"), list)
        or not manifest["builds"]
        or not isinstance(manifest.get("packages"), list)
        or not manifest["packages"]
        or manifest.get("validation", {}).get("hardware_session_tested") is not False
    ):
        raise PipelineError("Unsupported or incomplete bundle manifest")
    for relative, checksum in manifest["files"].items():
        if (
            not isinstance(relative, str)
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or not isinstance(checksum, str)
            or not re.fullmatch(r"[a-f0-9]{64}", checksum)
        ):
            raise PipelineError("Invalid bundle path or checksum")
        path = (directory / relative).resolve()
        if not path.is_relative_to(directory) or not path.is_file() or sha256(path) != checksum:
            raise PipelineError(f"Bundle integrity failure: {relative}")
    components = set()
    package_files = []
    for build in manifest["builds"]:
        component = build.get("component")
        if not isinstance(component, str) or component in components:
            raise PipelineError("Invalid or repeated bundle component")
        if component != "lua":
            source_repository(component)
        components.add(component)
        srpm = build.get("srpm", "")
        if not srpm.startswith("srpms/") or not srpm.endswith(".src.rpm") or (
            srpm not in manifest["files"]
        ):
            raise PipelineError("Source RPM is not checksummed")
        if not isinstance(build.get("packages"), list) or not build["packages"]:
            raise PipelineError("Component has no binary RPMs")
        package_files.extend(build["packages"])
        source = build.get("source", {})
        version_key(source.get("version", ""))
        if source.get("name") != component or not re.fullmatch(
            r"[a-f0-9]{64}", source.get("sha256", "")
        ):
            raise PipelineError("Invalid source provenance")
    metadata_files = [package.get("file") for package in manifest["packages"]]
    if len(set(package_files)) != len(package_files) or sorted(metadata_files) != sorted(
        package_files
    ):
        raise PipelineError("Package metadata and build records disagree")
    for package in manifest["packages"]:
        relative = package["file"]
        if not relative.startswith("rpms/") or not relative.endswith(".rpm") or (
            relative not in manifest["files"]
        ):
            raise PipelineError("Binary RPM is not checksummed")
        if package.get("arch") not in ("x86_64", "noarch"):
            raise PipelineError("Unexpected binary RPM architecture")
        for key in ("name", "epoch", "version", "release"):
            if not isinstance(package.get(key), str) or not re.fullmatch(
                r"[A-Za-z0-9_.+~^-]+", package[key]
            ):
                raise PipelineError(f"Invalid RPM identity field: {key}")
    if manifest["root"] not in components or "resolution.json" not in manifest["files"]:
        raise PipelineError("Bundle omits root component or resolution provenance")
    resolution = read_json(directory / "resolution.json")
    if digest_json(resolution) != manifest.get("resolution_digest") or (
        resolution.get("order") != [build["component"] for build in manifest["builds"]]
    ):
        raise PipelineError("Resolution provenance does not match the build order")
    policy = manifest.get("source_policy")
    if policy != resolution.get("source_policy") or policy not in (None, SOURCE_POLICY):
        raise PipelineError("Bundle source policy differs from its resolution")
    if policy == SOURCE_POLICY:
        sources = resolution.get("sources", {})
        if set(sources) != components or set(resolution.get("requirements", {})) != components:
            raise PipelineError("Bundle source set differs from its resolution")
        for build in manifest["builds"]:
            if build["source"] != sources[build["component"]]:
                raise PipelineError("Bundle source provenance differs from its resolution")
        for consumer, requirements in resolution.get("requirements", {}).items():
            for value in requirements:
                requirement = normalize_requirement(Requirement.from_dict(value))
                owner = requirement.managed
                if owner and hypr_project(owner) and (
                    owner not in components
                    or owner not in sources
                    or not requirement.accepts(sources[owner]["version"])
                ):
                    raise PipelineError(f"Bundle omits a required hypr source: {consumer}: {owner}")
    primaries = manifest.get("primaries", [manifest["root"]])
    if (
        not isinstance(primaries, list)
        or not primaries
        or len(set(primaries)) != len(primaries)
        or manifest["root"] not in primaries
        or not set(primaries).issubset(components & set(ROOT_COMPONENTS))
        or primaries != resolution.get("primaries", [manifest["root"]])
        or manifest.get("generation", 1) != resolution.get("generation", 1)
        or type(manifest.get("generation", 1)) is not int
        or manifest.get("generation", 1) < 1
    ):
        raise PipelineError("Bundle primary selection or generation differs from its resolution")
    return manifest


class Builder:
    def __init__(self, config, state):
        self.config = config
        self.state = Path(state).resolve()

    def build(self, resolution, destination):
        if resolution["status"] != "resolved":
            raise PipelineError("Dependencies are blocked; no build or publication attempted")
        if resolution.get("source_policy") != SOURCE_POLICY:
            raise PipelineError("Saved resolution uses an older source policy; resolve again")
        destination = Path(destination).resolve()
        if destination.exists() and any(destination.iterdir()):
            raise PipelineError("Use a new/empty bundle directory; existing artifacts are not overwritten")
        destination.mkdir(parents=True, exist_ok=True)
        for name in ("rpms", "srpms", "logs"):
            (destination / name).mkdir()
        provenance = {
            **resolution,
            "sources": {
                name: {
                    key: value
                    for key, value in source.items()
                    if key not in ("directory", "archive")
                }
                for name, source in resolution["sources"].items()
            },
        }
        save_json(destination / "resolution.json", provenance)
        image = build_image(self.config, destination / "logs/builder-image.log")
        release = rpm_release(resolution, self.config["pipeline"]["release"])
        package_metadata = []
        build_records = []
        for name in resolution["order"]:
            source = resolution["sources"][name]
            work = Path(tempfile.mkdtemp(prefix=f"{name}-", dir=self.state))
            spec = prepare(
                source,
                resolution["requirements"][name],
                release,
                work,
            )
            print(f"Building {name} {source['version']}", flush=True)
            with Container(image, [
                (work, "/work", False),
                (destination / "rpms", "/repo", True),
            ]) as container:
                # Existing RPMs are run-local, not an ambient COPR repository.
                if list((destination / "rpms").glob("*.rpm")):
                    container.exec([
                        "dnf", *REPO_OPTIONS, "-y", "install",
                        *[
                            f"/repo/{path.name}"
                            for path in sorted((destination / "rpms").glob("*.rpm"))
                            if "-debuginfo-" not in path.name and "-debugsource-" not in path.name
                        ],
                    ], log=destination / f"logs/{name}-prerequisites.log")
                container.exec([
                    "dnf", *REPO_OPTIONS, "-y", "builddep", f"/work/SPECS/{spec.name}",
                ], log=destination / f"logs/{name}-builddep.log")
                with container.offline_image() as offline:
                    offline_run(
                        offline, work, ["python3", "/pipeline/build.py", spec.name],
                        log=destination / f"logs/{name}-build.log",
                    )
            trace = work / "trace.jsonl"
            if name not in ("lua", "uwsm") and (
                name != "hyprland-protocols" or protocol_build_system(source["directory"]) == "cmake"
            ):
                if not trace.exists():
                    raise PipelineError(f"Missing CMake configure trace for {name}")
                check_trace(trace, resolution["requirements"][name])
                shutil.copyfile(trace, destination / f"logs/{name}-configure.jsonl")
            metadata = read_json(work / "packages.json")
            for value in metadata:
                path = work / value["file"]
                shutil.copyfile(path, destination / "rpms" / path.name)
                value["file"] = f"rpms/{path.name}"
                package_metadata.append(value)
            srpms = list((work / "SRPMS").glob("*.src.rpm"))
            if len(srpms) != 1:
                raise PipelineError(f"Expected exactly one source RPM for {name}")
            shutil.copyfile(srpms[0], destination / "srpms" / srpms[0].name)
            build_records.append({
                "component": name,
                "srpm": f"srpms/{srpms[0].name}",
                "packages": [value["file"] for value in metadata],
                "source": {
                    key: value
                    for key, value in source.items()
                    if key not in ("directory", "archive")
                },
            })
        manifest = {
            "schema_version": 1,
            "source_policy": SOURCE_POLICY,
            "status": "built",
            "target": resolution["target"],
            "root": resolution["root"],
            "primaries": resolution.get("primaries", [resolution["root"]]),
            "generation": resolution.get("generation", 1),
            "builder_image": image,
            "resolution_digest": digest_json(provenance),
            "packages": package_metadata,
            "builds": build_records,
        }
        save_json(destination / "manifest.json", manifest)
        self.validate(destination, manifest, image)
        manifest["files"] = {
            str(path.relative_to(destination)): sha256(path)
            for folder in ("rpms", "srpms")
            for path in sorted((destination / folder).glob("*.rpm"))
        }
        manifest["files"]["resolution.json"] = sha256(destination / "resolution.json")
        manifest["files"]["baseline.json"] = sha256(destination / "baseline.json")
        manifest["status"] = "validated"
        manifest["validation"] = {
            "scope": (
                "rpmlint, Fedora44 install/upgrade, dependency closure, ELF, Lua coexistence, "
                "companion CLI/assets, installed Glaze consumer"
            ),
            "hardware_session_tested": False,
        }
        save_json(destination / "manifest.json", manifest)
        return manifest

    def validate(self, destination, manifest, image, baseline_rpms=()):
        destination = Path(destination).resolve()
        # Public artifacts must be readable by the unprivileged test UID in
        # the read-only mount, including after a credentialed publishing job.
        destination.chmod(0o755)
        (destination / "manifest.json").chmod(0o644)
        with Container(image, [
            (destination, "/work", True),
            (ROOT / "tests", "/tests", True),
        ]) as container:
            if (destination / "pubkey.gpg").is_file():
                container.exec(["rpm", "--import", "/work/pubkey.gpg"])
            # Install the distribution baseline first. The next transaction is
            # an ordinary upgrade with full dependency checks, never --allowerasing.
            names = [
                value["name"] for value in manifest["packages"]
                if not value["name"].endswith(("-debuginfo", "-debugsource"))
            ]
            names += ["lua", "lua-libs", "lua-devel"]
            baseline = {}
            for name in sorted(set(names)):
                available = container.exec([
                    "dnf", *REPO_OPTIONS, "repoquery", "--available",
                    "--queryformat", "%{name}\\n", name,
                ])
                if not available:
                    continue
                container.exec(["dnf", *REPO_OPTIONS, "-y", "install", name])
                output = container.exec([
                    "rpm", "-q", "--qf", "%{EPOCHNUM}\\t%{VERSION}\\t%{RELEASE}", name,
                ])
                baseline[name] = output.split("\t")
            if baseline_rpms:
                for relative in baseline_rpms:
                    path = (destination / relative).resolve()
                    if not path.is_relative_to(destination) or not path.is_file():
                        raise PipelineError("Unsafe published baseline RPM path")
                container.exec([
                    "dnf", *REPO_OPTIONS, "-y", "install",
                    *[f"/work/{relative}" for relative in baseline_rpms],
                ], log=destination / "logs/published-baseline.log")
                installed = set(container.exec(["rpm", "-qa", "--qf", "%{NAME}\\n"]).splitlines())
                for name in sorted(set(names) & installed):
                    output = container.exec([
                        "rpm", "-q", "--qf", "%{EPOCHNUM}\\t%{VERSION}\\t%{RELEASE}", name,
                    ])
                    baseline[name] = output.split("\t")
            save_json(destination / "baseline.json", baseline)
            (destination / "baseline.json").chmod(0o644)
            container.exec([
                "dnf", *REPO_OPTIONS, "--setopt=tsflags=", "-y", "install",
                *[
                    f"/work/{value['file']}"
                    for value in manifest["packages"]
                    if not value["name"].endswith(("-debuginfo", "-debugsource"))
                ],
            ], log=destination / "logs/install.log")
            with container.offline_image() as offline:
                offline_run(
                    offline, destination,
                    ["python3", "/pipeline/validate.py", "/work/manifest.json"],
                    log=destination / "logs/validate.log",
                    readonly_mounts=[(ROOT / "tests", "/tests")],
                    readonly_work=True,
                )
