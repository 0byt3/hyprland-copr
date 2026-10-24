"""Stable release discovery and reproducible, recursively populated snapshots."""

import gzip
import hashlib
import json
from pathlib import Path
import re
import tarfile
import urllib.error
import urllib.parse
import urllib.request

from .model import PipelineError, source_repository, version_key
from .util import run, sha256


def download(url, destination=None):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise PipelineError("Source downloads require credential-free HTTPS")
    request = urllib.request.Request(url, headers={"User-Agent": "hyprland-copr/0.1"})
    with urllib.request.urlopen(request, timeout=120) as response:
        if urllib.parse.urlsplit(response.url).scheme != "https":
            raise PipelineError("Refusing non-HTTPS redirect")
        content = response.read(256 * 1024 * 1024 + 1)
    if len(content) > 256 * 1024 * 1024:
        raise PipelineError("Source response exceeds the size limit")
    if destination:
        Path(destination).write_bytes(content)
    return content


def stable_releases(repository, limit=10):
    releases = []
    for page in range(1, 11):
        values = json.loads(download(
            f"https://api.github.com/repos/{repository}/releases?per_page=100&page={page}"
        ))
        if not isinstance(values, list):
            raise PipelineError(f"Invalid release response for {repository}")
        for value in values:
            if value["draft"] or value["prerelease"]:
                continue
            tag = value["tag_name"]
            try:
                version_key(tag)
            except PipelineError:
                continue
            releases.append({
                "version": tag.removeprefix("v"),
                "tag": tag,
                "repository": repository,
            })
        if len(releases) >= limit or len(values) < 100:
            break
    return sorted(releases, key=lambda value: version_key(value["version"]), reverse=True)[:limit]


def lua_releases():
    page = download("https://www.lua.org/ftp/").decode()
    matches = re.findall(
        r'lua-(5\.5\.\d+)\.tar\.gz.*?([a-f0-9]{64})',
        page,
        re.DOTALL,
    )
    if not matches:
        raise PipelineError("Cannot discover Lua 5.5 archives and official SHA256 checksums")
    return sorted(
        [
            {
                "version": version,
                "url": f"https://www.lua.org/ftp/lua-{version}.tar.gz",
                "sha256": checksum,
            }
            for version, checksum in set(matches)
        ],
        key=lambda value: version_key(value["version"]),
        reverse=True,
    )


def git(arguments, cwd=None):
    return run([
        "git",
        "--no-pager",
        "-c",
        "protocol.file.allow=never",
        "-c",
        "protocol.ext.allow=never",
        "-c",
        "protocol.ssh.allow=never",
        *arguments,
    ], cwd=cwd)


def populate_submodules(directory):
    """Validate each level before following any nested submodule URLs."""
    modules = directory / ".gitmodules"
    if not modules.exists():
        return
    entries = git(["config", "--file", str(modules), "--list"]).splitlines()
    for entry in entries:
        key, separator, value = entry.partition("=")
        if not separator:
            raise PipelineError("Malformed submodule metadata")
        if key.endswith(".url"):
            parsed = urllib.parse.urlsplit(value)
            if parsed.scheme != "https" or parsed.hostname != "github.com":
                raise PipelineError(f"Unapproved submodule URL: {value}")
        if key.endswith(".path"):
            path = Path(value)
            if path.is_absolute() or ".." in path.parts:
                raise PipelineError(f"Unsafe submodule path: {value}")
    git(["submodule", "update", "--init", "--depth=1"], cwd=directory)
    paths = [
        entry.partition("=")[2]
        for entry in entries
        if entry.partition("=")[0].endswith(".path")
    ]
    for path in paths:
        populate_submodules(directory / path)


def archive_tree(directory, output, prefix, epoch):
    """Include initialized submodules without Git metadata or host timestamps."""
    def paths(root):
        for child in sorted(root.iterdir()):
            if child.name == ".git":
                continue
            yield child
            if child.is_dir() and not child.is_symlink():
                yield from paths(child)
    with output.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                for path in paths(directory):
                    relative = path.relative_to(directory)
                    if ".git" in relative.parts or path.name == ".git":
                        continue
                    info = archive.gettarinfo(str(path), arcname=f"{prefix}/{relative}")
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    info.mtime = epoch
                    if info.isfile():
                        with path.open("rb") as stream:
                            archive.addfile(info, stream)
                    else:
                        archive.addfile(info)


