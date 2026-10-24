"""Run only inside the offline, unprivileged build container."""

import json
import os
from pathlib import Path
import subprocess
import sys


def main(work=Path("/work")):
    if os.geteuid() == 0:
        raise RuntimeError("Refusing upstream compilation as root")
    spec = work / "SPECS" / sys.argv[1]
    subprocess.run(["rpmlint", str(spec)], check=True)
    subprocess.run(
        ["rpmbuild", "-ba", "--define", f"_topdir {work}", str(spec)],
        check=True,
    )
    binaries = sorted((work / "RPMS").rglob("*.rpm"))
    sources = sorted((work / "SRPMS").glob("*.src.rpm"))
    if not binaries or len(sources) != 1:
        raise RuntimeError("Expected binary RPMs and exactly one source RPM for linting")
    # Default rpmlint policy fails on errors; warnings remain visible in the
    # build log. Do not use permissive mode or blanket diagnostic filters.
    subprocess.run(["rpmlint", str(spec), *map(str, sources + binaries)], check=True)
    packages = []
    for path in binaries:
        metadata = subprocess.check_output([
            "rpm", "-qp", "--qf",
            "%{NAME}\\t%{EPOCHNUM}\\t%{VERSION}\\t%{RELEASE}\\t%{ARCH}",
            str(path),
        ], text=True)
        name, epoch, version, release, arch = metadata.split("\t")
        packages.append({
            "file": str(path.relative_to(work)),
            "name": name,
            "epoch": epoch,
            "version": version,
            "release": release,
            "arch": arch,
        })
    (work / "packages.json").write_text(json.dumps(packages, indent=4) + "\n")


if __name__ == "__main__":
    main()
