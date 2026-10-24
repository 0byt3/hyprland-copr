# AGENTS.md

This file provides guidance to WARP (warp.dev) when working with code in this repository.

## Scope and execution model

This repository is a Python controller for discovering stable Hyprland-stack releases, resolving dependencies, building Fedora 44 RPMs, validating upgrades, and publishing coordinated releases to COPR. It is not the compositor source tree.

Run commands from the repository root with Python 3.11+. The host needs Git and native rootless Podman; install prerequisites from distribution repositories. Compilation tools and Fedora-specific Python bindings are supplied by the container images, not a host pip environment. GitHub hosts source code and upstream release APIs only: `ci/` is local automation, not GitHub Actions.

## Commands

### Checks and focused tests

Unit tests use only the Python standard library and mock external services/container operations:

```bash
python3 -m unittest discover -s tests -v
python3 -m unittest discover -s tests -p 'test_core.py' -v
PYTHONPATH=tests python3 -m unittest test_core.ModelTests.test_combined_bounds -v
python3 -m compileall -q hyprland_copr container deploy tests ci
```

The single-test example uses `PYTHONPATH=tests` because `tests/` is not a Python package. Substitute another module/class/method as needed.

Run the canonical containerized QA gate without building RPMs or publishing:

```bash
python3 -m ci.release --checks-only
```

For direct checks without the release runner's per-run artifacts:

```bash
podman --remote=false build --layers -f container/Checksfile -t hyprland-copr-checks .
podman --remote=false run --rm --pull=never --network=none \
    --userns=keep-id --user "$(id -u):$(id -g)" \
    --cap-drop=ALL --security-opt=no-new-privileges \
    --env HOME=/tmp \
    -v "$PWD:/workspace:ro,z" hyprland-copr-checks
```

The image runs `python3 ci/check.py`: Ruff, all unit tests, Python compilation, and `bash -n`/ShellCheck on project shell scripts. If the tools are already installed from repositories, `python3 ci/check.py` also works on the host. The standalone Python lint command is:

```bash
ruff check hyprland_copr container deploy tests ci
```

Ruff settings live in `pyproject.toml`; there is no separate configured formatter or type-checker gate. RPM builds additionally enforce rpmlint on rendered specs, SRPMs, and all binary/debug RPMs. `verify` does not rerun lint or runtime checks.

### Discovery, builds, and bundle verification

```bash
python3 -m hyprland_copr discover --all
python3 -m hyprland_copr resolve
python3 -m hyprland_copr resolve --component hyprpaper
python3 -m hyprland_copr run --component hypridle --output dist/hypridle
python3 -m hyprland_copr run --lua-only --output dist/lua55
python3 -m hyprland_copr run --output dist/stack
python3 -m hyprland_copr build .state/resolution.json --output dist/retry
python3 -m hyprland_copr verify dist/retry
```

Discovery, resolution, image preparation, and dependency transactions need network access; compilation and runtime checks are offline. Core `run` builds and validates but never publishes. Without `--component`, the primary is Hyprland and its four companions are independently selected at their own stable releases; `--version` then pins only Hyprland. `--component NAME` builds that component and its dependency closure. `--component lua` and `--lua-only` are equivalent.

Use a new/empty bundle output directory; partial artifacts are not overwritten. Saved resolutions retain absolute cache/source paths. Resolution exits with 3 for dependency blockers, 1 for operational/unsupported-source failures, and 0 on success.

For QA followed by a full local build without publication:

```bash
python3 -m ci.release --no-publish
```

**`python3 -m ci.release` without flags can mutate the live `byte0/hyprland` COPR project.** Publication is enabled in the checked-in config. `--checks-only`, `--component`, `--lua-only`, and `--no-publish` prevent automatic publication and pending-publication recovery. See README.md for deliberate raw publication/recovery commands and live-project prerequisites.

## Architecture and data flow
Unqualified Python module filenames below refer to `hyprland_copr/`.

