"""Submit exact SRPMs, validate signed candidates, and gate COPR publication.

Operational prerequisite: this automation MUST be the project's sole writer.
Manual builds, webhooks, other pipelines, and manual repository regeneration
are prohibited while a run is pending. COPR's asynchronous regenerate_repos API
has no compare-and-swap or candidate-set parameter. Snapshot checks detect
observed interference; they cannot implement an atomic multi-writer gate.
Project creation must have completed its initial empty public/devel repodata
generation before submit; a missing repository is never assumed to be empty.

Only submit/promote load python-copr or read its credential configuration.
validate uses anonymous, restricted HTTPS GETs and disposable Fedora containers.
No candidate RPM script runs in the process or filesystem holding COPR keys.
"""

import copy
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from .build import Builder, verify_bundle
from .fedora import Container, build_image
from .model import PipelineError, SOURCE_POLICY, root_package
from .transport import PulpRedirect, read_content
from .util import digest_json, read_json, save_json, sha256


API = "https://copr.fedorainfracloud.org"
DOWNLOAD = "https://download.copr.fedorainfracloud.org"
TERMINAL = {
    "succeeded",
    "failed",
    "canceled",
    "skipped",
}
ACTIVE = {
    "importing",
    "pending",
    "starting",
    "running",
    "waiting",
}
RPM_FIELDS = ("name", "epoch", "version", "release", "arch")
NS = {
    "repo": "http://linux.duke.edu/metadata/repo",
    "rpm": "http://linux.duke.edu/metadata/common",
}
HEX256 = re.compile(r"[0-9a-f]{64}")


def identity(package):
    """Canonical, unambiguous NEVRA identity shared by API/XML/RPM headers."""
    values = []
    for key in RPM_FIELDS:
        value = package.get(key)
        if key == "epoch" and value in (None, "", "(none)"):
            value = "0"
        value = str(value) if value is not None else ""
        if not value or not re.fullmatch(r"[A-Za-z0-9_.+~^:-]+", value):
            raise PipelineError(f"Invalid RPM identity field: {key}")
        if key == "epoch" and not value.isdigit():
            raise PipelineError("Invalid RPM epoch")
        values.append(value)
    if values[-1] not in ("x86_64", "noarch"):
        raise PipelineError("Unexpected candidate architecture")
    return "|".join(values)


def filename(package):
    identity(package)
    return "{name}-{version}-{release}.{arch}.rpm".format(**package)


def xml(data):
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise PipelineError("DTD/entity declarations are not accepted in repository metadata")
    try:
        return ET.fromstring(data)
    except ET.ParseError as error:
        raise PipelineError("Malformed repository metadata") from error


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        raise PipelineError("COPR redirects are unsupported; refusing an unpinned destination")


