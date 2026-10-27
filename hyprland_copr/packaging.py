"""Render reviewed specs, with generated versioned BuildRequires."""

import hashlib
from datetime import datetime, timezone
from pathlib import Path
import re
import shutil

from .config import ROOT
from .components import (
    APPLICATIONS,
    PORTAL_PREP,
    PORTAL_PROTOCOL_FILES,
    protocol_build_system,
)
from .model import HYPR_ALIASES, PipelineError, Requirement, REPOSITORIES, source_repository
from .util import sha256


def library_policy(source):
    name = source["name"]
    if name == "hyprtoolkit":
        return "BSD-3-Clause AND HPND-sell-variant", (
            "LICENSE protocols/wlr-layer-shell-unstable-v1.xml"
        )
    if name not in REPOSITORIES:
        from .dependencies import commands

        root = Path(source["directory"])
        entry = root / "CMakeLists.txt"
        declarations = list(commands(entry.read_text(), str(entry))) if entry.is_file() else []
        if not (
            any(command == "add_library" and "SHARED" in args for command, args, _ in declarations)
            and (root / "include" / name).is_dir()
            and (root / f"{name}.pc.in").is_file()
        ):
            raise PipelineError(f"{name}: unsupported shared-library packaging layout; review adapter")
        license_file = root / "LICENSE"
        text = license_file.read_text() if license_file.is_file() else ""
        header, marker, body = text.partition("Redistribution and use")
        fingerprint = hashlib.sha256(" ".join((marker + body).split()).encode()).hexdigest()
        notices = [
            path for path in root.rglob("*")
            if path.is_file() and ".git" not in path.relative_to(root).parts
            and (
                path.suffix.lower() == ".xml"
                or path.name.upper().startswith(("LICENSE", "COPYING", "NOTICE"))
            )
            and path != license_file
        ]
        if (
            not re.fullmatch(r"BSD 3-Clause License\s+Copyright[^\n]+\s*", header)
            or fingerprint != "70435d703f5e4bdca9a1fa6b9291501b0dd850d2ad5457b491ce936217b1e063"
            or notices
        ):
            raise PipelineError(f"{name}: unreviewed license form or bundled notices; review packaging")
    return "BSD-3-Clause", "LICENSE*"