- **Source and requirement model:** `model.py` defines version constraints, component ownership/aliases, source policy, topological ordering, and coordinated RPM release generations. `sources.py` discovers stable numeric releases, verifies tags/source versions, initializes recursive submodules at recorded revisions, and produces deterministic checksummed archives. Protocol submodule snapshots are keyed by commit.
- **Resolution:** `dependencies.py` parses bounded CMake forms, evaluates known Release/Linux conditions, conservatively retains unknown branches, and adds reviewed component-specific requirements. `resolver.py` combines constraints across all selected applications/dependencies and uses `FedoraProviders` in `fedora.py` for repository capability queries. The result is a JSON graph with frozen sources, requirements, provider decisions, blockers, and build order.
- **Packaging and building:** `packaging.py` renders `packaging/*.spec.in`; `components.py` holds companion file/license/runtime policy and protocol-layout detection. `build.py` stages checked archives, builds in dependency order using run-local prerequisite RPMs, and checks expanded CMake configure traces against discovered requirements. `container/build.py` runs rpmlint and rpmbuild inside the offline non-root worker and emits RPM identity metadata.
- **Validation and bundle contract:** `Builder.validate` prepares a Fedora/prior-publication baseline and installs candidates through ordinary dependency-checked upgrades. `container/validate.py`, `container/smoke.py`, and the Lua/Glaze fixtures exercise RPM EVR ordering, dependency closure, ELF linkage, Lua coexistence, and headless consumers/companion checks. A bundle contains `manifest.json`, sanitized `resolution.json`, `baseline.json`, RPMs, SRPMs, and logs. `verify_bundle` checks hashes, provenance, source policy, identities, and graph consistency; it is not a fresh execution of those tests.
- **Controller and isolation:** `cli.py` exposes the core commands and state lock. `fedora.py` owns container lifecycle, repository transactions, and temporary offline snapshots. `runtime.py` centralizes native `podman --remote=false`, UID preservation, and the allowlisted credential-free validation environment.
- **Publication:** `publish.py` implements resumable submission → signed-candidate validation → promotion. It uploads exact SRPMs for external COPR rebuilds, not locally built binary RPMs. It checks project policy, pinned signing keys, build provenance, NEVRA sets, repository snapshots, and validation receipts. Both backend and Pulp storage are supported; `transport.py` confines anonymous Pulp redirects to pinned production content paths and bounds downloads without exposing signed storage URLs.
- **Scheduled orchestration:** `ci/release.py` runs QA, builds/verifies a bundle, and optionally publishes; `ci/copr.py` persists the handoff and separates credentialed submission/promotion containers from credential-free candidate execution. `deploy/install.py` renders user-systemd templates for the actual checkout/interpreter. The default timer runs every six hours; normal installation can trigger catch-up publication, whereas `--no-enable` only stages units.

## Policies that span modules

`config/project.toml` is strictly validated by `config.py`: Fedora 44, x86_64, only `fedora`/`updates`, and co-installable Lua 5.5 are intentional restrictions, not generic configuration knobs.

Every reachable `hypr*` requirement must be source-built, even if Fedora offers a compatible provider. This includes active optional probes and pkg-config/CMake/RPM/program aliases. Unknown safe names are confined to the official `hyprwm` organization; unsupported layouts, dependency alternatives, or licenses require adapter review. Non-hypr requirements are Fedora-first, with managed fallbacks for Aquamarine, Lua, Glaze, and the portal. Changing discovery often also requires reviewing ownership mappings, specs, configure-trace checks, and installed-package validation.

Lua packages are `lua55`, `lua55-libs`, and `lua55-devel`, exposing `lua5.5`, `luac5.5`, `pkgconfig(lua55)`, and `liblua5.5.so.0` without replacing Fedora Lua. Glaze is a header-only/noarch `glaze-devel` package, not a runtime shared library. Normal upgrades must not use `--allowerasing`. Headless bundle validation does not establish GPU/session, plugin, or PAM lock-security correctness; separate VM evidence described in README.md does not change the manifest's `hardware_session_tested = false` boundary.

Keep repository transactions separate from unprivileged, networkless compilation/runtime workers. Rootless workers preserve the invoking UID with `--userns=keep-id`; mounts use `z`/`ro,z`, not recursive ownership changes. The host user service deliberately omits `NoNewPrivileges=yes` so subordinate-ID helpers can work; containers retain their own no-new-privileges protection.

## State, artifacts, and recovery

- `.state/` retains source caches/checkouts, RPM workspaces, saved resolutions, and the core lock. The release runner has a separate `.state/automation` lock.
- `dist/runs/<unique-id>/` retains per-run logs, `result.json`, a resolution when available, and the attempted bundle. `--output` for `ci.release` selects a new **run directory**, whereas the core CLI's `--output` selects a **bundle directory**.
- Preserve `.state/copr.json`, `.state/automation/publication.json`, `.state/copr-candidates/`, and the referenced validated bundle together. They bind generation, source-policy/version sets, destination, remote build IDs, and candidate receipts. Increment `pipeline.release` for packaging-only rebuilds; do not reset publication state to force another build.
- Automatic full-stack releases resume a durable pending handoff before resolving another stack. Once promotion intent exists, recovery observes convergence rather than repeating submission/validation or blindly regenerating metadata.

Publication assumes this pipeline is the COPR project's sole writer; snapshot checks are not an atomic multi-writer transaction. Credentialed workers never execute candidate code. The controller accepts only a protected, user-owned regular COPR config file outside source/state/artifacts. Live policy drift, uncertain uploads, changed signing keys, or lost state require explicit reconciliation, not bypassing gates.
