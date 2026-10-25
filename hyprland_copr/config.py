"""Validate policy before executing commands or contacting COPR."""

import copy
from email.headerregistry import Address
from pathlib import Path
import re
import tomllib

from .model import PipelineError


ROOT = Path(__file__).resolve().parent.parent
SMTP_CONFIG = ROOT / "config/smtp.toml"

DEFAULT_SMTP = {
    "host": "",
    "port": 587,
    "security": "starttls",
    "username": "",
    "password_file": "",
    "from_address": "",
    "to_addresses": [],
    "timeout_seconds": 30,
}
SMTP_ALIASES = {
    "server": "host",
    "from": "from_address",
    "to": "to_addresses",
}


def _validate_address(value):
    if (
        not isinstance(value, str)
        or not value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise PipelineError("Invalid SMTP email address")
    try:
        parsed = Address(addr_spec=value)
    except Exception as error:
        raise PipelineError("Invalid SMTP email address") from error
    if not parsed.username or not parsed.domain or parsed.addr_spec != value.strip():
        raise PipelineError("SMTP email addresses must include a user and domain")


def _validate_smtp(raw):
    if raw is None:
        return copy.deepcopy(DEFAULT_SMTP)
    if not isinstance(raw, dict):
        raise PipelineError("Invalid keys in smtp")
    normalized = {}
    for key, value in raw.items():
        canonical = SMTP_ALIASES.get(key, key)
        if canonical not in DEFAULT_SMTP or canonical in normalized:
            raise PipelineError("Invalid keys in smtp")
        normalized[canonical] = value
    smtp = {**copy.deepcopy(DEFAULT_SMTP), **normalized}
    if not isinstance(smtp["host"], str) or (
        smtp["host"]
        and (
            not smtp["host"].strip()
            or any(character.isspace() for character in smtp["host"])
            or "/" in smtp["host"]
            or any(ord(character) <= 32 or ord(character) == 127 for character in smtp["host"])
        )
    ):
        raise PipelineError("Invalid SMTP host")
    if type(smtp["port"]) is not int or not 0 <= smtp["port"] <= 65535:
        raise PipelineError("smtp.port must be an integer between 1 and 65535 (or 0 when unset)")
    if not isinstance(smtp["security"], str) or smtp["security"] not in ("", "starttls", "tls"):
        raise PipelineError("smtp.security must be starttls or tls")
    for key in ("username", "password_file"):
        value = smtp[key]
        if not isinstance(value, str) or (
            value
            and (
                not value.strip()
                or any(ord(character) < 32 or ord(character) == 127 for character in value)
            )
        ):
            raise PipelineError(f"Invalid SMTP {key}")
    if smtp["password_file"] and not Path(smtp["password_file"]).is_absolute():
        raise PipelineError("smtp.password_file must be an absolute path")
    if not isinstance(smtp["from_address"], str):
        raise PipelineError("Invalid SMTP from_address")
    if smtp["from_address"]:
        _validate_address(smtp["from_address"])
    if not isinstance(smtp["to_addresses"], list):
        raise PipelineError("smtp.to_addresses must be a list")
    for address in smtp["to_addresses"]:
        _validate_address(address)
    if type(smtp["timeout_seconds"]) is not int or not 1 <= smtp["timeout_seconds"] <= 300:
        raise PipelineError("smtp.timeout_seconds must be an integer between 1 and 300")
    return smtp


def smtp_configured(config):
    smtp = (config or {}).get("smtp")
    if not isinstance(smtp, dict):
        return False
    return bool(
        isinstance(smtp.get("host"), str) and smtp["host"].strip()
        and type(smtp.get("port")) is int and 1 <= smtp["port"] <= 65535
        and smtp.get("security") in ("starttls", "tls")
        and isinstance(smtp.get("username"), str) and smtp["username"].strip()
        and isinstance(smtp.get("password_file"), str) and smtp["password_file"].strip()
        and isinstance(smtp.get("from_address"), str) and smtp["from_address"].strip()
        and isinstance(smtp.get("to_addresses"), list) and len(smtp["to_addresses"]) > 0
    )


def load_smtp_config(path=None):
    explicit = path is not None
    path = Path(path) if explicit else SMTP_CONFIG
    if not path.exists():
        if explicit:
            raise PipelineError(f"Cannot read SMTP configuration: {path}")
        return copy.deepcopy(DEFAULT_SMTP)
    try:
        with path.open("rb") as stream:
            loaded = tomllib.load(stream)
    except (OSError, ValueError) as error:
        raise PipelineError(f"Cannot read SMTP configuration: {error}") from error
    if not isinstance(loaded, dict):
        raise PipelineError("Invalid SMTP configuration")
    if set(loaded.keys()) == {"smtp"}:
        return _validate_smtp(loaded["smtp"])
    if "smtp" in loaded:
        raise PipelineError("Invalid keys in smtp")
    return _validate_smtp(loaded)


def load_config(path=None, *, smtp_path=None):
    explicit_project = path is not None
    path = Path(path) if explicit_project else ROOT / "config/project.toml"
    try:
        with path.open("rb") as stream:
            config = tomllib.load(stream)
    except (OSError, ValueError) as error:
        raise PipelineError(f"Cannot read configuration: {error}") from error
    required = {
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
    if not (required.keys() <= config.keys() <= (required.keys() | {"smtp"})):
        raise PipelineError("Configuration has missing or unknown sections")
    for name, keys in required.items():
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
    if smtp_path is not None:
        config["smtp"] = load_smtp_config(smtp_path)
    elif "smtp" in config:
        config["smtp"] = _validate_smtp(config["smtp"])
    elif not explicit_project or path.resolve() == (ROOT / "config/project.toml").resolve():
        config["smtp"] = load_smtp_config()
    else:
        sibling = path.parent / "smtp.toml"
        config["smtp"] = load_smtp_config(sibling) if sibling.exists() else copy.deepcopy(DEFAULT_SMTP)
    return config

