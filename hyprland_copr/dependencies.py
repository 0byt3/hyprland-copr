"""Seed dependency discovery from supported CMake forms, never regex a whole build as truth."""

from pathlib import Path
import re
import shlex

from .model import PipelineError, Requirement, managed_project, normalize_requirement, version_key
from .components import (
    APPLICATIONS,
    PORTAL_PROTOCOL_FILES,
    PORTAL_PROTOCOL_MINIMUM,
    protocol_build_system,
)


PKG_FLAGS = {
    "REQUIRED",
    "QUIET",
    "IMPORTED_TARGET",
    "GLOBAL",
    "NO_CMAKE_PATH",
    "NO_CMAKE_ENVIRONMENT_PATH",
}


def commands(text, origin):
    """Read balanced CMake invocations, respecting strings and comments."""
    text = re.sub(r"#\[(=*)\[.*?\]\1\]", "", text, flags=re.DOTALL)
    cursor = 0
    pattern = re.compile(r"(?m)^\s*([A-Za-z_]\w*)\s*\(")
    while match := pattern.search(text, cursor):
        start = match.end()
        index = start
        depth = 1
        quoted = False
        escaped = False
        while index < len(text) and depth:
            character = text[index]
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = not quoted
            elif not quoted and character == "#":
                newline = text.find("\n", index)
                index = len(text) if newline < 0 else newline
                continue
            elif not quoted and character == "(":
                depth += 1
            elif not quoted and character == ")":
                depth -= 1
            index += 1
        if depth:
            raise PipelineError(f"Unbalanced CMake declaration in {origin}")
        lexer = shlex.shlex(text[start:index - 1], posix=True)
        lexer.whitespace_split = True
        lexer.commenters = "#"
        yield (
            match[1].lower(),
            list(lexer),
            f"{origin}:{text.count(chr(10), 0, match.start()) + 1}",
        )
        cursor = index


def expand(value, variables, origin):
    for _ in range(20):
        expanded = re.sub(
            r"\$\{([^}]+)\}",
            lambda match: variables.get(match[1], match[0]),
            value,
        )
        if expanded == value:
            break
        value = expanded
    if "$" in value:
        raise PipelineError(f"Unsupported dynamic requirement {value!r} at {origin}")
    return value


def pkg_requirement(value, origin):
    match = re.fullmatch(
        r"\s*([A-Za-z0-9_.+-]+)\s*(?:(>=|<=|=|>|<)\s*([0-9.]+))?\s*", value
    )
    if not match:
        raise PipelineError(f"Unsupported pkg-config requirement {value!r} at {origin}")
    capability = f"pkgconfig({match[1]})"
    constraints = ()
    if match[2]:
        version_key(match[3])
        constraints = ((match[2], match[3]),)
    return Requirement(
        capability,
        constraints,
        origin,
        managed_project(capability),
    )


def cmake_version(value, origin):
    if "..." in value:
        low, high = value.split("...", 1)
        exclusive = high.startswith("<")
        high = high.removeprefix("<")
        version_key(low)
        version_key(high)
        return ((">=", low), ("<" if exclusive else "<=", high))
    try:
        version_key(value)
    except PipelineError:
        return ()
    return ((">=", value),)


def pkg_requirements(arguments, variables, origin):
    required = "REQUIRED" in arguments
    found = []
    for argument in arguments[1:]:
        if argument in PKG_FLAGS:
            continue
        try:
            values = expand(argument, variables, origin).split(";")
        except PipelineError:
            if required:
                raise
            # Expanded configure traces must catch any unresolved optional hypr probe.
            continue
        for value in values:
            requirement = pkg_requirement(value, origin)
            if required or requirement.managed.startswith("hypr"):
                found.append(requirement)
    return found


