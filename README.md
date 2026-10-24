# Fedora 44 Hyprland COPR pipeline
Discover stable upstream releases, resolve dependencies against Fedora 44, build separate RPMs in disposable containers, and publish a validated coordinated stack through a dedicated COPR project.

All automation and tests are orchestrated locally on your workstation or on-premises server. GitHub is only a source-code hosting/deployment destination for this project: there are no hosted workflows, runners, secrets, or artifact uploads. Upstream release discovery still queries the upstream projects' GitHub APIs. After successful local QA, compilation and validation, complete-stack automation submits checked SRPMs to `byte0/hyprland`, validates the signed COPR rebuilds locally, and promotes them. COPR necessarily performs its own external rebuilds; local binary RPMs are not uploaded.

Every reachable dependency whose name begins with `hypr` is included as a source build, even when Fedora provides a compatible package. This applies recursively to pkg-config modules, CMake packages, direct RPM requirements and required programs; `-devel`/`-libs` names and aliases such as `hyprwire-scanner` map to their owning source project. Safe new names are discovered only in the official `hyprwm` GitHub organization, retaining explicit repository mappings such as `hyprwm/Hyprland`. Non-hypr dependencies remain Fedora-first, with managed source fallbacks for Aquamarine, Lua 5.5, Glaze, and xdg-desktop-portal-hyprland.
Active optional hypr probes are included too; known disabled build branches remain excluded. Unsupported module/program alternatives require adapter review. Generated library RPMs provide versioned `-libs` aliases, and hyprwire provides its scanner package aliases, so direct RPM requirements use the source-built packages rather than pulling Fedora fallbacks back into the build.

The default coordinated stack selects each application's own stable release and resolves their shared dependencies together. Every Git submodule is initialized recursively at its recorded revision; profiling stays disabled. Unknown repositories, incompatible stable releases, unsupported packaging layouts and unreviewed licenses stop the pipeline explicitly rather than silently using Fedora or skipping a hypr dependency. New libraries can use the bounded shared-library/header/pkg-config adapter with the reviewed BSD-3-Clause license form; other layouts or bundled notices require adapter review.

The complete 16-component coordinated stack, including Hyprland 0.56.2 and all four companion applications, has passed a real Fedora 44 build and headless install/upgrade validation. A separate Fedora 44 VM session has also passed the graphical checks described below. Hyprland 0.56.2's `>=7,<8` Glaze requirement selects compatible Glaze 7.x, not the newer incompatible 8.x/9.x majors.

## Local use
Requirements: Python 3.11 or newer, Git, Docker with a working daemon, and network access for source acquisition and Fedora repository transactions. Install these through your distribution's repositories. Compilation tooling is installed from Fedora repositories inside containers, not on the host.

Run commands from this project directory:

```bash
python3 -m hyprland_copr discover --all
python3 -m hyprland_copr resolve
python3 -m hyprland_copr run --output dist/stack
```

Without `--component`, `--version 0.56.2` selects Hyprland's version; the four companions are selected independently. The candidate search is bounded by `pipeline.max_candidates`, including older compatible Glaze majors. Increase that configured window if a required compatible release falls outside it. `resolve` exits with code 3 for a missing dependency, 1 for an operational/unsupported-source error, and 0 for a resolved graph. `run` never builds a blocked graph.

Build or resolve one component and its required dependencies:

```bash
python3 -m hyprland_copr run --component hypridle --output dist/hypridle
python3 -m hyprland_copr run --component hyprlock --output dist/hyprlock
python3 -m hyprland_copr run --component xdg-desktop-portal-hyprland --output dist/portal
python3 -m hyprland_copr resolve --component hyprpaper
python3 -m hyprland_copr run --component hyprtoolkit --output dist/hyprtoolkit
python3 -m hyprland_copr run --component glaze --version 7.9.1 --output dist/glaze7
python3 -m hyprland_copr run --component hyprland --version 0.56.2 --output dist/compositor
```

Standalone Glaze or hyprtoolkit without `--version` tracks its latest stable release. Coordinated dependency resolution still enforces each consumer's major/version bounds. `--component lua` and the existing `--lua-only` select the same Lua55 validation path. Individual-component runs never publish automatically, including `--component hyprland`. The lower-level `hyprland_copr run` command itself remains build/validation-only; automatic publication belongs to `ci.release`.

