"""Container-only Fedora package queries and dependency transactions."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import uuid

from .config import ROOT
from .model import PipelineError
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
            "docker", "run", "--detach", "--platform=linux/amd64",
            "--name", self.name,
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
        return run(["docker", "exec", self.name, *arguments], **options)

    def __exit__(self, *exception):
        run(["docker", "rm", "--force", self.name])

    @contextmanager
    def offline_image(self):
        image = run(["docker", "commit", self.name])
        try:
            yield image
        finally:
            run(["docker", "image", "rm", image])


class FedoraProviders:
    def __init__(self, container):
        self.container = container
        self.cache = {}

    def query(self, expression):
        if expression not in self.cache:
            output = self.container.exec([
                "dnf", *REPO_OPTIONS,
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
    # Cached exports must not get fresh attestation-index IDs on every build.
    # Retagging and temporary-image cleanup could otherwise remove a live run's ID.
    run([
        "docker", "build", "--platform=linux/amd64", "--provenance=false",
        "--file", ROOT / "container/Containerfile",
        "--tag", config["pipeline"]["build_image"], ROOT,
    ], log=log, timeout=1800)
    return run([
        "docker", "image", "inspect", "--format={{.Id}}",
        config["pipeline"]["build_image"],
    ])


def offline_run(
    image, work, arguments, *, log, readonly_mounts=(), user=True, readonly_work=False
):
    if user and os.getuid() == 0:
        raise PipelineError("Run builds from a non-root account; upstream compilation must be unprivileged")
    return run([
        "docker", "run", "--rm", "--network=none", "--platform=linux/amd64",
        "--cap-drop=ALL", "--security-opt=no-new-privileges",
        "--pids-limit=1024",
        *(["--user", f"{os.getuid()}:{os.getgid()}"] if user else []),
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
    return json.loads(run(["docker", "image", "inspect", image]))[0]["Id"]