FIND_KEYWORDS = {
    "BYPASS_PROVIDER",
    "CMAKE_FIND_ROOT_PATH_BOTH",
    "COMPONENTS",
    "CONFIG",
    "CONFIGS",
    "DOC",
    "EXACT",
    "GLOBAL",
    "HINTS",
    "NAMES_PER_DIR",
    "NO_CACHE",
    "NO_CMAKE_BUILDS_PATH",
    "NO_CMAKE_ENVIRONMENT_PATH",
    "NO_CMAKE_FIND_ROOT_PATH",
    "NO_CMAKE_INSTALL_PREFIX",
    "NO_CMAKE_PACKAGE_REGISTRY",
    "NO_CMAKE_PATH",
    "NO_CMAKE_SYSTEM_PACKAGE_REGISTRY",
    "NO_CMAKE_SYSTEM_PATH",
    "NO_DEFAULT_PATH",
    "NO_MODULE",
    "NO_PACKAGE_ROOT_PATH",
    "NO_POLICY_SCOPE",
    "NO_SYSTEM_ENVIRONMENT_PATH",
    "ONLY_CMAKE_FIND_ROOT_PATH",
    "OPTIONAL",
    "OPTIONAL_COMPONENTS",
    "PATHS",
    "PATH_SUFFIXES",
    "QUIET",
    "REGISTRY_VIEW",
    "REQUIRED",
    "UNWIND_INCLUDE",
}


def reject_hypr_alternatives(arguments, variables, origin):
    if "NAMES" not in arguments:
        return
    for argument in arguments[arguments.index("NAMES") + 1:]:
        if argument in FIND_KEYWORDS:
            break
        try:
            names = expand(argument, variables, origin).split(";")
        except PipelineError:
            # The expanded trace must reject dynamically discovered hypr alternatives.
            continue
        if any(managed_project(f"cmake({name})").startswith("hypr") for name in names):
            raise PipelineError(f"Unsupported hypr NAMES alternatives at {origin}")


def program_requirement(arguments, variables, origin):
    reject_hypr_alternatives(arguments, variables, origin)
    required = "REQUIRED" in arguments
    if len(arguments) < 2 or arguments[1] == "NAMES":
        if required:
            raise PipelineError(f"Unsupported required program alternatives at {origin}")
        return None
    try:
        program = expand(arguments[1], variables, origin)
    except PipelineError:
        if required:
            raise
        return None
    capability = f"/usr/bin/{program}"
    if not required and not managed_project(capability).startswith("hypr"):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_.+-]+", program):
        raise PipelineError(f"Unsupported required program at {origin}")
    return normalize_requirement(Requirement(capability, origin=origin))


BUILD_POLICY = {
    "BUILD_TESTING": "OFF",
    "WITH_TESTS": "OFF",
    "USE_TRACY": "OFF",
    "NO_XWAYLAND": "OFF",
    "NO_HYPRPM": "OFF",
    "NO_UWSM": "ON",
    "CMAKE_BUILD_TYPE": "Release",
    "CMAKE_SYSTEM_NAME": "Linux",
    "CMAKE_CXX_COMPILER_ID": "GNU",
    "WIN32": "OFF",
    "APPLE": "OFF",
    "UNIX": "ON",
    "MSVC": "OFF",
    "ANDROID": "OFF",
    "glaze_DEVELOPER_MODE": "OFF",
    "glaze_ENABLE_FUZZING": "OFF",
    "glaze_BUILD_EXAMPLES": "OFF",
    "glaze_EETF_FORMAT": "OFF",
    "glaze_ENABLE_SSL": "OFF",
    "glaze_ENABLE_REFLECTION26": "OFF",
}


def condition(arguments):
    """Evaluate only known build-policy predicates; unknown branches stay visible."""
    for operator in ("OR", "AND"):
        if operator in arguments:
            index = arguments.index(operator)
            left = condition(arguments[:index])
            right = condition(arguments[index + 1:])
            if operator == "OR":
                return True if True in (left, right) else (
                    False if left is False and right is False else None
                )
            return False if False in (left, right) else (
                True if left is True and right is True else None
            )
    if arguments and arguments[0] == "NOT":
        result = condition(arguments[1:])
        return None if result is None else not result
    if len(arguments) == 1:
        value = BUILD_POLICY.get(arguments[0], arguments[0]).upper()
        if value in ("ON", "TRUE", "1"):
            return True
        if value in ("OFF", "FALSE", "0"):
            return False
    if len(arguments) == 3 and arguments[0] in BUILD_POLICY:
        left = BUILD_POLICY[arguments[0]]
        operator, right = arguments[1:]
        if operator == "STREQUAL":
            return left == right
        if operator == "MATCHES":
            return re.search(right, left) is not None
    return None


