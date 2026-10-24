"""Validate policy before executing commands or contacting COPR."""

from pathlib import Path
import re
import tomllib

from .model import PipelineError


ROOT = Path(__file__).resolve().parent.parent


def load_config(path=None):
    path = Path(path) if path else ROOT / "config/project.toml"
    try:
        with path.open("rb") as stream:
            config = tomllib.load(stream)
    except (OSError, ValueError) as error:
        raise PipelineError(f"Cannot read configuration: {error}") from error
    expected = {
        "schema_version": None,
        "target": {"release", "architecture", "image", "repositories"},
        "pipeline": {
            "release",
            "max_candidates",
            "state_directory",
            "build_image",
            "lua_series",
        },
        "copr": {
            "owner",
            "project",
            "chroot",
            "publish",
            "exclusive_writer",
            "poll_seconds",
            "timeout_seconds",
        },
    }
    if config.keys() != expected.keys():
        raise PipelineError("Configuration has missing or unknown sections")
    for name, keys in expected.items():
        if keys is not None and (
            not isinstance(config[name], dict) or config[name].keys() != keys
        ):
            raise PipelineError(f"Invalid keys in {name}")
    if type(config["schema_version"]) is not int or config["schema_version"] != 1:
        raise PipelineError("Only schema_version = 1 is supported")
    target = config["target"]
    if target != {
        "release": 44,
        "architecture": "x86_64",
        "image": "registry.fedoraproject.org/fedora:44",
        "repositories": ["fedora", "updates"],
    }:
        raise PipelineError("Target policy is Fedora 44 x86_64, fedora and updates only")
    pipeline = config["pipeline"]
    for key in ("release", "max_candidates"):
        if type(pipeline[key]) is not int or not 1 <= pipeline[key] <= 100:
            raise PipelineError(f"pipeline.{key} must be an integer between 1 and 100")
    if pipeline["lua_series"] != "5.5":
        raise PipelineError("Only co-installable Lua 5.5 is supported")
    if not isinstance(pipeline["state_directory"], str) or not pipeline["state_directory"]:
        raise PipelineError("A state_directory is required")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._:/-]*", pipeline["build_image"]):
        raise PipelineError("Invalid builder image name")
    copr = config["copr"]
    for key in ("owner", "project"):
        if not isinstance(copr[key], str) or (
            copr[key] and not re.fullmatch(r"@?[A-Za-z0-9][A-Za-z0-9_.-]*", copr[key])
        ):
            raise PipelineError(f"Invalid COPR {key}")
    if bool(copr["owner"]) != bool(copr["project"]):
        raise PipelineError("Set both COPR owner and project, or neither")
    if copr["chroot"] != "fedora-44-x86_64" or type(copr["publish"]) is not bool:
        raise PipelineError("Invalid COPR chroot or publication flag")
    if type(copr["exclusive_writer"]) is not bool:
        raise PipelineError("copr.exclusive_writer must be a boolean")
    for key in ("poll_seconds", "timeout_seconds"):
        if type(copr[key]) is not int or copr[key] < 1:
            raise PipelineError(f"copr.{key} must be a positive integer")
    return config
