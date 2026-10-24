"""Headless package checks; never start a desktop service or authenticate."""

import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile


APPLICATION_BINARIES = {
    "hyprpaper": "/usr/bin/hyprpaper",
    "hyprlock": "/usr/bin/hyprlock",
    "hypridle": "/usr/bin/hypridle",
    "xdg-desktop-portal-hyprland": "/usr/libexec/xdg-desktop-portal-hyprland",
}
APPLICATION_ASSETS = {
    "hyprpaper": (
        "/usr/lib/systemd/user/hyprpaper.service",
    ),
    "hyprlock": (
        "/etc/pam.d/hyprlock",
        "/usr/share/hypr/hyprlock.conf",
    ),
    "hypridle": (
        "/usr/lib/systemd/user/hypridle.service",
        "/usr/share/hypr/hypridle.conf",
    ),
    "xdg-desktop-portal-hyprland": (
        "/usr/bin/hyprland-share-picker",
        "/usr/lib/systemd/user/xdg-desktop-portal-hyprland.service",
        "/usr/share/dbus-1/services/org.freedesktop.impl.portal.desktop.hyprland.service",
        "/usr/share/xdg-desktop-portal/portals/hyprland.portal",
    ),
}


def check_assets(name, root=Path("/")):
    binary = APPLICATION_BINARIES[name]
    for relative in (binary, *APPLICATION_ASSETS[name]):
        path = root / relative.lstrip("/")
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"Missing or unexpected symlinked package asset: {relative}")
        if path.stat().st_mode & 0o6022:
            raise RuntimeError(f"Unsafe package asset permissions: {relative}")
        if relative.endswith(".service"):
            prefix = "Exec=" if "/dbus-1/" in relative else "ExecStart="
            if not re.search(
                rf"^{re.escape(prefix + binary)}(?:[ \t]|$)", path.read_text(), re.M
            ):
                raise RuntimeError(f"Activation asset references an unexpected binary: {relative}")
    if name == "hyprlock":
        policy = root / "etc/pam.d/hyprlock"
        if stat.S_IMODE(policy.stat().st_mode) != 0o644 or (
            re.findall(r"^\s*(auth|account)\s+include\s+system-auth\s*$", policy.read_text(), re.M)
            != ["auth", "account"]
        ):
            raise RuntimeError("Unexpected Fedora-oriented hyprlock PAM policy")
    if name == "xdg-desktop-portal-hyprland":
        portal = root / "usr/share/xdg-desktop-portal/portals/hyprland.portal"
        if "DBusName=org.freedesktop.impl.portal.desktop.hyprland" not in portal.read_text():
            raise RuntimeError("Unexpected portal D-Bus identity")
        if not os.access(root / "usr/bin/hyprland-share-picker", os.X_OK):
            raise RuntimeError("Portal share picker is not executable")


def check_applications(packages):
    for package in packages:
        name = package["name"]
        if name not in APPLICATION_BINARIES:
            continue
        check_assets(name)
        with tempfile.TemporaryDirectory(prefix="companion-smoke-") as temporary:
            directory = Path(temporary)
            (directory / "config").mkdir()
            (directory / "runtime").mkdir(mode=0o700)
            environment = {
                **os.environ,
                "HOME": temporary,
                "XDG_CONFIG_HOME": str(directory / "config"),
                "XDG_RUNTIME_DIR": str(directory / "runtime"),
            }
            for variable in ("WAYLAND_DISPLAY", "DISPLAY", "DBUS_SESSION_BUS_ADDRESS"):
                environment.pop(variable, None)
            for option in ("--help", "--version"):
                result = subprocess.run(
                    [APPLICATION_BINARIES[name], option],
                    env=environment,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                text = result.stdout + result.stderr
                if option == "--version" and not re.search(
                    rf"\bv?{re.escape(package['version'])}\b", text
                ):
                    raise RuntimeError(f"Installed {name} reports an unexpected version")
                print(text.strip())
        print(f"{name}: isolated CLI and package asset checks passed")


def check_hyprland(version):
    with tempfile.TemporaryDirectory(prefix="hyprland-smoke-") as temporary:
        directory = Path(temporary)
        (directory / "config").mkdir()
        (directory / "runtime").mkdir(mode=0o700)
        environment = {
            **os.environ,
            "HOME": temporary,
            "XDG_CONFIG_HOME": str(directory / "config"),
            "XDG_RUNTIME_DIR": str(directory / "runtime"),
        }
        for variable in (
            "WAYLAND_DISPLAY",
            "DISPLAY",
            "DBUS_SESSION_BUS_ADDRESS",
            "HYPRLAND_INSTANCE_SIGNATURE",
        ):
            environment.pop(variable, None)
        result = subprocess.run(
            ["Hyprland", "--version"],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        text = result.stdout + result.stderr
        if not re.search(rf"\bv?{re.escape(version)}\b", text):
            raise RuntimeError("Installed Hyprland reports an unexpected version")
        print(text.strip())
    print("Hyprland: isolated CLI version check passed")


def check_glaze(packages):
    versions = [package["version"] for package in packages if package["name"] == "glaze-devel"]
    if not versions:
        return
    if len(versions) != 1:
        raise RuntimeError("Expected exactly one installed Glaze development RPM")
    metadata = subprocess.run([
        "rpm", "-q", "--provides", "glaze-devel"
    ], check=True, capture_output=True, text=True, timeout=10)
    if f"cmake(glaze) = {versions[0]}" not in metadata.stdout.splitlines():
        raise RuntimeError("Installed Glaze RPM lacks the expected versioned CMake provide")
    with tempfile.TemporaryDirectory(prefix="glaze-consumer-") as temporary:
        directory = Path(temporary)
        shutil.copytree("/tests/glaze", directory / "source")
        subprocess.run([
            "cmake", "-S", str(directory / "source"), "-B", str(directory / "build"),
            "-G", "Ninja", f"-DEXPECTED_GLAZE_VERSION={versions[0]}",
        ], check=True, timeout=60)
        subprocess.run([
            "cmake", "--build", str(directory / "build")
        ], check=True, timeout=180)
        subprocess.run([str(directory / "build/glaze-smoke")], check=True, timeout=10)
    print("Installed Glaze headers, versioned CMake metadata and C++23 JSON round-trip passed")
