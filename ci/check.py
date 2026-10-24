"""Repository-installed quality tools, run inside the Fedora checks image."""

from pathlib import Path
import subprocess


def check_shell_scripts(root=Path(".")):
    for directory in ("ci", "container", "deploy", "hyprland_copr", "packaging", "tests"):
        for path in sorted((root / directory).rglob("*")):
            if path.is_file() and path.suffix in (".sh", ".bash"):
                subprocess.run(["bash", "-n", str(path)], check=True)
                subprocess.run(["shellcheck", "--external-sources", str(path)], check=True)



def main():
    subprocess.run(["ruff", "check", "hyprland_copr", "container", "deploy", "tests", "ci"], check=True)
    subprocess.run(["python3", "-m", "unittest", "discover", "-s", "tests", "-v"], check=True)
    subprocess.run([
        "python3", "-m", "compileall", "-q", "hyprland_copr", "container", "deploy", "tests", "ci"
    ], check=True)
    check_shell_scripts()
    print("Ruff, unit tests, Python/shell syntax and ShellCheck checks passed")


if __name__ == "__main__":
    main()
