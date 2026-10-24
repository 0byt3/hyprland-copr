"""Offline checks against installed RPMs in a disposable container."""

import json
from pathlib import Path
import subprocess
import sys

import rpm
from smoke import check_applications, check_glaze, check_hyprland


def output(arguments):
    return subprocess.check_output(arguments, text=True).strip()

def lint_candidates(manifest):
    files = [
        *[build["srpm"] for build in manifest["builds"]],
        *[package["file"] for package in manifest["packages"]],
    ]
    if not files:
        raise RuntimeError("No candidate RPMs to lint")
    subprocess.run(["rpmlint", *[str(Path("/work") / path) for path in files]], check=True)


def main():
    manifest = json.loads(Path(sys.argv[1]).read_text())
    lint_candidates(manifest)
    baseline = json.loads(Path("/work/baseline.json").read_text())
    candidates = manifest["packages"]
    for package in candidates:
        if package["name"].endswith(("-debuginfo", "-debugsource")):
            continue
        previous = baseline.get(package["name"])
        if previous and rpm.labelCompare(
            (package["epoch"], package["version"], package["release"]), tuple(previous)
        ) <= 0:
            raise RuntimeError(f"Candidate does not upgrade baseline: {package['name']}")
    subprocess.run(["dnf", "--cacheonly", "check"], check=True)
    if any(package["name"] == "lua55" for package in candidates):
        subprocess.run(["bash", "/tests/lua55/validate-installed.sh"], check=True)
    check_applications(candidates)
    check_glaze(candidates)
    if manifest["root"] == "hyprland":
        check_hyprland(next(
            package["version"] for package in candidates if package["name"] == "hyprland"
        ))
        dynamic = output(["readelf", "-d", "/usr/bin/Hyprland"])
        if "liblua5.5.so.0" not in dynamic or "liblua-5.4" in dynamic:
            raise RuntimeError("Hyprland is not linked to the co-installable Lua55 runtime")
    for package in candidates:
        if package["name"].endswith(("-debuginfo", "-debugsource")):
            continue
        for name in output(["rpm", "-ql", package["name"]]).splitlines():
            path = Path(name)
            if not path.is_file() or path.is_symlink():
                continue
            with path.open("rb") as stream:
                is_elf = stream.read(4) == b"\x7fELF"
            if not is_elf:
                continue
            dynamic = subprocess.run(["ldd", str(path)], capture_output=True, text=True)
            if "not found" in dynamic.stdout + dynamic.stderr:
                raise RuntimeError(f"Unresolved library dependency in {path}")
    print("Offline rpmlint, RPM transaction, ELF, Lua, companion CLI/assets and Glaze checks passed")


if __name__ == "__main__":
    main()
