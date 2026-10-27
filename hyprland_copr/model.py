"""Version constraints and the supported source boundary."""

from dataclasses import asdict, dataclass
import re


class PipelineError(RuntimeError):
    """An actionable, fail-closed pipeline failure."""


def version_key(value):
    match = re.fullmatch(r"v?(\d+(?:\.\d+){0,3})", value)
    if not match:
        raise PipelineError(f"Not a supported stable numeric version: {value!r}")
    return tuple(int(part) for part in match[1].split(".")) + (0,) * (
        4 - len(match[1].split("."))
    )


@dataclass(frozen=True)
class Requirement:
    capability: str
    constraints: tuple = ()
    origin: str = ""
    managed: str = ""

    def accepts(self, version):
        actual = version_key(version)
        comparisons = {
            ">=": lambda expected: actual >= expected,
            ">": lambda expected: actual > expected,
            "<=": lambda expected: actual <= expected,
            "<": lambda expected: actual < expected,
            "=": lambda expected: actual == expected,
        }
        return all(comparisons[op](version_key(wanted)) for op, wanted in self.constraints)

    def expressions(self):
        if not self.constraints:
            return [self.capability]
        return [
            f"{self.capability} {operator} {version}"
            for operator, version in self.constraints
        ]

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        return cls(
            value["capability"],
            tuple(tuple(pair) for pair in value.get("constraints", [])),
            value.get("origin", ""),
            value.get("managed", ""),
        )


REPOSITORIES = {
    "hyprland": "hyprwm/Hyprland",
    "aquamarine": "hyprwm/aquamarine",
    "hyprutils": "hyprwm/hyprutils",
    "hyprgraphics": "hyprwm/hyprgraphics",
    "hyprcursor": "hyprwm/hyprcursor",
    "hyprwayland-scanner": "hyprwm/hyprwayland-scanner",
    "hyprlang": "hyprwm/hyprlang",
    "hyprwire": "hyprwm/hyprwire",
    "hyprtoolkit": "hyprwm/hyprtoolkit",
    "hyprpaper": "hyprwm/hyprpaper",
    "hyprlock": "hyprwm/hyprlock",
    "hypridle": "hyprwm/hypridle",
    "hyprpicker": "hyprwm/hyprpicker",
    "hyprsunset": "hyprwm/hyprsunset",
    "hyprland-guiutils": "hyprwm/hyprland-guiutils",
    "uwsm": "Vladimir-csp/uwsm",
    "xdg-desktop-portal-hyprland": "hyprwm/xdg-desktop-portal-hyprland",
    "glaze": "stephenberry/glaze",
    "hyprland-protocols": "hyprwm/hyprland-protocols",
}

COMPANIONS = (
    "hyprpaper",
    "hyprlock",
    "hypridle",
    "hyprpicker",
    "hyprsunset",
    "hyprland-guiutils",
    "uwsm",
    "xdg-desktop-portal-hyprland",
)
ROOT_COMPONENTS = (
    "hyprland",
    *COMPANIONS,
    "hyprtoolkit",
    "glaze",
    "lua",
)

MANAGED_CAPABILITIES = {
    **{f"pkgconfig({name})": name for name in REPOSITORIES},
    "cmake(hyprwayland-scanner)": "hyprwayland-scanner",
    "cmake(glaze)": "glaze",
    "pkgconfig(lua55)": "lua",
    "pkgconfig(hyprland-protocols)": "hyprland-protocols",
}


SOURCE_POLICY = "hypr-prefix-v1"
HYPR_ALIASES = {
    "hyprwire-scanner": "hyprwire",
    **{name: "hyprland-guiutils" for name in (
        "hyprland-dialog", "hyprland-donate-screen", "hyprland-update-screen",
        "hyprland-welcome", "hyprland-run",
    )},
}


def hypr_project(name):
    """Canonical project identity, never a URL or an arbitrary filesystem path."""
    if not isinstance(name, str):
        return ""
    name = name.lower()
    if not name.startswith("hypr"):
        return ""
    if not re.fullmatch(r"hypr[a-z0-9]*(?:-[a-z0-9]+)*", name):
        raise PipelineError(f"Unsafe hypr dependency name: {name!r}")
    for suffix in ("-devel", "-libs"):
        name = name.removesuffix(suffix)
    return HYPR_ALIASES.get(name, name)


def source_repository(name):
    if name in REPOSITORIES:
        return REPOSITORIES[name]
    project = hypr_project(name)
    if project != name or not project:
        raise PipelineError(f"Unsupported source project: {name!r}")
    return f"hyprwm/{project}"


def managed_project(capability):
    """Map pkg-config/CMake/RPM/program requirements to their source owner."""
    match = re.fullmatch(r"(?:pkgconfig|cmake)\(([^()]+)\)", capability)
    name = match[1] if match else capability.removeprefix("/usr/bin/")
    project = hypr_project(name)
    return project or MANAGED_CAPABILITIES.get(capability, "")


def normalize_requirement(requirement):
    owner = managed_project(requirement.capability)
    if owner and requirement.managed and owner != requirement.managed:
        raise PipelineError(f"Conflicting source ownership: {requirement.capability}")
    owner = owner or requirement.managed
    if owner and owner != "lua":
        source_repository(owner)
    return Requirement(
        requirement.capability,
        requirement.constraints,
        requirement.origin,
        owner,
    )


def root_package(component):
    return {
        "lua": "lua55",
        "glaze": "glaze-devel",
    }.get(component, component)

def source_versions(resolution):
    return {
        name: source["version"]
        for name, source in sorted(resolution["sources"].items())
    }


def rpm_release(resolution, release):
    value = str(release)
    if resolution["root"] == "hyprland":
        value += ".hypr" + resolution["sources"]["hyprland"]["version"]
        if len(resolution.get("primaries", [])) > 1:
            generation = resolution.get("generation", 1)
            if type(generation) is not int or generation < 1:
                raise PipelineError("Invalid coordinated stack generation")
            value += ".stack" + str(generation)
    return value


def topological_order(graph):
    result = []
    active = set()
    done = set()

    def visit(name):
        if name in active:
            raise PipelineError(f"Dependency cycle involving {name}")
        if name in done:
            return
        active.add(name)
        for dependency in sorted(graph[name]):
            if dependency in graph:
                visit(dependency)
        active.remove(name)
        done.add(name)
        result.append(name)

    for name in sorted(graph):
        visit(name)
    return result
