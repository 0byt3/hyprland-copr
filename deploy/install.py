#!/usr/bin/env python3
"""Install the local automation's systemd user service and timer."""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


PROJECT = Path(__file__).resolve().parent.parent
MARKER = "# Managed by hyprland-copr deploy/install.py.\n"
SERVICE = "hyprland-copr.service"
TIMER = "hyprland-copr.timer"


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--no-enable", action="store_true",
        help="Only install/validate unit files; do not contact systemd or change activation",
    )
    result.add_argument(
        "--calendar", help="Override the default six-hour systemd OnCalendar expression",
    )
    return result


def check_text(value):
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("Unit settings cannot contain newlines or other control characters")
    return value


def unit_quote(value):
    value = check_text(str(value)).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    # systemd does not expand environment variables in the executable path.
    return f'"{value}"'


def unit_path(value):
    # Unlike ExecStart, WorkingDirectory does not remove shell-style quotes.
    # Interior spaces are literal; percent signs still expand as specifiers.
    value = check_text(str(value))
    if value.endswith("\\") or value != value.rstrip():
        raise ValueError("Checkout paths cannot end in whitespace or a backslash")
    return value.replace("%", "%%")


def replace_setting(text, name, value):
    lines = text.splitlines()
    matches = [index for index, line in enumerate(lines) if line.startswith(name + "=")]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one {name} setting in the unit template")
    lines[matches[0]] = name + "=" + check_text(value)
    return "\n".join(lines) + "\n"


def render_units(project, interpreter, calendar=None):
    templates = project / "deploy/systemd"
    service = (templates / SERVICE).read_text()
    service = replace_setting(service, "WorkingDirectory", unit_path(project))
    service = replace_setting(
        service, "ExecStart", f"{unit_quote(interpreter)} -m ci.release",
    )
    timer = (templates / TIMER).read_text()
    if calendar is not None:
        if not calendar.strip():
            raise ValueError("--calendar must not be empty")
        timer = replace_setting(timer, "OnCalendar", calendar)
    return {
        SERVICE: MARKER + service,
        TIMER: MARKER + timer,
    }


def run(arguments, **options):
    return subprocess.run(
        arguments, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **options,
    )


def atomic_write(path, content):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=path.parent, prefix=f".{path.name}-", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def install(arguments, project=None):
    if os.getuid() == 0:
        raise RuntimeError("Run this installer as the automation user, without sudo")
    if sys.version_info < (3, 11):
        raise RuntimeError("Python 3.11 or newer is required")
    project = (project or PROJECT).resolve(strict=True)
    for relative in (
        "ci/release.py",
        "hyprland_copr/__main__.py",
        "config/project.toml",
        "container/Checksfile",
    ):
        if not (project / relative).is_file():
            raise RuntimeError(f"Missing project file: {relative}")
    for tool in ("git", "docker", "systemd-analyze", "systemctl"):
        if shutil.which(tool) is None:
            raise RuntimeError(f"Missing {tool}; install prerequisites from distribution repositories")
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    if not config_home.is_absolute():
        raise ValueError("XDG_CONFIG_HOME must be an absolute path")
    directory = config_home / "systemd/user"
    units = render_units(project, Path(sys.executable).absolute(), arguments.calendar)
    for name in units:
        path = directory / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise RuntimeError(f"Refusing to replace a symlink or non-file: {path}")
        if path.exists() and not path.read_text().startswith(MARKER):
            raise RuntimeError(
                f"Refusing to overwrite an unmanaged unit: {path}; back it up and remove it first"
            )
    with tempfile.TemporaryDirectory(prefix="hyprland-copr-systemd-") as staging:
        staged = []
        for name, content in units.items():
            path = Path(staging) / name
            path.write_text(content)
            staged.append(str(path))
        try:
            run(["systemd-analyze", "--user", "verify", *staged])
        except subprocess.CalledProcessError as error:
            detail = error.stderr or b"systemd rejected the generated units"
            if isinstance(detail, bytes):
                detail = detail.decode(errors="replace")
            raise RuntimeError(f"Generated unit validation failed: {detail.strip()}") from error
    if not arguments.no_enable:
        # Only query a non-secret property; never dump the manager environment.
        run(["systemctl", "--user", "show", "--property=Version", "--value"])
        active = subprocess.run(
            ["systemctl", "--user", "is-active", SERVICE],
            check=False, capture_output=True, text=True,
        )
        if active.returncode not in (3, 4) or active.stdout.strip() not in (
            "inactive", "failed", "unknown",
        ):
            raise RuntimeError(
                "The automation service is active or its state is unknown; "
                "wait for it to finish before installing"
            )
        run(["docker", "info"])
    directory.mkdir(parents=True, exist_ok=True)
    previous = {
        name: (directory / name).read_text() if (directory / name).exists() else None
        for name in units
    }
    written = []
    try:
        for name, content in units.items():
            if previous[name] != content:
                atomic_write(directory / name, content)
                written.append(name)
    except OSError:
        for name in reversed(written):
            if previous[name] is None:
                (directory / name).unlink(missing_ok=True)
            else:
                atomic_write(directory / name, previous[name])
        raise
    print(f"Installed user units in {directory}")
    print(f"Automation checkout: {project}")
    print("Complete-stack runs publish to the configured COPR after all validation gates pass.")
    print("Prepare protected credentials and the documented COPR project policy before activation.")
    if arguments.no_enable:
        print("Activation unchanged; reload and enable the timer manually when ready.")
    else:
        try:
            run(["systemctl", "--user", "daemon-reload"])
            run(["systemctl", "--user", "enable", TIMER])
            run(["systemctl", "--user", "restart", TIMER])
        except subprocess.CalledProcessError as error:
            raise RuntimeError(
                "Unit files are installed, but activation failed; "
                "fix the user systemd session and rerun the installer"
            ) from error
        print("Enabled and started hyprland-copr.timer; persistent catch-up may start a build now.")
    return directory


def main(argv=None):
    arguments = parser().parse_args(argv)
    try:
        install(arguments)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Installation stopped: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