def validate_source_version(name, directory, expected):
    if name == "glaze":
        from .dependencies import commands

        projects = [
            arguments
            for command, arguments, _ in commands(
                (directory / "CMakeLists.txt").read_text(), "CMakeLists.txt"
            )
            if command == "project" and arguments and arguments[0] == "glaze"
        ]
        if len(projects) != 1 or "VERSION" not in projects[0]:
            raise PipelineError("Glaze project version layout changed; review the source adapter")
        arguments = projects[0]
        index = arguments.index("VERSION") + 1
        actual = arguments[index] if index < len(arguments) else ""
    else:
        path = directory / "VERSION"
        actual = path.read_text().strip() if path.exists() else ""
    if actual != expected:
        raise PipelineError(f"Tag and source version disagree for {name}")


class Sources:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.candidates = {}

    def releases(self, name, limit=10):
        if name not in self.candidates:
            if name == "lua":
                self.candidates[name] = lua_releases()
            else:
                repository = source_repository(name)
                try:
                    self.candidates[name] = stable_releases(repository, limit)
                except urllib.error.HTTPError as error:
                    if error.code != 404:
                        raise
                    error.close()
                    raise PipelineError(
                        f"No official source repository/release endpoint for {name}: {repository}"
                    ) from None
        return self.candidates[name]

    def acquire(self, name, candidate):
        repository = source_repository(name) if name != "lua" else ""
        version_key(candidate["version"])
        if name != "lua" and (
            candidate.get("repository", repository) != repository
            or candidate.get("tag") not in (candidate["version"], "v" + candidate["version"])
        ):
            raise PipelineError(f"Unexpected source repository or stable tag for {name}")
        destination = self.directory / f"{name}-{candidate['version']}"
        archive = destination.with_suffix(destination.suffix + ".tar.gz")
        if name == "lua":
            content = download(candidate["url"])
            if hashlib.sha256(content).hexdigest() != candidate["sha256"]:
                raise PipelineError("Lua source checksum mismatch")
            archive.write_bytes(content)
            if not destination.exists():
                with tarfile.open(archive) as source:
                    source.extractall(self.directory, filter="data")
            return {
                **candidate,
                "name": name,
                "directory": str(destination),
                "archive": str(archive),
                "epoch": 0,
                "commit": "",
                "submodules": [],
            }
        if not destination.exists():
            git([
                "clone",
                "--depth=1",
                "--single-branch",
                "--branch",
                candidate["tag"],
                "--no-checkout",
                "--filter=blob:none",
                f"https://github.com/{repository}.git",
                str(destination),
            ])
            git(["checkout", "--detach", candidate["tag"]], cwd=destination)
        validate_source_version(name, destination, candidate["version"])
        commit = git(["rev-parse", "HEAD"], cwd=destination)
        tag_commit = git(["rev-parse", f"{candidate['tag']}^{{commit}}"], cwd=destination)
        if commit != tag_commit or git(["status", "--porcelain"], cwd=destination):
            raise PipelineError(f"Dirty or mismatched source checkout: {destination}")
        populate_submodules(destination)
        status = git(["submodule", "status", "--recursive"], cwd=destination)
        if any(line.startswith(("-", "+", "U")) for line in status.splitlines()):
            raise PipelineError("Submodule revision mismatch")
        epoch = int(git(["show", "-s", "--format=%ct", "HEAD"], cwd=destination))
        archive_tree(destination, archive, f"{name}-{candidate['version']}", epoch)
        return {
            **candidate,
            "name": name,
            "directory": str(destination),
            "archive": str(archive),
            "sha256": sha256(archive),
            "epoch": epoch,
            "commit": commit,
            "submodules": status.splitlines(),
        }

    def protocols(self, hyprland):
        directory = Path(hyprland["directory"]) / "subprojects/hyprland-protocols"
        version = (directory / "VERSION").read_text().strip()
        version_key(version)
        commit = git(["rev-parse", "HEAD"], cwd=directory)
        epoch = int(git(["show", "-s", "--format=%ct", "HEAD"], cwd=directory))
        archive = self.directory / f"hyprland-protocols-{version}-{commit}.tar.gz"
        archive_tree(directory, archive, f"hyprland-protocols-{version}", epoch)
        return {
            "name": "hyprland-protocols",
            "version": version,
            "directory": str(directory),
            "archive": str(archive),
            "sha256": sha256(archive),
            "commit": commit,
            "epoch": epoch,
            "submodules": [],
        }