def active_declarations(declarations):
    """Conservatively retain unknown conditions, excluding proven disabled branches."""
    stack = []
    enabled = True
    for command, arguments, origin in declarations:
        if command == "if":
            value = condition(arguments)
            stack.append([enabled, value])
            enabled = enabled and value is not False
        elif command in ("elseif", "else"):
            if not stack:
                raise PipelineError(f"Unmatched {command} at {origin}")
            parent, previous = stack[-1]
            value = condition(arguments) if command == "elseif" else True
            enabled = parent and previous is not True and value is not False
            stack[-1][1] = (
                True if previous is True or value is True else
                False if previous is False and value is False else None
            )
        elif command == "endif":
            if not stack:
                raise PipelineError(f"Unmatched endif at {origin}")
            enabled = stack.pop()[0]
        elif enabled:
            yield command, arguments, origin
    if stack:
        raise PipelineError("Unclosed CMake conditional")


def package_requirement(arguments, origin, variables):
    package = arguments[0]
    if package in ("Threads", "PkgConfig", "Git"):
        return []
    if package == "OpenGL":
        return [Requirement("libglvnd-devel", origin=origin)]
    if package == "Python3":
        return [Requirement("python3", origin=origin)]
    capability = f"cmake({package})"
    bounds = cmake_version(expand(arguments[1], variables, origin), origin) if (
        len(arguments) > 1
    ) else ()
    if "EXACT" in arguments and bounds:
        bounds = (("=", bounds[0][1]),)
    if package in ("QT", "Qt6"):
        if package == "QT":
            if "NAMES" not in arguments:
                raise PipelineError(f"Unsupported Qt alternatives at {origin}")
            alternatives = arguments[arguments.index("NAMES") + 1:]
            if (
                len(alternatives) < 2
                or alternatives[0] != "Qt6"
                or alternatives[1] not in ("REQUIRED", "COMPONENTS")
            ):
                raise PipelineError(f"Unsupported Qt alternatives at {origin}")
        components = []
        if "COMPONENTS" in arguments:
            components = arguments[arguments.index("COMPONENTS") + 1:]
            if any(not re.fullmatch(r"[A-Za-z0-9]+", value) for value in components):
                raise PipelineError(f"Unsupported Qt components at {origin}")
        return [
            Requirement("cmake(Qt6)", bounds, origin),
            *[
                Requirement(f"cmake(Qt6{component})", bounds, origin)
                for component in components
            ],
        ]
    return [Requirement(
        capability, bounds, origin, managed_project(capability)
    )]


