"""Reviewed companion package policy, separate from upstream dependency discovery."""
from pathlib import Path

from .model import PipelineError


def protocol_build_system(directory):
    root = Path(directory)
    systems = [
        name
        for name, filename in (
            ("meson", "meson.build"),
            ("cmake", "CMakeLists.txt"),
        )
        if (root / filename).is_file()
    ]
    if len(systems) != 1:
        raise PipelineError("Protocol build layout is missing or ambiguous; review the adapter")
    return systems[0]


APPLICATIONS = {
    "hyprpaper": {
        "summary": "Wayland wallpaper utility",
        "license": "BSD-3-Clause AND HPND-sell-variant",
        "license_files": (
            "LICENSE",
            "protocols/wlr-layer-shell-unstable-v1.xml",
        ),
        "binary": "%{_bindir}/hyprpaper",
        "files": (
            "%{_bindir}/hyprpaper",
            "%{_userunitdir}/hyprpaper.service",
        ),
        "runtime": (),
        "options": (),
    },
    "hyprlock": {
        "summary": "GPU-accelerated Wayland screen locker",
        "license": "BSD-3-Clause AND MIT",
        "license_files": (
            "LICENSE",
            "protocols/wlr-screencopy-unstable-v1.xml",
        ),
        "binary": "%{_bindir}/hyprlock",
        "files": (
            "%{_bindir}/hyprlock",
            "%config(noreplace) %{_sysconfdir}/pam.d/hyprlock",
            "%{_datadir}/hypr/hyprlock.conf",
        ),
        "runtime": (
            "pam",
        ),
        "options": (
            "-DHYPRLOCK_COMMIT={commit}",
            "-DHYPRLOCK_VERSION_COMMIT={commit}",
        ),
    },
    "hypridle": {
        "summary": "Wayland idle management daemon",
        "license": "BSD-3-Clause",
        "license_files": (
            "LICENSE",
        ),
        "binary": "%{_bindir}/hypridle",
        "files": (
            "%{_bindir}/hypridle",
            "%{_userunitdir}/hypridle.service",
            "%{_datadir}/hypr/hypridle.conf",
        ),
        "runtime": (),
        "options": (),
    },
    "hyprpicker": {
        "summary": "Wayland color picker utility",
        "license": "BSD-3-Clause AND MIT AND HPND-sell-variant",
        "license_files": (
            "LICENSE",
            "protocols/wlr-layer-shell-unstable-v1.xml",
            "protocols/wlr-screencopy-unstable-v1.xml",
        ),
        "binary": "%{_bindir}/hyprpicker",
        "files": (
            "%{_bindir}/hyprpicker",
            "%{_mandir}/man1/hyprpicker.1*",
        ),
        "runtime": (),
        "options": (),
    },
    "hyprsunset": {
        "summary": "Wayland blue-light filter utility",
        "license": "BSD-3-Clause",
        "license_files": (
            "LICENSE",
        ),
        "binary": "%{_bindir}/hyprsunset",
        "files": (
            "%{_bindir}/hyprsunset",
            "%{_userunitdir}/hyprsunset.service",
        ),
        "runtime": (),
        "options": (),
    },
    "uwsm": {
        "summary": "Universal Wayland Session Manager",
        "license": "MIT",
        "license_files": (
            "LICENSE",
        ),
        "binary": "%{_bindir}/uwsm",
        "files": (
            "%{_bindir}/uwsm",
            "%{_bindir}/uwsm-app",
            "%{_bindir}/uwsm-terminal",
            "%{_bindir}/uwsm-terminal-service",
            "%{_bindir}/uwsm-terminal-scope",
            "%{_bindir}/uuctl",
            "%{_bindir}/fumon",
            "%{_bindir}/ttyautolock",
            "%{_bindir}/wait-tray",
            "%{_libexecdir}/uwsm",
            "%{_datadir}/uwsm",
            "%{_datadir}/applications/uuctl.desktop",
            "%{_userunitdir}/*",
            "%{_userpresetdir}/*",
            "%{_mandir}/man1/*.1*",
            "%{_mandir}/man3/*.3*",
        ),
        "runtime": (
            "python3",
            "python3-dbus",
            "python3-pyxdg",
            "systemd",
            "util-linux",
        ),
        "options": (
            "-Duuctl=enabled",
            "-Dfumon=enabled",
            "-Dttyautolock=enabled",
            "-Dwait-tray=enabled",
            "-Duwsm-app=enabled",
            "-Dman-pages=enabled",
        ),
    },
    "xdg-desktop-portal-hyprland": {
        "summary": "XDG desktop portal backend and share picker for Hyprland",
        "license": "BSD-3-Clause AND MIT AND HPND-sell-variant",
        "license_files": (
            "LICENSE",
            "protocols/wlr-screencopy-unstable-v1.xml",
            "protocols/wlr-foreign-toplevel-management-unstable-v1.xml",
        ),
        "binary": "%{_libexecdir}/xdg-desktop-portal-hyprland",
        "files": (
            "%{_libexecdir}/xdg-desktop-portal-hyprland",
            "%{_bindir}/hyprland-share-picker",
            "%{_userunitdir}/xdg-desktop-portal-hyprland.service",
            "%{_datadir}/dbus-1/services/org.freedesktop.impl.portal.desktop.hyprland.service",
            "%{_datadir}/xdg-desktop-portal/portals/hyprland.portal",
        ),
        "runtime": (
            "xdg-desktop-portal",
            "qt6-qtwayland",
            "grim",
            "slurp",
            "dbus",
        ),
        "options": (
            "-DSYSTEMD_SERVICES=ON",
        ),
    },
}

PORTAL_PROTOCOL_MINIMUM = "0.7.0"
PORTAL_PROTOCOL_FILES = (
    "hyprland-global-shortcuts-v1.xml",
    "hyprland-toplevel-export-v1.xml",
    "hyprland-toplevel-mapping-v1.xml",
    "hyprland-input-capture-v1.xml",
)

PORTAL_PREP = """python3 - <<'PY'
from pathlib import Path
path = Path("CMakeLists.txt")
text = path.read_text()
for prefix in ("SDBUS", "HYPRLAND_PROTOS"):
    original = f"pkg_check_modules({prefix} IMPORTED_TARGET "
    if text.count(original) != 1:
        raise SystemExit("Portal dependency layout changed; review packaging")
    text = text.replace(original, f"pkg_check_modules({prefix} REQUIRED IMPORTED_TARGET ")
original = "pkg_check_modules(HYPRLAND_PROTOS REQUIRED IMPORTED_TARGET hyprland-protocols)"
if text.count(original) != 1:
    raise SystemExit("Portal protocol requirement changed; review packaging")
text = text.replace(
    original,
    "pkg_check_modules(HYPRLAND_PROTOS REQUIRED IMPORTED_TARGET hyprland-protocols>=@PORTAL_PROTOCOL_MINIMUM@)",
)
path.write_text(text)
PY""".replace("@PORTAL_PROTOCOL_MINIMUM@", PORTAL_PROTOCOL_MINIMUM)
