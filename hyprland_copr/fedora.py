"""Container-only Fedora package queries and dependency transactions."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import uuid

from .config import ROOT
from .model import PipelineError
from .runtime import PODMAN, user_arguments
from .util import run


REPO_OPTIONS = [
    "--repo=fedora",
    "--repo=updates",
    "--setopt=install_weak_deps=False",
    "--setopt=skip_if_unavailable=False",
]


class Container:
    def __init__(self, image, mounts=()):
        self.image = image
        self.mounts = mounts
        self.name = f"hyprland-copr-{uuid.uuid4().hex}"

    def __enter__(self):
        run([
            *PODMAN, "run", "--detach", "--platform=linux/amd64",
            "--name", self.name,
            "--user", "0:0",
            "--security-opt=no-new-privileges",
            *[
                item
                for source, destination, readonly in self.mounts
                for item in (
                    "--volume",
                    f"{Path(source).resolve()}:{destination}:{'ro,z' if readonly else 'z'}",
                )
            ],
            self.image, "sleep", "infinity",
        ])
        return self

    def exec(self, arguments, **options):
        return run([*PODMAN, "exec", self.name, *arguments], **options)

    def __exit__(self, *exception):
        run([*PODMAN, "rm", "--force", self.name])

    @contextmanager
    def offline_image(self):
        image = run([*PODMAN, "commit", "--quiet", self.name])
        try:
            yield image
        finally:
            run([*PODMAN, "image", "rm", image])


class FedoraProviders:
    def __init__(self, container):
        self.container = container
        self.cache = {}

    def query(self, expression):
        if expression not in self.cache:
            options = [
                *REPO_OPTIONS,
                *(
                    ["--setopt=optional_metadata_types=filelists"]
                    if expression.startswith("/usr/share/") else []
                ),
            ]
            output = self.container.exec([
                "dnf", *options,
                "repoquery", "--available", "--arch=x86_64,noarch",
                "--whatprovides", expression,
                "--queryformat", "%{full_nevra}\\n",
            ])
            self.cache[expression] = sorted(set(output.splitlines()))
        return self.cache[expression]

    def find(self, requirement):
        candidates = [set(self.query(value)) for value in requirement.expressions()]
        return sorted(set.intersection(*candidates))

    def available(self, capability):
        return self.query(capability)


def build_image(config, log):
    # Native Podman layer caching reuses unchanged preparation images. Docker's
    # BuildKit provenance flag is not supported or needed by this builder.
    run([
        *PODMAN, "build", "--platform=linux/amd64", "--layers",
        "--file", ROOT / "container/Containerfile",
        "--tag", config["pipeline"]["build_image"], ROOT,
    ], log=log, timeout=1800)
    return run([
        *PODMAN, "image", "inspect", "--format={{.Id}}",
        config["pipeline"]["build_image"],
    ])


def offline_run(
    image, work, arguments, *, log, readonly_mounts=(), user=True, readonly_work=False
):
    if user and os.getuid() == 0:
        raise PipelineError("Run builds from a non-root account; upstream compilation must be unprivileged")
    return run([
        *PODMAN, "run", "--rm", "--pull=never", "--network=none", "--platform=linux/amd64",
        "--cap-drop=ALL", "--security-opt=no-new-privileges",
        "--pids-limit=1024",
        *(user_arguments() if user else ["--user", "0:0"]),
        "--env", "HOME=/tmp",
        "--volume", f"{Path(work).resolve()}:/work:{'ro,z' if readonly_work else 'z'}",
        "--volume", f"{ROOT / 'container'}:/pipeline:ro,z",
        *[
            item
            for source, destination in readonly_mounts
            for item in ("--volume", f"{Path(source).resolve()}:{destination}:ro,z")
        ],
        image, *arguments,
    ], log=log, timeout=18000)


def image_identity(image):
    return json.loads(run([*PODMAN, "image", "inspect", image]))[0]["Id"]