def discover(source):
    name = source["name"]
    if name == "lua":
        return [
            Requirement("gcc", origin="Lua Linux build adapter"),
            Requirement("make", origin="Lua Linux build adapter"),
            Requirement("pkgconf-pkg-config", origin="Lua embedding test adapter"),
        ]
    root = Path(source["directory"])
    if name == "hyprland-protocols" and protocol_build_system(root) == "meson":
        text = (root / "meson.build").read_text()
        minimum = re.search(r"meson_version:\s*'>=\s*([0-9.]+)'", text)
        if not minimum or re.search(r"\b(?:dependency|subproject|run_target)\s*\(", text):
            raise PipelineError("Protocol Meson layout changed; review the bounded source adapter")
        return [
            Requirement("meson", ((">=", minimum[1]),), "Protocol Meson adapter"),
            Requirement("ninja-build", origin="Protocol Meson adapter"),
            Requirement("pkgconf-pkg-config", origin="Protocol metadata validation"),
        ]
    if name == "uwsm":
        text = (root / "meson.build").read_text() if (root / "meson.build").is_file() else ""
        minimum = re.search(r"meson_version:\s*'>=\s*([0-9.]+)'", text)
        if not minimum:
            raise PipelineError("uwsm Meson layout changed; review the bounded source adapter")
        return [
            Requirement("meson", ((">=", minimum[1]),), "uwsm Meson adapter"),
            Requirement("ninja-build", origin="uwsm Meson adapter"),
            Requirement("pkgconf-pkg-config", origin="uwsm Meson adapter"),
            Requirement("pkgconfig(scdoc)", ((">=", "1.9.2"),), "uwsm manpage adapter"),
            Requirement("python3", ((">=", "3.10"),), "uwsm Python adapter"),
            *[
                Requirement(package, origin="uwsm runtime packaging policy")
                for package in APPLICATIONS["uwsm"]["runtime"]
            ],
        ]
    if name == "glaze":
        # Only these library entrypoints execute with developer mode disabled.
        # Optional Asio/Erlang discovery and developer helpers are not active.
        paths = [
            root / path
            for path in (
                "CMakeLists.txt",
                "cmake/prelude.cmake",
                "cmake/project-is-top-level.cmake",
                "cmake/variables.cmake",
                "cmake/install-rules.cmake",
            )
        ]
    else:
        paths = sorted(set(root.rglob("CMakeLists.txt")) | set(root.rglob("*.cmake")))
    paths = [
        path for path in paths
        if not {"subprojects", "tests", "examples", ".git"}.intersection(
            path.relative_to(root).parts[:-1]
        )
    ]
    declarations = [
        item
        for path in paths
        for item in active_declarations(commands(path.read_text(), str(path.relative_to(root))))
    ]
    if not declarations:
        raise PipelineError(f"No supported CMake build declarations for {name}")
    variables = {
        "CMAKE_SOURCE_DIR": str(root),
        "PROJECT_SOURCE_DIR": str(root),
        "CMAKE_CURRENT_SOURCE_DIR": str(root),
    }
    # Resolve literal version declarations and their references to a fixed point.
    for _ in range(10):
        for command, arguments, origin in declarations:
            if command == "set" and len(arguments) >= 2:
                value = ";".join(arguments[1:])
                try:
                    variables[arguments[0]] = expand(value, variables, origin)
                except PipelineError:
                    pass
    fetched = {
        arguments[0].lower()
        for command, arguments, _ in declarations
        if command == "fetchcontent_declare" and arguments
    }
    requirements = [
        Requirement(
            "gcc" if name == "hyprland-protocols" else "gcc-c++",
            origin=f"{name}: Fedora compiler adapter",
        ),
        Requirement("cmake", origin=f"{name}: CMake build adapter"),
        Requirement("ninja-build", origin=f"{name}: Ninja build adapter"),
        Requirement("pkgconf-pkg-config", origin=f"{name}: pkg-config adapter"),
    ]
    for command, arguments, origin in declarations:
        if command == "cmake_minimum_required":
            if len(arguments) >= 2 and arguments[0] == "VERSION":
                requirements.append(Requirement(
                    "cmake", cmake_version(arguments[1], origin), origin
                ))
        elif command in ("pkg_check_modules", "pkg_search_module"):
            if command == "pkg_search_module":
                if "REQUIRED" not in arguments:
                    if pkg_requirements(arguments, variables, origin):
                        raise PipelineError(f"Unsupported hypr module alternatives at {origin}")
                    continue
                if not arguments or arguments[0] != "LUA":
                    raise PipelineError(f"Unsupported required pkg_search_module at {origin}")
                if not any("5.5" in value or "55" in value for value in arguments):
                    raise PipelineError(f"Lua dependency changed; review adapter at {origin}")
                requirements.append(Requirement(
                    "pkgconfig(lua55)",
                    ((">=", "5.5"), ("<", "5.6")),
                    origin,
                    "lua",
                ))
                continue
            requirements.extend(pkg_requirements(arguments, variables, origin))
        elif command == "find_package" and arguments:
            reject_hypr_alternatives(arguments, variables, origin)
            try:
                package = expand(arguments[0], variables, origin)
            except PipelineError:
                if "REQUIRED" in arguments:
                    raise
                continue
            if (
                "REQUIRED" not in arguments
                and package.lower() not in fetched
                and not managed_project(f"cmake({package})").startswith("hypr")
            ):
                continue
            requirements.extend(package_requirement([package, *arguments[1:]], origin, variables))
        elif command == "find_program":
            requirement = program_requirement(arguments, variables, origin)
            if requirement:
                requirements.append(requirement)
    # Enforce tools invoked outside find_package/pkg_check_modules.
    if name == "hyprland":
        requirements.append(Requirement(
            "/usr/bin/hyprwire-scanner", origin="hyprctl protocol generation", managed="hyprwire"
        ))
        requirements.append(Requirement(
            "pkgconfig(hyprland-protocols)",
            ((">=", "0.7.0"),),
            origin="Pinned protocol submodule adapter",
            managed="hyprland-protocols",
        ))
        requirements.append(Requirement("python3", origin="Required Lua stub generator"))
    if name in (
        "hyprland", "aquamarine", "hyprpaper", "hyprlock", "hypridle",
        "hyprpicker", "hyprsunset",
        "xdg-desktop-portal-hyprland", "hyprtoolkit",
    ):
        requirements.append(Requirement(
            "pkgconfig(wayland-scanner)", origin="Core Wayland XML generation adapter"
        ))
    if name in ("hyprpicker", "hyprsunset"):
        requirements.append(Requirement(
            "pkgconfig(wayland-cursor)", origin=f"{name} direct-link adapter"
        ))
    if name == "hyprpaper":
        requirements.extend([
            Requirement(
                "/usr/bin/hyprwire-scanner",
                origin="hyprpaper protocol generation",
                managed="hyprwire",
            ),
            *[
                Requirement(f"pkgconfig({module})", origin="hyprpaper direct-link adapter")
                for module in (
                    "opengl",
                    "glesv2",
                    "libmagic",
                    "wayland-cursor",
                    "wayland-protocols",
                )
            ],
        ])
    if name == "hyprlock":
        requirements.append(Requirement("pam-devel", origin="hyprlock PAM adapter"))
    if name == "xdg-desktop-portal-hyprland":
        requirements.extend([
            Requirement(
                "pkgconfig(sdbus-c++)", ((">=", "2.0.0"),),
                origin="Required system sdbus-c++ adapter",
            ),
            Requirement(
                "pkgconfig(hyprland-protocols)",
                ((">=", PORTAL_PROTOCOL_MINIMUM),),
                origin="Required external protocol definitions adapter",
                managed="hyprland-protocols",
            ),
            *[
                Requirement(
                    f"/usr/share/hyprland-protocols/protocols/{filename}",
                    origin="Portal external protocol generation",
                    managed="hyprland-protocols",
                )
                for filename in PORTAL_PROTOCOL_FILES
            ],
        ])
    if name == "hyprwire":
        requirements.append(Requirement("pkgconfig(pugixml)", origin="hyprwire scanner adapter"))
    for package in APPLICATIONS.get(name, {}).get("runtime", ()):
        requirements.append(Requirement(package, origin=f"{name} runtime packaging policy"))
    if name == "glaze":
        requirements.append(Requirement("cmake-filesystem", origin="Glaze CMake packaging policy"))
    return list(dict.fromkeys(normalize_requirement(value) for value in requirements))


