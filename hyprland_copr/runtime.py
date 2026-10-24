"""Native local Podman arguments and credential-free controller environments."""

import os
from pathlib import Path

from .model import PipelineError


PODMAN = ("podman", "--remote=false")


def user_arguments():
    if os.getuid() == 0:
        raise PipelineError("Run rootless Podman automation as a non-root account")
    return ["--userns=keep-id", "--user", f"{os.getuid()}:{os.getgid()}"]


def validation_environment(home):
    environment = {
        key: os.environ[key]
        for key in (
            "PATH",
            "LANG",
            "LC_ALL",
            "XDG_RUNTIME_DIR",
            "DBUS_SESSION_BUS_ADDRESS",
            "CONTAINERS_STORAGE_CONF",
            "CONTAINERS_CONF",
            "CONTAINERS_REGISTRIES_CONF",
            "STORAGE_DRIVER",
            "STORAGE_OPTS",
        )
        if key in os.environ
    }
    # Changing HOME alone would select a different rootless image database.
    # Preserve public Podman configuration/storage paths, not credential values.
    original = Path.home()
    for key, default in (
        ("XDG_CONFIG_HOME", original / ".config"),
        ("XDG_DATA_HOME", original / ".local/share"),
        ("XDG_CACHE_HOME", original / ".cache"),
    ):
        path = Path(os.environ.get(key) or default)
        if not path.is_absolute():
            raise PipelineError(f"{key} must be an absolute path for rootless Podman")
        environment[key] = str(path)
    environment["HOME"] = str(home)
    return environment