Exercise the independently useful Lua packaging and validation path:

```bash
python3 -m hyprland_copr run --lua-only --output dist/lua55
python3 -m hyprland_copr verify dist/lua55
```

Rebuild a saved resolution with `python3 -m hyprland_copr build .state/resolution.json --output dist/retry`. Resolutions saved under the earlier Fedora-first source policy must be resolved again before building. The most recent resolutions are also retained as `.state/resolution-stack.json` or `.state/resolution-COMPONENT.json`. Source archives are checked against that resolution before staging; protocol snapshots are isolated by commit to prevent different applications' pins overwriting one another. The resolver prefers compatible pinned protocol sources, then compatible official protocol releases when required. Choose a new/empty output directory; partial artifacts are not overwritten. The shared `.state` lock prevents concurrent local runs. Source checkouts, RPM work directories, and logs remain available for diagnosis.

`config/project.toml` controls the release number, state paths, and COPR destination. Target Fedora release, architecture, repositories, and Lua ABI are intentionally restricted. An incompatible upstream declaration requires adapter/spec review, not loosening repository checks.
Coordinated RPM releases include the compositor version and a persisted generation, for example `1.hypr0.56.2.stack1.fc44`. When a companion or managed dependency changes, the next generation also upgrades rebuilt libraries whose own version is unchanged. Preserve `.state/copr.json` across runs: it binds the generation, complete source-version set, source policy, primary selection, packaging release, and destination. Old publication records cannot cause the new source policy to be skipped. Saved resolutions retain their generation for exact rebuilds. Increment `pipeline.release` for a packaging-only rebuild of the same inputs. Strict RPM EVR checks reject stale generations; lost publication state requires reconciliation rather than guessing a release number.

## Build and validation boundary
* Source-build all reachable `hypr*` dependencies and enforce their combined version bounds. Prefer compatible providers from Fedora's `fedora` and `updates` repositories for non-hypr dependencies; build missing managed fallbacks separately. Portal requirements include its actual external protocol XML files as well as a minimum pkg-config version, so an incomplete older provider cannot silently pass resolution.
* Discover requirements from supported CMake declarations, respecting known Release/Linux options. Retain unknown conditional branches conservatively; reject unsupported dynamic required declarations. Validate active source declarations against expanded configure traces. Protocol definitions support their older Meson and current CMake layouts; missing/ambiguous layouts fail closed. Lua uses a bounded Make adapter.
* Freeze source commits, recursive submodule revisions, archive checksums, generated BuildRequires, and dependency ordering.
* Install build dependencies before compilation. Compile with no networking, no Linux capabilities, and the host's non-root UID in a disposable Fedora 44 image. Bind mounts use Docker's shared SELinux label (`z`, or `ro,z` for read-only mounts), so enforcing Fedora hosts do not require disabling SELinux.
* Run rpmlint on each rendered spec before compilation, then on that spec, its SRPM, and every binary RPM (including debug packages) before accepting the build. Recheck SRPMs and binary RPMs during offline bundle validation, including signed COPR candidates. Errors stop the pipeline; warnings remain visible in the component build and validation logs. No permissive mode or blanket diagnostic filters are used. Generated specs include a deterministic changelog dated from the source timestamp; undated Lua sources use the fixed initial packaging date, 2026-10-24.
* Validate ordinary Fedora/prior-COPR package upgrades without `--allowerasing`, RPM EVR ordering, dependency closure, ELF library resolution, Lua embedding, bytecode, module loading, and system-Lua coexistence.
* Runtime validation has read-only artifact mounts and no network or publication credentials. It cannot establish GPU/session correctness or compatibility with arbitrary third-party plugins.
* Glaze is a noarch `glaze-devel` RPM from a `glaze` SRPM, with headers and `cmake(glaze)` metadata, not a fictional shared-library/runtime package. Its C++23 JSON consumer is compiled and run against both staged and installed CMake metadata.
* hyprtoolkit is split into a runtime shared-library RPM and a development RPM. Its `BSD-3-Clause AND HPND-sell-variant` license expression includes the embedded layer-shell protocol notice. Graphical toolkit test applications stay disabled.
* The four companion daemons get isolated, time-bounded `--help`/`--version` and installed-file checks. User units and portal/D-Bus registration are packaged but never enabled or started during validation. The graphical share picker is checked as an installed executable, not launched.
* hyprlock installs a Fedora-oriented `%config(noreplace)` PAM service at `/etc/pam.d/hyprlock`, including `system-auth` for auth/account checks. The package never edits global `system-auth` or authselect profiles. Container checks verify packaging, not real PAM authentication or lock safety; those need separate graphical/security validation.