class Publisher:
    def __init__(self, config, bundle, state_path):
        self.config = config
        self.bundle = Path(bundle).resolve()
        self.manifest = verify_bundle(self.bundle)
        self.digest = digest_json(self.manifest)
        self.state_path = Path(state_path).resolve()
        self.policy = config["copr"]
        self.owner = self.policy["owner"]
        self.project = self.policy["project"]
        self.chroot = self.policy["chroot"]
        if (
            not re.fullmatch(r"@?[A-Za-z0-9][A-Za-z0-9_.-]*", self.owner)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", self.project)
            or self.chroot != "fedora-44-x86_64"
        ):
            raise PipelineError("Configure a dedicated Fedora 44 COPR project first")
        self.base = f"{DOWNLOAD}/results/{self.owner}/{self.project}/"
        self.repo = self.base + self.chroot + "/"
        self.storage = "backend"
        self.key_url = self.base + "pubkey.gpg"
        self.records = self.manifest["builds"]
        self.packages = {value["file"]: value for value in self.manifest["packages"]}
        if not self.records or not self.packages or len(self.packages) != len(self.manifest["packages"]):
            raise PipelineError("Empty or duplicate publication package records")
        self.components = {}
        package_files = []
        for record in self.records:
            name = record["component"]
            if name in self.components:
                raise PipelineError("Duplicate build component in manifest")
            source = self._bundle_file(record["srpm"])
            if not source.name.endswith(".src.rpm"):
                raise PipelineError("Only checked source RPM uploads are supported")
            self.components[name] = "lua55" if name == "lua" else name
            package_files.extend(record["packages"])
            for path in record["packages"]:
                if path not in self.packages:
                    raise PipelineError("Build references an unknown RPM")
                value = self.packages[path]
                identity(value)
                if path != "rpms/" + filename(value):
                    raise PipelineError("Candidate filenames must match their RPM identities")
                self._bundle_file(path)
        if len(package_files) != len(set(package_files)) or set(package_files) != set(self.packages):
            raise PipelineError("Each RPM must belong to exactly one source build")
        identities = [identity(value) for value in self.packages.values()]
        if len(identities) != len(set(identities)):
            raise PipelineError("Duplicate candidate NEVRA")
        roots = [
            record for record in self.records
            if record["component"] == self.manifest["root"]
        ]
        if len(roots) != 1:
            raise PipelineError("Publication root must identify exactly one source build")
        self.root_version = roots[0]["source"]["version"]
        self.source_versions = {
            record["component"]: record["source"]["version"]
            for record in self.records
        }
        self.primaries = self.manifest.get("primaries", [self.manifest["root"]])
        self.generation = self.manifest.get("generation", 1)
        self.source_policy = self.manifest.get("source_policy")
        root_packages = [
            value for value in self.packages.values()
            if value["name"] == root_package(self.manifest["root"])
        ]
        if len(root_packages) != 1:
            raise PipelineError("Publication root must have exactly one main runtime RPM")
        self.root_release = root_packages[0]["release"]
        self.candidate_root = self.state_path.parent / "copr-candidates" / self.digest

    @property
    def devel(self):
        return self.base + self.chroot + "-devel/" if self.storage == "pulp" else self.repo + "devel/"

    def _pulp_package_url(self, value, *, devel=False):
        name = filename(value)
        return (self.devel if devel else self.repo) + f"Packages/{name[0]}/{name}"

    @staticmethod
    def _binary_identities(result):
        if not isinstance(result, dict) or not isinstance(result.get("packages"), list):
            raise PipelineError("Unsupported COPR built-packages response")
        packages = result["packages"]
        if any(not isinstance(value, dict) for value in packages):
            raise PipelineError("Malformed COPR built-package record")
        binaries = [identity(value) for value in packages if value.get("arch") != "src"]
        if len(binaries) != len(set(binaries)):
            raise PipelineError("Duplicate COPR binary package identity")
        return sorted(binaries)

    def _output(self, path):
        path = Path(path)
        root = self.candidate_root
        if not path.is_relative_to(root):
            raise PipelineError("Candidate output is outside the isolated workspace")
        for value in (path, *path.parents):
            if value.is_symlink():
                raise PipelineError("Symlinks are not accepted in candidate output paths")
            if value == root:
                break
        if not path.resolve().is_relative_to(root.resolve()):
            raise PipelineError("Candidate output escapes the isolated workspace")
        return path

    def _bundle_file(self, relative):
        if not isinstance(relative, str) or relative not in self.manifest["files"]:
            raise PipelineError("Publication artifact is absent from checksummed manifest")
        path = (self.bundle / relative).resolve()
        if not path.is_relative_to(self.bundle) or sha256(path) != self.manifest["files"][relative]:
            raise PipelineError("Publication artifact integrity failure")
        return path

    def _mutation_gate(self):
        if self.source_policy != SOURCE_POLICY:
            raise PipelineError("Publication requires the current hypr source policy; resolve/build again")
        if self.policy.get("publish") is not True or self.policy.get("exclusive_writer") is not True:
            raise PipelineError(
                "Mutation requires copr.publish=true and copr.exclusive_writer=true; "
                "manual/webhook/other project writers must be disabled"
            )

    def _client(self):
        # Never move this import/configuration read to module scope or validate.
        try:
            from copr.v3 import Client
        except ImportError as error:
            raise PipelineError("Install Fedora's python3-copr in the trusted publishing worker") from error
        try:
            path = os.environ.get("COPR_CONFIG_FILE")
            client = (
                Client.create_from_config_file(path=path) if path
                else Client.create_from_config_file()
            )
            if client.config.get("copr_url", "").rstrip("/") != API:
                raise PipelineError("Only the Fedora production COPR API is supported")
            # An upload retry is unsafe without an idempotency key. Persist an
            # intent before a single request and reconcile uncertain responses.
            configuration = dict(client.config)
            configuration["connection_attempts"] = 1
            return Client(configuration)
        except PipelineError:
            raise
        except Exception:
            raise PipelineError(
                "Cannot initialize COPR credentials; check the protected client configuration"
            ) from None

    def _safe_url(self, url, prefix=None):
        if not isinstance(url, str):
            raise PipelineError("Missing COPR artifact URL")
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != urllib.parse.urlsplit(DOWNLOAD).netloc
            or parsed.query
            or parsed.fragment
            or "%" in parsed.path
            or "\\" in parsed.path
            or any(ord(character) <= 32 or ord(character) == 127 for character in url)
            or "//" in parsed.path
            or any(part in (".", "..") for part in parsed.path.split("/"))
            or not url.startswith(prefix or self.base)
        ):
            raise PipelineError("Artifact URL is outside the fixed COPR project destination")
        return url

    def _get(self, url, limit=16 * 1024 * 1024):
        parsed = urllib.parse.urlsplit(url)
        redirect = NoRedirect()
        if url.startswith(API + "/api_3/"):
            if parsed.netloc != urllib.parse.urlsplit(API).netloc or parsed.fragment:
                raise PipelineError("Invalid anonymous COPR API URL")
        else:
            self._safe_url(url)
            if self.storage == "pulp" and (
                url.startswith(self.repo) or url.startswith(self.devel)
            ):
                redirect = PulpRedirect(url)
        # Explicitly no netrc, cookies, auth handlers, inherited proxies, or
        # credential-file discovery in this transport.
        return read_content(url, limit, redirect)

    def _api(self, path, **parameters):
        url = API + "/api_3/" + path + "?" + urllib.parse.urlencode(parameters)
        return json.loads(self._get(url))

    def _download(self, url, destination, checksum):
        self._safe_url(url)
        if not isinstance(checksum, str) or not HEX256.fullmatch(checksum):
            raise PipelineError("Candidate lacks a pinned SHA256")
        destination = self._output(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and sha256(destination) == checksum:
            return
        data = self._get(url, limit=1024 * 1024 * 1024)
        if hashlib.sha256(data).hexdigest() != checksum:
            raise PipelineError("COPR artifact changed from its reviewed checksum")
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".download-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        temporary.chmod(0o644)
        temporary.replace(destination)

    def _inventory(self):
        builds = {}
        offset = 0
        while offset < 10000:
            response = self._api(
                "build/list",
                ownername=self.owner,
                projectname=self.project,
                limit=100,
                offset=offset,
                order="id",
                order_type="ASC",
            )
            if not isinstance(response, dict) or not isinstance(response.get("items"), list):
                raise PipelineError("Unsupported COPR inventory response")
            page = response["items"]
            for build in page:
                self._check_build(build)
                # Some API versions omit project_dirname from their Build
                # projection. Do not assume that absence means the main dir:
                # prove it using the per-build, per-chroot result URL instead.
                if build.get("project_dirname") is None:
                    target = self._api(
                        "build-chroot/",
                        build_id=build["id"],
                        chrootname=self.chroot,
                    )
                    source = (build.get("source_package") or {}).get("name")
                    expected = self.repo + f"{build['id']:08d}-{source}/"
                    result = self._safe_url(target.get("result_url"), self.repo)
                    if source not in self.components.values() or result != expected:
                        raise PipelineError("Cannot prove this build belongs to the main project directory")
                    build = {**build, "project_dirname": self.project}
                key = str(build["id"])
                if key in builds:
                    raise PipelineError("COPR pagination changed during inventory")
                builds[key] = {
                    key: build.get(key)
                    for key in (
                        "id", "state", "ownername", "projectname", "project_dirname",
                        "chroots", "submitted_on", "ended_on", "submitter",
                    )
                }
                builds[key]["source_package"] = {
                    field: (build.get("source_package") or {}).get(field)
                    for field in ("name", "version")
                }
                if self.storage == "pulp" and build["state"] in TERMINAL:
                    builds[key]["packages"] = self._binary_identities(self._api(
                        "build-chroot/built-packages",
                        build_id=build["id"],
                        chrootname=self.chroot,
                    ))
            if len(page) < 100:
                return builds
            offset += len(page)
        raise PipelineError("Dedicated project inventory exceeds the supported audit bound")

    def _check_build(self, build, component=None, completed=False):
        if (
            type(build.get("id")) is not int
            or build["id"] < 1
            or build.get("ownername") != self.owner
            or build.get("projectname") != self.project
            or build.get("project_dirname") not in (None, self.project)
            or build.get("chroots") != [self.chroot]
            or build.get("state") not in TERMINAL | ACTIVE
        ):
            raise PipelineError("Unexpected build identity, directory, chroot, or status")
        source = build.get("source_package") or {}
        if source.get("name") not in (None, *self.components.values()):
            raise PipelineError("Dedicated project contains an unrelated source package")
        if completed and (
            build.get("state") != "succeeded"
            or source.get("name") != self.components[component]
        ):
            raise PipelineError("COPR source build did not succeed with the expected package identity")

    def _project_policy(self):
        project = self._api("project/", ownername=self.owner, projectname=self.project)
        chroot = self._api(
            "project-chroot/",
            ownername=self.owner,
            projectname=self.project,
            chrootname=self.chroot,
        )
        effective = self._api(
            "project-chroot/build-config",
            ownername=self.owner,
            projectname=self.project,
            chrootname=self.chroot,
        )
        if (
            project.get("ownername") != self.owner
            or project.get("name") != self.project
            or project.get("devel_mode") is not True
            or project.get("enable_net") is not False
            or project.get("additional_repos") != []
            or set(project.get("chroot_repos", {})) != {self.chroot}
            or project.get("follow_fedora_branching") is not False
            or project.get("packit_forge_projects_allowed") != []
            or project.get("storage") not in ("backend", "pulp")
            or project.get("chroot_repos", {}).get(self.chroot) != self.repo
        ):
            raise PipelineError(
                "Require a dedicated backend/Pulp project, manual createrepo, "
                "one fixed chroot, no network, extra repositories, Packit, or automatic branching"
            )
        self.storage = project["storage"]
        for field in (
            "additional_repos", "additional_packages", "additional_modules",
            "with_opts", "without_opts",
        ):
            if chroot.get(field) != []:
                raise PipelineError(f"Unsupported COPR chroot override: {field}")
            if field != "additional_repos" and effective.get(field) != []:
                raise PipelineError(f"Unsupported effective COPR chroot override: {field}")
        if (
            chroot.get("comps_name") is not None
            or chroot.get("ownername") != self.owner
            or chroot.get("projectname") != self.project
            or chroot.get("mock_chroot") != self.chroot
            or effective.get("enable_net") is not False
            or effective.get("chroot") != self.chroot
            or effective.get("bootstrap_image")
            or project.get("bootstrap") not in (None, "default", "on", "off")
        ):
            raise PipelineError("Unsupported effective COPR build configuration")
        repositories = effective.get("repos")
        # Verified against BuildConfigLogic.generate_build_config and
        # generate_additional_repos: manual mode uses both the last public
        # snapshot and the devel overlay. These are not third-party extras.
        expected = {
            "copr_base": self.repo,
            "copr_base_devel": self.devel,
        }
        if (
            not isinstance(repositories, list)
            or len(repositories) != 2
            or any(not isinstance(value, dict) for value in repositories)
            or {value.get("id"): value.get("baseurl") for value in repositories} != expected
            or effective.get("additional_repos") != [
                f"copr://{self.owner}/{self.project}",
                f"copr://{self.owner}/{self.project}/devel",
            ]
        ):
            raise PipelineError(
                "Effective COPR overlays must be exactly this project's public and devel repositories; "
                "unsupported storage URL/configuration requires review"
            )
        return {
            "project": project,
            "chroot": chroot,
            "effective": effective,
        }

    def _repository(self, base):
        if base not in (self.repo, self.devel):
            raise PipelineError("Unexpected COPR repository base")
        try:
            repomd = self._get(base + "repodata/repomd.xml")
        except urllib.error.HTTPError as error:
            error.close()
            if error.code != 404:
                raise
            raise PipelineError(
                "Initialize this dedicated project's public AND devel repository metadata "
                "before submission; a 404 repository is not evidence of an empty candidate set"
            ) from None
        root = xml(repomd)
        if root.tag != "{" + NS["repo"] + "}repomd":
            raise PipelineError("Unexpected repomd XML root")
        entries = [
            node for node in root.findall("repo:data", NS)
            if node.get("type") == "primary"
        ]
        if len(entries) != 1:
            raise PipelineError("Require exactly one primary repository metadata document")
        entry = entries[0]
        location = entry.find("repo:location", NS)
        checksum = entry.find("repo:checksum", NS)
        if location is None or checksum is None or checksum.get("type") != "sha256":
            raise PipelineError("Primary metadata must have a SHA256 checksum and location")
        url = self._safe_url(urllib.parse.urljoin(base, location.get("href", "")), base)
        data = self._get(url, limit=64 * 1024 * 1024)
        if hashlib.sha256(data).hexdigest() != checksum.text:
            raise PipelineError("Primary repository metadata checksum mismatch")
        if url.endswith(".gz"):
            try:
                # Bound decompression before parsing untrusted metadata.
                import io

                with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
                    data = stream.read(64 * 1024 * 1024 + 1)
            except (OSError, EOFError) as error:
                raise PipelineError("Invalid compressed repository metadata") from error
        elif not url.endswith(".xml"):
            raise PipelineError("Unsupported primary metadata compression; expected XML or gzip")
        if len(data) > 64 * 1024 * 1024:
            raise PipelineError("Decompressed repository metadata exceeds supported limit")
        primary = xml(data)
        if primary.tag != "{" + NS["rpm"] + "}metadata":
            raise PipelineError("Unexpected primary XML root")
        records = primary.findall("rpm:package", NS)
        count = primary.get("packages")
        if count is not None and (not count.isdigit() or int(count) != len(records)):
            raise PipelineError("Primary metadata package count does not match its contents")
        packages = {}
        for node in records:
            version = node.find("rpm:version", NS)
            location = node.find("rpm:location", NS)
            checksum = node.find("rpm:checksum", NS)
            if version is None or location is None or checksum is None:
                raise PipelineError("Incomplete repository package record")
            value = {
                "name": node.findtext("rpm:name", namespaces=NS),
                "epoch": version.get("epoch", "0"),
                "version": version.get("ver"),
                "release": version.get("rel"),
                "arch": node.findtext("rpm:arch", namespaces=NS),
            }
            # Some repositories contain SRPM records alongside binaries.
            if value["arch"] == "src":
                continue
            key = identity(value)
            if key in packages or checksum.get("type") != "sha256" or not HEX256.fullmatch(checksum.text or ""):
                raise PipelineError("Duplicate NEVRA or unsupported package checksum")
            location_base = location.get("{http://www.w3.org/XML/1998/namespace}base")
            if location_base is not None:
                allowed_base = base if self.storage == "pulp" else self.repo
                if location_base.rstrip("/") != allowed_base.rstrip("/"):
                    raise PipelineError("Unexpected explicit COPR package base URL")
                # COPR passes a directory baseurl without a trailing slash to
                # createrepo; urljoin alone would treat the chroot as a file.
                location_base = allowed_base
            url = urllib.parse.urljoin(location_base or base, location.get("href", ""))
            if self.storage == "pulp":
                if url != self._pulp_package_url(value, devel=base == self.devel):
                    raise PipelineError("Pulp RPM location differs from its exact repository/NEVRA path")
                # Both distributions reference the same bytes at different
                # paths. Persist the stable public path for set comparisons;
                # candidate downloads explicitly select the development path.
                url = self._pulp_package_url(value)
            self._safe_url(url, self.repo)
            if PurePosixPath(urllib.parse.urlsplit(url).path).name != filename(value):
                raise PipelineError("Repository filename/NEVRA mismatch")
            packages[key] = {
                **value,
                "url": url,
                "sha256": checksum.text,
            }
        return {
            "repomd_sha256": hashlib.sha256(repomd).hexdigest(),
            "packages": packages,
        }

    def _snapshot(self):
        policy = self._project_policy()
        before = self._inventory()
        result = {
            "policy": policy,
            "inventory": before,
            "public": self._repository(self.repo),
            "devel": self._repository(self.devel),
        }
        try:
            result["key_sha256"] = hashlib.sha256(self._get(self.key_url)).hexdigest()
        except urllib.error.HTTPError as error:
            error.close()
            if (
                error.code != 404
                or self.storage != "pulp"
                or result["public"]["packages"]
                or result["devel"]["packages"]
            ):
                raise PipelineError("COPR signing key is missing; project initialization required") from None
            # COPR exports the key during the first binary build. submit's
            # explicit empty-project gate must authorize this bootstrap state.
            result["key_sha256"] = None
        if before != self._inventory():
            raise PipelineError("COPR build inventory changed during the snapshot; retry safely")
        if policy != self._project_policy():
            raise PipelineError("COPR project policy changed during the snapshot")
        return result

    def _read_state(self):
        state = read_json(self.state_path)
        if (
            state.get("schema_version") != 1
            or state.get("manifest_digest") != self.digest
            or state.get("destination") != [self.owner, self.project, self.chroot]
            or state.get("root") != self.manifest["root"]
            or state.get("root_version") != self.root_version
            or state.get("root_release") != self.root_release
            or state.get("source_versions", self.source_versions) != self.source_versions
            or state.get("primaries", self.primaries) != self.primaries
            or state.get("generation", self.generation) != self.generation
            or state.get("source_policy") != self.source_policy
            or [entry.get("component") for entry in state.get("builds", [])]
            != [record["component"] for record in self.records]
        ):
            raise PipelineError("Recovery state belongs to a different manifest or COPR destination")
        for index, record in enumerate(self.records):
            entry = state["builds"][index]
            if (
                entry.get("srpm") != record["srpm"]
                or entry.get("srpm_sha256") != self.manifest["files"][record["srpm"]]
            ):
                raise PipelineError("Recovery state source RPM identity changed")
        return state

    def _save(self, state):
        save_json(self.state_path, state)

    def _assert_progress(self, state, snapshot, extra=()):
        baseline = state["initial"]
        if snapshot["policy"] != baseline["policy"] or snapshot["key_sha256"] != baseline["key_sha256"]:
            raise PipelineError("COPR configuration or project signing key changed")
        if snapshot["public"] != baseline["public"]:
            raise PipelineError("Public repository changed while candidates were pending")
        known = {str(entry["id"]) for entry in state["builds"] if entry.get("id")}
        inventory = snapshot["inventory"]
        if set(inventory) != set(baseline["inventory"]) | known:
            raise PipelineError("Unrelated, missing, or ambiguous builds appeared in the project")
        for key, value in baseline["inventory"].items():
            if inventory.get(key) != value:
                raise PipelineError("Pre-existing COPR build inventory changed")
        for entry in state["builds"]:
            if entry.get("id") and entry.get("status") == "succeeded":
                key = str(entry["id"])
                if inventory[key] != entry["inventory"]:
                    raise PipelineError("Completed candidate build identity or status changed")
        expected = copy.deepcopy(baseline["devel"]["packages"])
        for entry in state["builds"]:
            for artifact in entry.get("artifacts", []):
                expected[identity(artifact)] = artifact
        for artifact in extra:
            expected[identity(artifact)] = artifact
        if snapshot["devel"]["packages"] != expected:
            raise PipelineError("Development repository contains missing, changed, or unrelated candidates")
        if not extra and snapshot["devel"] != state.get("checkpoint", baseline)["devel"]:
            raise PipelineError("Development repodata changed without a recorded own build")

    def _upgrade_gate(self, snapshot):
        if self.storage == "pulp":
            previous = {
                key for build in snapshot["inventory"].values()
                for key in build.get("packages", [])
            }
            if previous.intersection(identity(value) for value in self.packages.values()):
                raise PipelineError("Pulp candidate NEVRA was already used by a previous build")
        pairs = []
        for candidate in self.packages.values():
            for previous in snapshot["public"]["packages"].values():
                if (candidate["name"], candidate["arch"]) == (previous["name"], previous["arch"]):
                    pairs.append([
                        [str(candidate[key]) for key in ("epoch", "version", "release")],
                        [str(previous[key]) for key in ("epoch", "version", "release")],
                    ])
        if pairs:
            # submit runs in a trusted Fedora worker without a container-engine socket.
            # validate is the only command allowed to launch test containers.
            try:
                import rpm
            except ImportError as error:
                raise PipelineError("The trusted publishing worker requires Fedora python3-rpm") from error
            if any(rpm.labelCompare(tuple(candidate), tuple(previous)) <= 0 for candidate, previous in pairs):
                raise PipelineError("Candidate does not strictly upgrade the previously published COPR EVR")

    def _wait(self, client, entry):
        deadline = time.monotonic() + self.policy["timeout_seconds"]
        while True:
            build = self._authenticated_read(client.build_proxy.get, entry["id"])
            self._check_build(build)
            if build["state"] in TERMINAL:
                self._check_build(build, entry["component"], completed=True)
                return build
            if time.monotonic() >= deadline:
                raise PipelineError("COPR build wait timed out; rerun to resume the recorded build ID")
            time.sleep(self.policy["poll_seconds"])

    @staticmethod
    def _authenticated_read(method, *arguments):
        try:
            return method(*arguments)
        except Exception:
            raise PipelineError("COPR status read failed; recorded recovery state is preserved") from None

    def _completed_artifacts(self, client, entry, record):
        chroot = self._authenticated_read(
            client.build_chroot_proxy.get, entry["id"], self.chroot
        )
        if chroot.get("state") != "succeeded":
            raise PipelineError("COPR target chroot did not succeed")
        root = self._safe_url(chroot.get("result_url"), self.repo)
        expected_root = self.repo + f"{entry['id']:08d}-{self.components[entry['component']]}/"
        if root != expected_root:
            raise PipelineError("Unsupported COPR result layout; cannot pin build-specific artifacts")
        result = self._authenticated_read(
            client.build_chroot_proxy.get_built_packages, entry["id"], self.chroot
        )
        expected = {identity(self.packages[path]) for path in record["packages"]}
        if set(self._binary_identities(result)) != expected:
            raise PipelineError("COPR-produced package NEVRAs differ from the validated local manifest")
        deadline = time.monotonic() + self.policy["timeout_seconds"]
        while True:
            snapshot = self._snapshot()
            available = snapshot["devel"]["packages"]
            if expected.issubset(available):
                artifacts = [available[key] for key in sorted(expected)]
                if any(
                    value["url"] != (
                        self._pulp_package_url(value) if self.storage == "pulp"
                        else root + filename(value)
                    )
                    for value in artifacts
                ):
                    raise PipelineError("Candidate metadata does not reference the recorded build")
                self._check_build(
                    snapshot["inventory"][str(entry["id"])],
                    entry["component"],
                    completed=True,
                )
                state = self._read_state()
                if self.storage == "pulp":
                    if snapshot["inventory"][str(entry["id"])].get("packages") != sorted(expected):
                        raise PipelineError("Pulp build inventory differs from recorded candidate NEVRAs")
                    for key, build in snapshot["inventory"].items():
                        if key != str(entry["id"]) and expected.intersection(build.get("packages", [])):
                            raise PipelineError("Pulp candidate identity is ambiguous across build history")
                    self._pin_bootstrap_key(state, snapshot, entry, artifacts)
                self._assert_progress(state, snapshot, artifacts)
                return artifacts, snapshot
            if time.monotonic() >= deadline:
                raise PipelineError("Signed devel metadata did not converge; no further builds submitted")
            time.sleep(self.policy["poll_seconds"])

    def _pin_bootstrap_key(self, state, snapshot, entry, artifacts):
        if state["initial"]["key_sha256"] is not None:
            return
        initial = state["initial"]
        if (
            state.get("key_bootstrap") is not True
            or self.storage != "pulp"
            or initial["inventory"]
            or initial["public"]["packages"]
            or initial["devel"]["packages"]
            or state["builds"][0].get("id") != entry["id"]
            or any(build.get("id") for build in state["builds"][1:])
            or set(snapshot["inventory"]) != {str(entry["id"])}
            or snapshot["policy"] != initial["policy"]
            or snapshot["public"] != initial["public"]
            or snapshot["devel"]["packages"] != {identity(value): value for value in artifacts}
            or not HEX256.fullmatch(snapshot.get("key_sha256") or "")
        ):
            raise PipelineError("Cannot bootstrap signing key outside the first recorded empty-project build")
        # Persist this one-time trust transition before another build or any
        # credential-free candidate execution. Subsequent key changes fail.
        initial["key_sha256"] = snapshot["key_sha256"]
        state["key_bootstrap"] = False
        self._save(state)

    def submit(self):
        self._mutation_gate()
        initial = None
        if self.state_path.exists():
            previous = read_json(self.state_path)
            if previous.get("manifest_digest") == self.digest:
                state = self._read_state()
                if state.get("status") == "promoted":
                    if not self._promotion_observation(state, self._snapshot()):
                        raise PipelineError("Previously promoted candidates are no longer public")
                    return state
            else:
                if (
                    previous.get("schema_version") != 1
                    or previous.get("status") != "promoted"
                    or previous.get("destination") != [self.owner, self.project, self.chroot]
                    or previous.get("root") != self.manifest["root"]
                    or not HEX256.fullmatch(previous.get("manifest_digest", ""))
                ):
                    raise PipelineError("Another manifest is pending; recover it before starting a new release")
                initial = self._snapshot()
                if initial != previous.get("promotion"):
                    raise PipelineError("Previously promoted project changed before the next release")
                archive = self.state_path.with_name(
                    self.state_path.name + ".promoted-" + previous["manifest_digest"]
                )
                if archive.exists() and read_json(archive) != previous:
                    raise PipelineError("Existing completed publication archive differs from recovery state")
                save_json(archive, previous)
                state = None
        else:
            state = None
        if state is None:
            initial = initial or self._snapshot()
            if any(value["state"] not in TERMINAL for value in initial["inventory"].values()):
                raise PipelineError("Project has active unrelated builds")
            if initial["public"]["packages"] != initial["devel"]["packages"]:
                raise PipelineError("Project has pre-existing unpublished candidates")
            if initial["key_sha256"] is None and (
                self.storage != "pulp" or initial["inventory"] or initial["public"]["packages"]
            ):
                raise PipelineError("Signing-key bootstrap requires a completely empty Pulp project")
            self._upgrade_gate(initial)
            state = {
                "schema_version": 1,
                "manifest_digest": self.digest,
                "root": self.manifest["root"],
                "root_version": self.root_version,
                "root_release": self.root_release,
                "source_versions": self.source_versions,
                "primaries": self.primaries,
                "generation": self.generation,
                "source_policy": self.source_policy,
                "destination": [self.owner, self.project, self.chroot],
                "status": "building",
                "initial": initial,
                "key_bootstrap": initial["key_sha256"] is None,
                "builds": [
                    {
                        "component": record["component"],
                        "srpm": record["srpm"],
                        "srpm_sha256": self.manifest["files"][record["srpm"]],
                        "status": "planned",
                    }
                    for record in self.records
                ],
            }
            self._save(state)
        if state["status"] in ("submitted", "validated"):
            self._assert_progress(state, self._snapshot())
            return state
        if state["status"] != "building":
            raise PipelineError("Publication is already promoting/promoted or requires manual recovery")
        client = None
        for index, record in enumerate(self.records):
            entry = state["builds"][index]
            if entry["status"] == "succeeded":
                continue
            if entry["status"] == "submitting" and not entry.get("id"):
                raise PipelineError(
                    "Upload outcome is ambiguous; refusing a duplicate submission. "
                    "Reconcile the exact build ID/source provenance in recovery state first"
                )
            if not entry.get("id"):
                self._assert_progress(state, self._snapshot())
                path = self._bundle_file(entry["srpm"])
                client = client or self._client()
                entry["status"] = "submitting"
                self._save(state)
                try:
                    build = client.build_proxy.create_from_file(
                        self.owner,
                        self.project,
                        str(path),
                        buildopts={
                            "chroots": [self.chroot],
                            "enable_net": False,
                        },
                    )
                except Exception:
                    # Never include a credential-bearing client exception in logs.
                    raise PipelineError("COPR upload outcome is uncertain; recovery state preserved") from None
                if type(build.get("id")) is not int or build["id"] < 1:
                    raise PipelineError("COPR upload returned no usable build ID; do not resubmit")
                entry["id"] = build["id"]
                entry["status"] = "waiting"
                self._save(state)
            client = client or self._client()
            self._wait(client, entry)
            artifacts, snapshot = self._completed_artifacts(client, entry, record)
            # Completion may have persisted the first-key bootstrap transition.
            state = self._read_state()
            entry = state["builds"][index]
            entry["artifacts"] = artifacts
            entry["inventory"] = snapshot["inventory"][str(entry["id"])]
            entry["status"] = "succeeded"
            state["checkpoint"] = snapshot
            self._save(state)
        state["submitted"] = self._snapshot()
        self._assert_progress(state, state["submitted"])
        state["status"] = "submitted"
        self._save(state)
        return state

    def _artifacts(self, state):
        artifacts = [
            value for entry in state["builds"]
            for value in entry.get("artifacts", [])
        ]
        expected = {identity(value) for value in self.packages.values()}
        if (
            any(entry.get("status") != "succeeded" for entry in state["builds"])
            or len(artifacts) != len(expected)
            or {identity(value) for value in artifacts} != expected
        ):
            raise PipelineError("Incomplete or mismatched signed candidate set")
        for value in artifacts:
            self._safe_url(value["url"], self.repo)
            if not HEX256.fullmatch(value.get("sha256", "")):
                raise PipelineError("Candidate SHA256 is not pinned")
        return artifacts

    def _check_signatures(self, destination, artifacts, image, directory="rpms"):
        with Container(image, [(destination, "/candidates", True)]) as container:
            # An isolated empty RPM trust database contains only this project's
            # pinned key, not the builder image's Fedora/other imported keys.
            container.exec(["rpm", "--dbpath", "/tmp/copr-trust", "--initdb"])
            container.exec([
                "rpm", "--dbpath", "/tmp/copr-trust",
                "--import", "/candidates/pubkey.gpg",
            ])
            for value in artifacts:
                path = "/candidates/" + directory + "/" + filename(value)
                output = container.exec([
                    "rpmkeys", "--dbpath", "/tmp/copr-trust",
                    "--define", "_pkgverify_level signature",
                    "--checksig", "--verbose", path,
                ])
                if (
                    re.search(r"NOT OK|NOKEY|NOTTRUSTED|UNSIGNED|BAD", output, re.IGNORECASE)
                    or not re.search(
                        r"(?:RSA|DSA|ECDSA|EdDSA|OpenPGP)[^\n]*signature[^\n]*:\s*OK",
                        output,
                        re.IGNORECASE,
                    )
                ):
                    raise PipelineError("Candidate lacks a verified signature from the project key")
                metadata = container.exec([
                    "rpm", "-qp", "--qf",
                    "%{NAME}\\t%{EPOCHNUM}\\t%{VERSION}\\t%{RELEASE}\\t%{ARCH}",
                    path,
                ]).split("\t")
                if len(metadata) != len(RPM_FIELDS) or identity(dict(zip(RPM_FIELDS, metadata))) != identity(value):
                    raise PipelineError("Signed RPM header NEVRA differs from the local validated manifest")

    def _baseline(self, state, image):
        names = {
            value["name"] for value in self.packages.values()
            if not value["name"].endswith(("-debuginfo", "-debugsource"))
        }
        previous = [
            value for value in state["submitted"]["public"]["packages"].values()
            if value["name"] in names
        ]
        if not previous:
            return []
        # Select the newest published package per name/architecture using
        # Fedora RPM ordering, not lexical or upstream-version comparisons.
        program = """import json, rpm, sys
values = json.loads(sys.argv[1])
selected = {}
for value in values:
    key = (value["name"], value["arch"])
    old = selected.get(key)
    evr = lambda item: tuple(str(item[field]) for field in ("epoch", "version", "release"))
    if old is None or rpm.labelCompare(evr(value), evr(old)) > 0:
        selected[key] = value
print(json.dumps(list(selected.values())))
"""
        with Container(image) as container:
            return json.loads(container.exec(["python3", "-c", program, json.dumps(previous)]))

    def validate(self):
        # Intentionally no mutation gate, copr import, credential config read,
        # authenticated client, upload, or repository regeneration.
        state = self._read_state()
        if state.get("status") not in ("submitted", "validated"):
            raise PipelineError("Submit all exact SRPMs before validating signed COPR candidates")
        artifacts = self._artifacts(state)
        snapshot = self._snapshot()
        self._assert_progress(state, snapshot)
        if snapshot != state["submitted"]:
            raise PipelineError("COPR snapshot changed since candidate capture")
        state.pop("validated", None)
        state["status"] = "submitted"
        self._save(state)
        destination = self.candidate_root
        self._output(destination / "rpms").mkdir(parents=True, exist_ok=True)
        self._output(destination / "logs").mkdir(exist_ok=True)
        self._download(self.key_url, destination / "pubkey.gpg", snapshot["key_sha256"])
        for value in artifacts:
            self._download(
                self._pulp_package_url(value, devel=True) if self.storage == "pulp" else value["url"],
                destination / "rpms" / filename(value),
                value["sha256"],
            )
        image = build_image(self.config, self._output(destination / "logs/builder-image.log"))
        self._check_signatures(destination, artifacts, image)
        baseline = self._baseline(state, image)
        for value in baseline:
            self._download(
                value["url"],
                destination / "baseline-rpms" / filename(value),
                value["sha256"],
            )
        if baseline:
            self._check_signatures(destination, baseline, image, directory="baseline-rpms")
        manifest = copy.deepcopy(self.manifest)
        manifest["status"] = "copr-candidate"
        for relative in self.manifest["files"]:
            if not relative.startswith("rpms/"):
                path = self._bundle_file(relative)
                target = self._output(destination / relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
        manifest["files"].update({
            "rpms/" + filename(value): value["sha256"] for value in artifacts
        })
        baseline_rpms = ["baseline-rpms/" + filename(value) for value in baseline]
        manifest["copr"] = {
            "manifest_digest": self.digest,
            "snapshot_digest": digest_json(snapshot),
            "build_ids": [entry["id"] for entry in state["builds"]],
            "published_baseline": baseline,
        }
        save_json(self._output(destination / "manifest.json"), manifest)
        Builder(self.config, destination / "validation-state").validate(
            destination,
            manifest,
            image,
            baseline_rpms=baseline_rpms,
        )
        for folder, values in (("rpms", artifacts), ("baseline-rpms", baseline)):
            for value in values:
                if sha256(destination / folder / filename(value)) != value["sha256"]:
                    raise PipelineError("Candidate/baseline bytes changed during validation")
        if self._snapshot() != snapshot:
            raise PipelineError("COPR state changed during candidate validation")
        manifest["status"] = "validated"
        save_json(destination / "manifest.json", manifest)
        state["validated"] = {
            "snapshot_digest": digest_json(snapshot),
            "artifacts_digest": digest_json(artifacts),
            "candidate_manifest_sha256": sha256(destination / "manifest.json"),
        }
        state["status"] = "validated"
        self._save(state)
        return state

    def _validation_gate(self, state):
        artifacts = self._artifacts(state)
        validation = state.get("validated", {})
        if (
            validation.get("snapshot_digest") != digest_json(state["submitted"])
            or validation.get("artifacts_digest") != digest_json(artifacts)
            or not (self.candidate_root / "manifest.json").is_file()
            or validation.get("candidate_manifest_sha256") != sha256(self.candidate_root / "manifest.json")
        ):
            raise PipelineError("Missing or changed credential-free candidate validation receipt")
        for value in artifacts:
            path = self.candidate_root / "rpms" / filename(value)
            if not path.is_file() or sha256(path) != value["sha256"]:
                raise PipelineError("Validated signed candidate bytes changed")

    def _promotion_observation(self, state, snapshot):
        expected = state["submitted"]
        for field in ("policy", "inventory", "key_sha256"):
            if snapshot[field] != expected[field]:
                raise PipelineError("COPR state changed during promotion; no regeneration retry is safe")
        # COPR's Pulp regeneration also republishes development metadata.
        # Only that metadata refresh is allowed; the entire candidate set,
        # checksums, policy, inventory and signing key must remain unchanged.
        if (
            snapshot["devel"]["packages"] != expected["devel"]["packages"]
            or (self.storage != "pulp" and snapshot["devel"] != expected["devel"])
        ):
            raise PipelineError("COPR development candidates changed during promotion")
        if snapshot["public"]["packages"] == expected["devel"]["packages"]:
            return True
        if snapshot["public"] != expected["public"]:
            raise PipelineError("Unexpected public repository state during promotion")
        return False

    def promote(self, yes=False):
        self._mutation_gate()
        if yes is not True:
            raise PipelineError("Promotion requires explicit --yes")
        state = self._read_state()
        if state.get("status") not in ("validated", "promoting", "promoted"):
            raise PipelineError("Credential-free signed candidate validation is required before promotion")
        self._validation_gate(state)
        snapshot = self._snapshot()
        if state["status"] in ("promoting", "promoted"):
            complete = self._promotion_observation(state, snapshot)
            if state["status"] == "promoted":
                if not complete:
                    raise PipelineError("Previously promoted repository is no longer the reviewed set")
                return state
        else:
            self._assert_progress(state, snapshot)
            if snapshot != state["submitted"]:
                raise PipelineError("Last-moment COPR snapshot differs from the validated candidate set")
            client = self._client()
            # Last read is immediately before mutation. This is safe only under
            # the documented sole-writer prerequisite, not as a distributed CAS.
            if self._snapshot() != snapshot:
                raise PipelineError("COPR state changed immediately before promotion")
            state["status"] = "promoting"
            self._save(state)
            try:
                client.project_proxy.regenerate_repos(self.owner, self.project)
            except Exception:
                raise PipelineError(
                    "Regeneration outcome is uncertain; rerun promotion to observe, never blindly retry"
                ) from None
        deadline = time.monotonic() + self.policy["timeout_seconds"]
        while True:
            snapshot = self._snapshot()
            if self._promotion_observation(state, snapshot):
                state["status"] = "promoted"
                state["promotion"] = snapshot
                self._save(state)
                return state
            if time.monotonic() >= deadline:
                raise PipelineError(
                    "Public metadata did not converge; promotion intent retained, regeneration not retried"
                )
            time.sleep(self.policy["poll_seconds"])