def render_spec(source, requirements, release):
    name = source["name"]
    repository = source_repository(name) if name != "lua" else ""
    template = {
        "lua": "lua55",
        "hyprland": "hyprland",
        "hyprwayland-scanner": "scanner",
        "hyprland-protocols": "protocols",
        "glaze": "glaze",
        **{component: "application" for component in APPLICATIONS},
        "uwsm": "uwsm",
    }.get(name, "library")
    text = (ROOT / "packaging" / f"{template}.spec.in").read_text()
    license_expression, license_files = library_policy(source) if template == "library" else ("", "")
    # RPM/createrepo_c primary.xml indexes /usr/bin and /etc paths, not
    # /usr/share files; DNF5 builddep in COPR mock does not load filelists.
    expressions = sorted({
        expression
        for value in requirements
        for expression in Requirement.from_dict(value).expressions()
        if not expression.startswith("/usr/share/")
    })
    udis = Path(source["directory"]) / "subprojects/udis86/configure.ac"
    udis_version = "1.7.2"
    if name == "hyprland":
        if not udis.exists():
            raise PipelineError("Bundled udis86 layout changed; review packaging")
        match = re.search(r"AC_INIT\(\[udis86\],\s*\[([^]]+)\]", udis.read_text())
        if not match:
            raise PipelineError("Cannot determine bundled udis86 version")
        udis_version = match[1]
    application = APPLICATIONS.get(name, {})
    cmake_protocols = (
        name == "hyprland-protocols" and protocol_build_system(source["directory"]) == "cmake"
    )
    values = {
        "NAME": name,
        "VERSION": source["version"],
        "RELEASE": str(release),
        "SUMMARY": f"{name} library for the Hyprland stack",
        "UPSTREAM": repository.split("/")[-1],
        "UPSTREAM_REPO": repository,
        "LIBRARY_LICENSE": license_expression,
        "LIBRARY_LICENSE_FILES": license_files,
        "RUNTIME_PROVIDES": "\n".join(
            f"Provides:       {capability} = %{{version}}-%{{release}}"
            for alias, owner in HYPR_ALIASES.items() if owner == name
            for capability in (alias, alias + "-libs")
        ),
        "DEVEL_PROVIDES": "\n".join(
            f"Provides:       {alias}-devel = %{{version}}-%{{release}}"
            for alias, owner in HYPR_ALIASES.items() if owner == name
        ),
        "BUILD_REQUIRES": "\n".join(f"BuildRequires:  {value}" for value in expressions),
        "EPOCH": str(source["epoch"]),
        "COMMIT": source["commit"],
        "UDIS_VERSION": udis_version,
        "CHECK": (
            "ctest --test-dir %{_vpath_builddir} --output-on-failure --timeout 60"
            if name == "hyprlang" else ":"
        ),
        "RUNTIME_FILES": "%{_bindir}/*" if name in ("hyprcursor", "hyprwire") else "",
        "DEVEL_FILES": (
            "%{_libdir}/cmake/hyprwire-scanner" if name == "hyprwire" else ""
        ),
        "APP_SUMMARY": application.get("summary", ""),
        "APP_LICENSE": application.get("license", ""),
        "APP_LICENSE_FILES": " ".join(application.get("license_files", ())),
        "APP_BINARY": application.get("binary", ""),
        "APP_VERSION_OPTION": "--help" if name == "hyprland-guiutils" else "--version",
        "APP_FILES": "\n".join(application.get("files", ())),
        "APP_OPTIONS": " ".join(
            option.format(commit=source["commit"]) for option in application.get("options", ())
        ),
        "EXTRA_SOURCES": "Source1:        hyprlock.pam" if name == "hyprlock" else "",
        "RUNTIME_REQUIRES": "\n".join(
            f"Requires:       {package}" for package in application.get("runtime", ())
        ),
        "APP_PREP": PORTAL_PREP if name == "xdg-desktop-portal-hyprland" else ":",
        "APP_BUILD_CHECK": (
            "\n".join([
                *(
                    f"test -s %{{_datadir}}/hyprland-protocols/protocols/{filename}"
                    for filename in PORTAL_PROTOCOL_FILES
                ),
                "grep -Fx 'SDBUS_FOUND:INTERNAL=1' %{_vpath_builddir}/CMakeCache.txt",
                "grep -Fx 'HYPRLAND_PROTOS_FOUND:INTERNAL=1' %{_vpath_builddir}/CMakeCache.txt",
            ])
            if name == "xdg-desktop-portal-hyprland" else ":"
        ),
        "APP_INSTALL": (
            "install -Dm0644 %{SOURCE1} %{buildroot}%{_sysconfdir}/pam.d/hyprlock"
            if name == "hyprlock" else ":"
        ),
        "PROTOCOL_MACROS": (
            "BuildRequires:  cmake-rpm-macros" if cmake_protocols else "BuildRequires:  meson"
        ),
        "PROTOCOL_BUILD": (
            "%cmake -G Ninja -DFETCHCONTENT_FULLY_DISCONNECTED=ON \\\n"
            "    -DCMAKE_INSTALL_DATADIR:STRING=share \\\n"
            "    --trace-expand --trace-format=json-v1 --trace-redirect=%{_topdir}/trace.jsonl\n"
            "%cmake_build"
            if cmake_protocols else "%meson --wrap-mode=nodownload\n%meson_build"
        ),
        "PROTOCOL_INSTALL": "%cmake_install" if cmake_protocols else "%meson_install",
    }
    for token, value in values.items():
        text = text.replace(f"@{token}@", value)
    if re.search(r"@[A-Z_]+@", text):
        raise PipelineError(f"Unexpanded spec template token for {name}")
    # Lua's archive adapter has no source timestamp (epoch=0). RPM rejects
    # the Unix epoch as a changelog date; use this fixed initial packaging
    # date instead of the current wall clock, preserving deterministic specs.
    date = (
        datetime.fromtimestamp(source["epoch"], timezone.utc)
        if source["epoch"] else datetime(2026, 10, 24, tzinfo=timezone.utc)
    )
    # Use fixed English names regardless of the resolver host's locale.
    weekday = (
        "Mon",
        "Tue",
        "Wed",
        "Thu",
        "Fri",
        "Sat",
        "Sun",
    )[date.weekday()]
    month = (
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
        "Jul",
        "Aug",
        "Sep",
        "Oct",
        "Nov",
        "Dec",
    )[date.month - 1]
    return (
        text.rstrip()
        + f"\n\n%changelog\n* {weekday} {month} {date.day:02d} {date.year} "
        + f"Hyprland COPR maintainers - {source['version']}-{release}\n"
        + f"- Package verified upstream source {source['version']} for the coordinated stack.\n"
    )


def prepare(source, requirements, release, directory):
    directory = Path(directory)
    for child in ("SOURCES", "SPECS", "BUILD", "BUILDROOT", "RPMS", "SRPMS"):
        (directory / child).mkdir(parents=True, exist_ok=True)
    archive = Path(source["archive"])
    if sha256(archive) != source["sha256"]:
        raise PipelineError(f"Source archive integrity failure: {archive.name}")
    shutil.copyfile(
        archive, directory / "SOURCES" / f"{source['name']}-{source['version']}.tar.gz"
    )
    if source["name"] == "hyprlock":
        shutil.copyfile(ROOT / "packaging/hyprlock.pam", directory / "SOURCES/hyprlock.pam")
    if source["name"] == "glaze":
        for original, staged in (
            ("main.cpp", "glaze-smoke.cpp"),
            ("CMakeLists.txt", "glaze-smoke.cmake"),
        ):
            shutil.copyfile(ROOT / "tests/glaze" / original, directory / "SOURCES" / staged)
    spec = directory / "SPECS" / f"{'lua55' if source['name'] == 'lua' else source['name']}.spec"
    spec.write_text(render_spec(source, requirements, release))
    return spec