`hyprland` retains Fedora's package name. A candidate must have a strictly newer RPM Epoch-Version-Release before it is accepted as an upgrade. Hyprland plugin headers are split into `hyprland-devel`; plugins need rebuilding for the exact compositor version.

### Fedora 44 VM session verification
The 2026-10-24 verification used the authenticated Fedora Cloud Base 44-1.7 image, session libvirt/KVM, 4 vCPUs, 6 GiB RAM, and enforcing SELinux. Builds remained offline and unprivileged after repository dependency installation. The resulting RPMs were installed only inside the guest.

The verified bundle and evidence are retained in `dist/stack-vm-fedora44`:
* All 16 source components built, followed by ordinary Fedora-baseline install/upgrade, dependency closure, ELF, Lua coexistence, companion asset/CLI, and Glaze consumer checks.
* The running compositor's source commit and installed RPM identities matched the verified bundle.
* An active local tty1/logind session used the DRM backend and VirtIO/virgl at 1280×720, not a nested or headless compositor.
* A native Wayland terminal rendered successfully; clipboard transfer, window/workspace operations, virtual keyboard bindings, and Lua configuration reload passed.
* hyprpaper rendered a supplied wallpaper with a verified pixel; hypridle's Wayland idle timer fired.
* The portal backend registered its D-Bus interfaces and connected to PipeWire. Interactive screencast selection/capture was not tested.
* Hyprland exited with status 0. The temporary autologin was removed, and the guest was shut down after collection.

The minimal Lua configuration used software cursors with animations and blur disabled, and launched directly from the VT rather than validating a display manager or `start-hyprland` lifecycle. This pass does not establish native physical-GPU behavior, multi-monitor hardware, plugin compatibility, or hyprlock/PAM authentication and lock security. QEMU's independent `screendump` returned `no surface` on the EGL display path; the retained, visually inspected screenshots were captured through Wayland.

`session-evidence/session-results.json`, `companion-results.json`, `keyboard-results.json`, and `shutdown-results.json` describe the individual checks. The bundle manifest's `hardware_session_tested = false` still describes its ordinary container-validation boundary; the VM evidence is separate and does not change publication gates.

### Co-installable Lua
The package family is `lua55`, `lua55-libs`, and `lua55-devel`:
* Commands: `/usr/bin/lua5.5` and `/usr/bin/luac5.5`.
* Headers: `/usr/include/lua5.5`.
* Shared ABI: `liblua5.5.so.0`; linker metadata: `pkgconfig(lua55)`.
* Modules: `/usr/share/lua/5.5` and `/usr/lib64/lua/5.5`.

There is no unversioned `lua` provide, system-binary replacement, generic `lua.pc`, or installed static archive. Hyprland selects Lua55 through pkg-config and the versioned ELF SONAME. Fedora's Lua 5.4 and its modules remain independent.

## Tests
Unit tests need only the Python standard library:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q hyprland_copr container deploy tests ci
```

Run the complete containerized lint/syntax/unit checks:

```bash
docker build -f container/Checksfile -t hyprland-copr-checks .
docker run --rm --network=none -v "$PWD:/workspace:ro,z" hyprland-copr-checks
```

The checks image installs Ruff, ShellCheck, rpmlint, and the COPR client from Fedora repositories. No pip, Go installer, third-party tool image, or host software installation is required. Checks run offline with a read-only source mount and temporary container-only caches.

`ci/check.py` runs Ruff, unit tests, Python compilation, and ShellCheck/`bash -n` on `.sh`/`.bash` files under `ci`, `container`, `deploy`, `hyprland_copr`, `packaging`, and `tests`, excluding generated `.state`/`dist` artifacts. Deployment Python scripts are included in lint and syntax checks. Linter failures are fatal. There is no actionlint dependency because this project does not use GitHub Actions.

The local automation runner invokes the same checks before every build. Real `run --component NAME` builds additionally run rpmlint and test the integrated source/RPM/container path; Glaze includes staged and installed consumers, and companions include headless CLI and asset validation. `run --lua-only` retains the Lua coexistence/embedding/module checks. Mock publisher tests do not establish live COPR compatibility.

To audit an existing bundle without rebuilding it:

```bash
docker run --rm --network=none -v "$PWD/dist/stack:/bundle:ro,z" \
    --entrypoint bash hyprland-copr-checks \
    -c 'rpmlint /bundle/srpms/*.rpm /bundle/rpms/*.rpm'