def trace_requirements(path):
    """Inspect expanded configure traces for requirements absent from the seed."""
    import json

    found = []
    seen = 0
    for line in Path(path).read_text().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise PipelineError("Malformed CMake configure trace") from error
        if not isinstance(event, dict):
            raise PipelineError("Invalid CMake configure trace event")
        if "cmd" not in event and "version" in event:
            continue
        if not isinstance(event.get("cmd"), str) or not isinstance(event.get("args"), list):
            raise PipelineError("Invalid CMake configure trace event")
        seen += 1
        command = event.get("cmd", "").lower()
        arguments = event.get("args", [])
        # Installed CMake modules' probes are transitive packaging concerns,
        # not declarations in the acquired source tree.
        if event.get("file", "").startswith(("/usr/", "/lib/", "/lib64/")):
            continue
        origin = f"{event.get('file')}:{event.get('line')}"
        if command == "pkg_check_modules":
            found.extend(pkg_requirements(arguments, {}, origin))
        elif command == "find_package" and arguments:
            reject_hypr_alternatives(arguments, {}, origin)
            if (
                "REQUIRED" in arguments
                or managed_project(f"cmake({arguments[0]})").startswith("hypr")
            ):
                found.extend(package_requirement(arguments, origin, {}))
        elif command == "pkg_search_module":
            if "REQUIRED" not in arguments:
                if pkg_requirements(arguments, {}, origin):
                    raise PipelineError(f"Unsupported hypr module alternatives at {origin}")
                continue
            if not arguments or arguments[0] != "LUA" or "lua55" not in arguments:
                raise PipelineError(f"Unsupported required module alternatives at {origin}")
            found.append(Requirement(
                "pkgconfig(lua55)", ((">=", "5.5"), ("<", "5.6")), origin, "lua"
            ))
        elif command == "find_program":
            requirement = program_requirement(arguments, {}, origin)
            if requirement:
                found.append(requirement)
    if not seen:
        raise PipelineError("Empty CMake configure trace")
    return found