```

Older bundles are not retroactively qualified by the new gate: an audit may report missing changelogs or other metadata errors, and a rebuild is required to incorporate packaging fixes. `verify` remains an integrity/provenance check; it does not run these linters.

## COPR setup and publication
`config/project.toml` targets **`byte0/hyprland`**, with `copr.publish = true` and `copr.exclusive_writer = true`. The latter is an operational commitment: disable all other writers before running publication. The pipeline never creates projects or silently changes live project settings.

An anonymous inspection on 2026-10-24 found this project's `devel_mode = false`, `follow_fedora_branching = true`, and `storage = "pulp"`. **Those settings block the guarded publisher.** Before unattended publication, the operator must arrange the following supported project policy; do not weaken the checks to fit the current settings. Existing Pulp repository migration/storage changes require COPR-supported administration rather than an assumed in-place toggle.

Configure the dedicated `fedora-44-x86_64` project with:
* Manual repository publication / development mode, initialized repository metadata, and normal COPR signing.
* No additional repositories, additional build packages/modules, alternative bootstrap image, automatic Fedora branching, or external submission hooks.
* Build network access disabled. Only the project's own public/development overlays are permitted in addition to the Fedora chroot's normal official repositories.
* Backend storage (`storage = "backend"`). Pulp layouts are not currently supported by the publisher's build-specific URL and metadata checks.
* This pipeline as the **sole build submitter and publisher**. Do not submit manual builds, run another pipeline, or manually regenerate metadata while candidates are pending.

COPR's asynchronous regeneration API has no atomic candidate-set or compare-and-swap parameter. The pipeline checks project policy, signing key, build inventories, public/development metadata, build-specific URLs, package identities, and checksums before publication. These checks depend on the sole-writer prerequisite; they are not an atomic multi-writer transaction. Unknown API/repository layouts fail closed and require review.

Keep credentials in the COPR client config supplied by your secret store, never in this tree. Set `COPR_CONFIG_FILE` to its protected path, or use `~/.config/copr` for the automation account. The local controller checks file metadata, not credential contents: the file must be regular, owned by that account, have no group/other permissions (normally mode `0600`), and be outside the checkout, pipeline state, and artifact directories. Symlinked credential files are rejected. Keep its containing directory protected too. No token is passed on a command line or embedded in generated configuration.

Authenticated submission/promotion use `container/Publisherfile`, which installs Fedora's `python3-copr` and `python3-rpm`. Containers run as the automation UID, with a read-only root filesystem, dropped capabilities, no Docker socket, read-only Python code/bundle/config/credential mounts, and writable recovery state. No source compilation, package installation, or candidate execution occurs in those containers. Candidate validation runs in a separate local process with a temporary credential-free home and an allowlisted environment; its disposable test containers receive no COPR credentials. A custom Docker context may require an explicit `DOCKER_CONFIG` path because validation does not use the account's normal home.

The three phases are deliberately separate:

```bash
python3 -m hyprland_copr copr-build dist/stack --state .state/copr.json
python3 -m hyprland_copr copr-validate dist/stack --state .state/copr.json
python3 -m hyprland_copr copr-promote dist/stack --state .state/copr.json --yes
```

`copr-build` submits the exact checksummed SRPMs in dependency order and waits for **external COPR rebuilds**. It does not upload local binary RPMs. `copr-validate` runs locally **without credentials**: it downloads pinned signed COPR artifacts, checks their signatures and header identities, tests Fedora/prior-publication upgrades, and writes a validation receipt. `copr-promote` rechecks that receipt and unchanged server state before regenerating public metadata, then observes convergence. The scheduled runner invokes these same phases using the isolation above. The raw commands are retained for deliberate operator recovery; run authenticated commands only in a trusted Fedora publishing environment.
Both direct submission and promotion require the current source policy. Legacy validated bundles remain available for read-only `verify`, but must be resolved and rebuilt under the new policy before publication; old pending bundles cannot be promoted through the new gate.

Persist the complete state and candidate artifacts across those phases. Re-running the same command resumes recorded IDs. An upload with an uncertain result is not blindly repeated: inspect the COPR build and reconcile its exact source provenance first. An uncertain regeneration is observed, not retriggered. Drift, unrelated pending candidates, failed builds, changed keys, or failed validation prevent promotion.
COPR automatic pruning can also change the captured inventory. Such drift requires operator reconciliation; it is never silently accepted as the original reviewed repository. Keep recovery artifacts independently of COPR's build retention.

After publication, Fedora clients can enable the configured project using Fedora's repository-installed `dnf copr enable byte0/hyprland`, then upgrade `hyprland`. The pipeline does not change host repositories or install its RPMs on your desktop.

## On-premises automation
Deploy the source tree to your server using an ordinary Git clone or other source synchronization. Install Python 3.11+, Git, and Docker from the server distribution's repositories, and use a non-root account with access to the Docker daemon. No GitHub CLI, hosted automation account, or GitHub secrets are required. Stop scheduled/active runs before updating the checkout; the runner does not fetch or update project code automatically.

Run these commands from the deployed project directory:

```bash
python3 -m ci.release --checks-only
python3 -m ci.release
python3 -m ci.release --no-publish
python3 -m ci.release --component hyprpaper
python3 -m ci.release --lua-only --version 5.5.1
```

The `ci` package is just local Python tooling, not a hosted CI service. The runner builds the Fedora checks image, executes all repository checks offline in a non-root container, then calls the existing local build/validation CLI. Image preparation, release discovery, and dependency downloads need network access; local compilation and runtime tests stay offline.

Complete-stack runs automatically publish only after local QA, all builds, runtime validation, and bundle integrity verification succeed. The manifest must use the current source policy and select Hyprland plus all four companion applications. Submission is followed by local validation of the signed COPR candidates; only then is public metadata regenerated and checked for convergence. Any failed gate stops later phases. `--checks-only`, `--component`, `--lua-only`, `--no-publish`, or `copr.publish = false` prevent automatic publication and pending-publication recovery. `--no-publish` still builds and validates a fresh local stack.

Each invocation creates a unique `dist/runs/<UTC-timestamp>-<unique-id>/` directory containing:
* `logs/checks-image.log` and `logs/checks.log`.
* `logs/build.log` for build invocations.
* `logs/publisher-image.log`, `logs/copr-build.log`, `logs/copr-validate.log`, and `logs/copr-promote.log` for the publication phases actually executed.
* `resolution.json`, the complete resolution saved before building, when resolution succeeds or reports dependency blockers.
* `bundle/`, with RPMs, SRPMs, validation metadata, and per-component logs when a build is attempted.
* `result.json`, recording pass/failure/blocker status, exit code, local artifact paths, publication progress, and destination. `publication_attempted` means the publication workflow was entered, not proof a remote mutation occurred.

`--output PATH` chooses a run directory explicitly; it must not already exist. The runner rejects overlapping invocations with a separate automation lock, while the core CLI retains its existing state lock. Checks must pass before building; dependency blockers retain exit code 3 instead of being treated as success. A killed/interrupted run can leave a `running` result and partial output; inspect its logs and retry into a new directory. Neither success nor failure removes previous artifacts.

For publication-enabled full-stack runs, the runner uses `--skip-published`: it still resolves releases, but avoids a new build/publication when the complete selected source-version set, source policy, primary selection, packaging release, and destination match the persisted promoted state. This is a local publication record, not a fresh audit of live repository health. Increment `pipeline.release` for packaging-only changes. Without automatic publication it performs a fresh build.

Before submission, `.state/automation/publication.json` records the exact verified bundle path, manifest digest, destination, and publication-state path. Subsequent complete-stack scheduled runs perform QA, reverify that bundle, and resume publication instead of resolving/rebuilding another stack. `validated`, `promoting`, and `promoted` states go directly to guarded promotion/observation, retaining the signed-candidate receipt. The handoff is removed only after confirmed promotion. `--version` cannot replace a pending publication. Custom `--publication-state` paths must reside inside `pipeline.state_directory` for automatic publication.

### Install the local systemd schedule
Run the installer **on the automation server as the non-root automation account**, without sudo:

```bash
python3 deploy/install.py
```

It uses the templates in `deploy/systemd/` to create `hyprland-copr.service` and `hyprland-copr.timer` under `$XDG_CONFIG_HOME/systemd/user` (or `~/.config/systemd/user`). The service points at this checkout's actual absolute path and the Python interpreter running the installer, so no hard-coded clone location or manual path editing is required. Rerun the installer after moving the checkout or changing interpreters.

Before writing files, the installer checks Python 3.11+, Git, Docker, and systemd tooling, validates the generated units with `systemd-analyze --user verify`, checks the user manager and Docker daemon, and refuses to update an active automation service. It does not install software or change Docker permissions, host repositories, publication settings, or user lingering.

Installation then reloads the user manager, enables the timer, and starts/restarts **the timer only** so schedule updates take effect. The default is every six hours in the server's local timezone, with up to five minutes of jitter and persistent catch-up after a missed start. **Enabling it can start a build and guarded publication immediately if a scheduled run was missed.** Prepare credentials and the supported live-project policy first, or stage with `--no-enable`. An already installed timer uses the updated runner automatically.

The default credential location is relative to the service account's home, not your interactive user's home. To use another protected file, set only its path with `Environment=COPR_CONFIG_FILE=/protected/path/copr` in a user-service drop-in, then reload the user manager. Never put credential contents in unit files or environment variables.

To stage the files without contacting the user manager or changing activation:

```bash
python3 deploy/install.py --no-enable
```

This still requires the prerequisite commands and validates the generated files, but does not check daemon availability, reload units, enable, disable, or start anything. It does not disable an already enabled timer. Activate the staged units later with `systemctl --user daemon-reload`, `systemctl --user enable hyprland-copr.timer`, and `systemctl --user restart hyprland-copr.timer`.

To choose a different systemd calendar expression:

```bash
python3 deploy/install.py --calendar daily
```

Rerunning safely updates installer-managed files; unrelated units and symlinks are never overwritten. If you previously copied the example units manually, back up and remove those two files before the first installer run. Keep custom settings in systemd drop-ins rather than editing managed files. If activation fails after installation, the validated unit files remain available for inspection and retry.

Use `systemctl --user list-timers hyprland-copr.timer --no-pager` to inspect the schedule, `systemctl --user start hyprland-copr.service` for an immediate run, and `journalctl --user -u hyprland-copr.service --no-pager` to find its run directory. `systemctl --user disable --now hyprland-copr.timer` stops future scheduled triggers without interrupting an already running build.

Enable user lingering through the server's normal administration process if the timer must run while the account is logged out. Rootless/custom Docker deployments may also need their Docker socket/context environment configured in a user-service drop-in; the installer checks the invoking account's Docker access, not a different manager environment. The timer does not require a GitHub runner.

### Local recovery and retention
Back up `.state/` and `dist/runs/` independently of the source repository. Retain failed runs for diagnosis; there is no remote artifact store or automatic pruning. Per-run resolutions contain absolute source/cache paths, so keep their referenced source archives/checkouts and account for those paths when moving between hosts.

To rebuild the exact saved resolution after an operational build failure, use a new bundle directory:

```bash
python3 -m hyprland_copr build dist/runs/RUN/resolution.json --output dist/retry-RUN
python3 -m hyprland_copr verify dist/retry-RUN
```

Blocked resolutions must be resolved again after their dependency issue is fixed; do not bypass their blocked status. Preserve the exact validated bundle, `.state/automation/publication.json`, `.state/copr.json`, and `.state/copr-candidates/` together. For automatically initiated publication, rerun `python3 -m ci.release` into a new run directory; it resumes the durable handoff. State from a manually initiated operation without a handoff blocks new automatic releases and requires explicit recovery. If using the raw phases and promotion is already pending, resume `copr-promote` directly rather than repeating earlier phases and discarding its validation receipt. An uncertain submission, incompatible live project policy, or repository change still requires operator reconciliation. Do not delete pending state merely to allow another submission. No hosted run IDs or artifact downloads are involved.
