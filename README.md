# MTV Releng Tooling

Automation tooling for MTV (Migration Toolkit for Virtualization) release engineering.
Handles automatic IIB builds, FBC catalog management, Konflux/Jenkins orchestration, and Slack notifications.

---

## Repository layout

```
mtv_pipelines/          # Main Python package
  main.py               # CLI entry point (pipeline subcommands)
  utils.py              # Shared helpers (temp dirs, Jira key extraction, …)
  auth/                 # Environment-backed credential helpers
  config/               # config.yaml + accessor functions
  core/                 # Async pipeline runner, task graph, DB persistence
  models/               # Domain models (Bundle, Component, IIB, FBCRepo, …)
  pipelines/            # One module per CLI subcommand
  tasks/                # Reusable async/sync units of work
  wrappers/             # Thin wrappers around external tools (Slack, Git, gh, Skopeo, Jenkins)
  templates/            # Slack Block Kit JSON snippets

scripts/                # Shell scripts (legacy / complementary automation)
templates/              # Top-level JSON templates used by shell scripts
Containerfile           # Python 3.14 image with Poetry, skopeo, gh
Makefile                # install / build / shell / run targets
pyproject.toml          # Poetry project + dependencies
```

---

## Running the tooling

### Prerequisites

| Requirement | Notes |
|-------------|-------|
| Python 3.14 | Managed via Poetry |
| `skopeo` | For pulling/copying container images |
| `gh` CLI | GitHub CLI for PR management |
| Registry access | [registry.redhat.io](https://access.redhat.com/terms-based-registry/) token |

**Install dependencies:**
```bash
make install   # runs poetry install
```

**Run a pipeline:**
```bash
# The pipeline name is the first token of ARGS (there is no PIPELINE variable):
make run ARGS="<pipeline_name> [options]"
# e.g. make run ARGS="branching 5.0.0 --ocp-versions v4.22-v5.0"
# or directly (outside the container):
poetry run python mtv_pipelines/main.py <pipeline_name> [options]
```

**Run inside the container:**
```bash
make build      # build container image
make shell      # open a shell inside the container
make run        # run the container
```

### Required environment variables

| Variable | Purpose |
|----------|---------|
| `SLACK_AUTH_TOKEN` | Slack Bot token |
| `GH_TOKEN` | GitHub personal access token (push + PRs to kubev2v repos) |
| `GITLAB_TOKEN` / `GITLAB_USER` | GitLab token (`write_repository` + `api`) and optional user for `btrfs_sync` (mtv/forklift) and `konflux_stream` (konflux-release-data) |
| `KONFLUX_SERVICE_TOKEN` | `oc login` token for the service cluster (rh-mtv-1); used by `bundle_sync` to read releases/snapshots |
| `KONFLUX_BTRFS_TOKEN` | `oc login` token for the btrfs cluster (rh-mtv-btrfs) |
| `JENKINS_USER` | Jenkins username |
| `JENKINS_TOKEN` | Jenkins API token |
| `REGISTRY_PROD_USER` / `REGISTRY_PROD_TOKEN` | Production registry credentials |
| `REGISTRY_STAGE_USER` / `REGISTRY_STAGE_TOKEN` | Stage registry credentials |
| `STORAGE_OFFLOAD_CLUSTER_EDGE112` | Kubeadmin password for ocp-edge112; required when MTV 2.11 storage offload is enabled |
| `STORAGE_OFFLOAD_CLUSTER_EDGE113` | Kubeadmin password for ocp-edge113; required when MTV 2.12 storage offload is enabled |
| `ROOTCOZ` | Rootcoz Bearer token for the Jenkins analyzer API |

---

## Pipelines

Each pipeline is a subcommand of `main.py`. Run `python mtv_pipelines/main.py <pipeline> --help` for available flags.

### `automatic_iib`
The main end-to-end automation pipeline:

1. Login to Skopeo registries
2. Fetch latest stage bundles for each tracked MTV version
3. Prepare and process FBC repos (OPM, catalog files)
4. Open/update GitHub PRs for each FBC change
5. Wait for Konflux PR checks to pass
6. Extract component commits from previous and current IIB
7. Compute per-repo commit diffs with Jira key extraction
8. Send a Slack build summary (OCP URLs, Jira issues, per-repo changes, Konflux info)
9. Trigger Jenkins jobs (release gate, non-gate, optional storage offload / UI tests)
10. Wait for Jenkins jobs and post CI status to Slack

**Key flags:** `--process-version`, `--process-bundle`, `--skip-jenkins`, `--skip-slack`

After Konflux FBC checks pass, `automatic_iib` also upserts `/app/data/latest_iib.json` (host `./data/latest_iib.json`) for versions listed under `tier1_jobs` in `config.yaml`. Saturday `trigger_tier1` reads that pointer. Konflux is not the scheduler.

### `trigger_tier1`
Weekly (Saturday) pipeline: trigger dedicated Jenkins tier1 jobs against the latest IIB recorded by `automatic_iib`.

1. Read `data/latest_iib.json` (missing file → nothing to do until the first successful weekday IIB)
2. For each `tier1_jobs` entry (today: MTV 2.12 on OCP 4.22, MTV 5.0 on OCP 5.0), rewrite the IIB OCP infix and trigger `mtv-{xy}-ocp-{ocp}-test-tier1` unless `last_tier1_iib` already equals that IIB
3. After Jenkins accepts the job, set `last_tier1_iib` so the same IIB is not tested again
4. Post a Slack parent thread per version on the same slackMTV bot / `slack_builds_channel` as weekday builds (not the IIB changelog), then job links
5. Wait for Jenkins, run the analyzer (best-effort), post CI pass/fail in the same thread

The container stays up until Jenkins finishes, like `automatic_iib`. This is not a Konflux timer; run it from the same releng host and `./data/` volume as weekday IIB automation:

```bash
make run ARGS="trigger_tier1"
```

Host crontab example (ops, not stored in this repo):

```cron
0 8 * * 6 cd /path/to/mtv-releng && make run ARGS="trigger_tier1"
```

Pointer file shape (one key per MTV x.y):

```json
{
  "2.12": {
    "mtv_version": "2.12.6",
    "iib": "forklift-fbc-prod-v50:on-pr-abc123",
    "recorded_at": "2026-09-17T14:02:00Z",
    "last_tier1_iib": null
  }
}
```

**Key flags:** `--skip-slack`, `--dry-run`

**Env:** same as `automatic_iib` (`JENKINS_USER`, `JENKINS_TOKEN`, `SLACK_AUTH_TOKEN`, `ROOTCOZ`)

### `handle_pr`
Processes an IIB from an FBC PR (PR-driven flow, mirrors `automatic_iib`).

**Key flags:** `--mtv`, `--ocps`

### `extract_iib_info`
Extracts component commit SHAs for a given IIB image.

**Key flags:** `--iib`, `--mtv`

### `extract_diff`
Computes the commit diff between two IIBs.

**Key flags:** `--new_iib`, `--old_iib`, `--old_version`, `--new_version`

### `create_fbc_release`
Creates a release branch in the FBC repo and opens a PR.

**Key flags:** `--process-bundle`

### `analyze_jenkins`
Waits for Jenkins job URLs to finish, analyzes failures, and optionally posts to a Slack thread.

**Key flags:** `--ts`

### `btrfs_sync`
Mirrors a public forklift branch into the internal GitLab repository (rebases onto the internal mirror and origin, then force-pushes). Uses shallow, targeted fetches. Python port of `scripts/btrfs_sync.sh`.

**Key flags:** `--branch` (required), `--dry-run`

### `bundle_sync`
Updates `Containerfile-downstream` ARG `*_IMAGE` SHA references from the latest stage snapshot and opens (or rebases and updates an existing) PR against the target branch. Pulls the virt-v2v-int SHA from the internal quay tenant. Python port of `scripts/bundle_sync.sh`. **Dry-run by default** — pass `--apply` to push.

**Key flags:** `version` (positional, required), `--target-branch`, `--apply`

### `branching`
Branches the MTV operator repos (forklift, console-plugin, must-gather) for a release. For each origin it creates the `release-X.Y` branch from main and opens a `CF-<version>` code-freeze PR into it that regenerates `build/release.conf` and retargets the `.tekton` pipelines from dev-preview to the release stream (and, for forklift, bumps `images.conf`). Python port of the MTV-repo half of `scripts/branching.sh` (uses the bot's direct push + PR model instead of personal forks). **Dry-run by default** — pass `--apply`.

**Key flags:** `version` (positional, required), `--origin` (repeatable), `--release`, `--channel`, `--default-channel`, `--registry`, `--ocp-versions`, `--apply`

### `konflux_stream`
Adds a release stream to the Konflux releng repo (`konflux-release-data`): renders the prod tenant's `operator/streams/<version>.yaml` from `dev-preview.yaml` and registers it in `kustomization.yaml`, appends the btrfs tenant `ProjectDevelopmentStream`, retargets both tenants' `ReleasePlanAdmission`s, runs `build-single.sh` (optionally `tox`), pushes `mtv_add_stream`, and opens the MR via the GitLab API (falls back to logging manual instructions if the token lacks `api` scope). Python port of the Konflux half of `scripts/branching.sh` (post-#53 model). **Dry-run by default** — pass `--apply`.

**Key flags:** `version` (positional, required), `--registry`, `--run-tox`, `--apply`

### `dep_bump`
Mass dependency and base image update for one release branch, as one PR per repo (forklift and forklift-must-gather; the console plugin is left to MintMaker). It automates what kubev2v/forklift #8571/#8572 (base image refresh, patch bumps) and #8853/#8854 (Go CVE fixes) did by hand.

1. **Base images.** For every `registry.redhat.io` `FROM` in the downstream Containerfiles (forklift `build/**/Containerfile-downstream*` except the operator bundle; must-gather `build/Dockerfile-downstream`), it picks the newest build of the *same stream*: same Go minor for go-toolset (`1.25.x`), same RHEL minor for ubi (`9.7-…`), same OpenShift minor for `ose-*` (`v4.18.0-…`). Digest pins such as must-gather's `ose-cli-rhel9@sha256:…` stay digest pins, moved to the newest manifest list of the stream. RHEL, Go or OpenShift minor jumps are never made routinely.
2. **Go modules** (forklift root, `cmd/kubectl-mtv`, the vsphere populator, `.konflux/validation`). Security fixes come first: `govulncheck -scan module` (for `.konflux/validation`, which only pins OPA, the built OPA binary is scanned instead). Each target is taken to the newest patch release of its fixed minor, which also picks up fixes the Go vulnerability database doesn't list (e.g. mongo-driver 1.17.10 for CVE-2026-88031). When one target needs a newer version of another, `go get` says so and that target is raised. Patch bumps of direct dependencies are then listed against the post-security state. A bump is kept only if:
   - it doesn't move a guarded module (`cascade_guard`: k8s, kubevirt, govmomi, ...) by a minor version, or anything by a major version;
   - it doesn't raise the module's `go` directive, unless all of the module's builders are free of RPM lockfiles (validation, kubectl-mtv). Those builders then move to a go-toolset that supports the new directive;
   - the module still builds (`go build` with `-mod=vendor`, as Konflux does).

   If the combined set fails, the bumps are retried one at a time and only the passing ones are kept. Root changes are re-vendored into the modules that `replace` forklift with `../../`. A module with a pre-existing problem is skipped with one note instead of a rejection per bump: its baseline build fails, it isn't tidy-clean (`go mod tidy` alone moves guarded modules), or re-running `go mod tidy`/`go mod vendor` alone breaks its build (stale `vendor/`). `.konflux/validation` is never tidied (it has no imports, so tidy would drop every requirement); its `go.sum` is filled by building OPA, then checked with a read-only build as Konflux does. Everything left out is listed in the PR with the reason.
3. **PR.** Tables of image and Go changes, what was left out and why, and the RPM-locked components whose base changed, with the exact `scripts/rpm_lockfile_refresh.sh` command. The body is also written to `dep_bump.report_dir` (`data/dep_bump/<repo>-<branch>.md`), so dry runs can be reviewed. RPM lockfiles, `.tekton` digests, bundle `ARG *_IMAGE` nudges and `release.conf` are not touched.

**Dry-run by default** (it still clones and runs the Go tooling, but stops before committing). If an open `dep_bump` PR already exists for the branch, the run is skipped unless `--update-existing` is given; that replaces the PR branch and drops commits added to it by hand, such as RPM lockfile refreshes.

Run it through `make run` (Linux container): the forklift root module links libvirt through cgo, so builds need the image's `libvirt-dev`, and they fail on macOS. Local builds use `-tags=strictfipsruntime` without Red Hat's `strictfipsruntime` Go experiment, so they approximate the Konflux build; Konflux CI on the PR does the real one.

```bash
make run ARGS="dep_bump --branch release-2.12"                 # dry run, both repos
make run ARGS="dep_bump --branch release-2.12 --repo forklift --apply"
```

**RPM lockfiles.** After the PR is open, on an x86_64 VM with podman and skopeo, check out the PR branch and run the command from the PR body:

```bash
REGISTRY_AUTH_USER='11009103|migration-toolkit-virtualization' REGISTRY_AUTH_PASS=... \
  scripts/rpm_lockfile_refresh.sh /path/to/forklift .konflux/forklift-api .konflux/virt-v2v/appliance
```

For each component it reads the `rpms.in.yaml` stage's base image and takes the RHEL version from its `cpe` label. Components sharing a solver image (same RHEL major and minor) are then refreshed together in one `ubiN` container — one RHSM registration and one dependency install per group — following the RPM update guide: register with the `mtv-konflux-rhelN` activation key, disable EUS repos, regenerate `redhat.repo`, then run `rpm-lockfile-prototype`. Registrations are retried and unregistered on exit, and failed components get one retry after a cooldown, since RHSM rate-limits rapid registrations. `winlegacyiso` (RHEL 7 packages) uses the guide's two-stage ubi7 → ubi9 flow. The script never commits; review `git status` and push to the PR branch.

**Key flags:** `--branch` (required), `--repo`, `--skip-images`, `--skip-go`, `--apply`, `--update-existing`, `--report-dir`

**Env:** `GH_TOKEN`, `REGISTRY_PROD_USER` / `REGISTRY_PROD_TOKEN`

---

## Tasks

Reusable async/sync work units composed inside pipelines.

| Task | Purpose |
|------|---------|
| `extract_bundle_from_iib` | Pulls IIB image, finds the `mtv-operator` bundle image ref in the catalog |
| `extract_cmps_from_bundle` | Pulls bundle, parses the CSV, returns a `Component` list with commits |
| `get_origin_commits_from_cmps` | Maps component images to upstream repo SHAs via config `cmp_mappings` |
| `extract_info` | Orchestrates bundle → components → origin commits for one IIB |
| `get_commit_diff` | Walks `git log` between two SHAs and builds `CommitDTO` list with Jira keys |
| `get_mtv_versions` | Fetches `VERSION=X.Y.Z` from raw GitHub URLs per repo/branch |
| `prepare_slack_build` | Assembles a `SlackBuildMessageDTO` from FBC repo + diffs |
| `process_fbc_repo` | Bumps the prerelease version in FBC git, updates catalogs per OCP, commits/pushes |
| `process_ocp_catalog` | Initializes/renders an OPM catalog, adds bundle and channel entries |
| `wait_for_pr` | Polls GitHub PR checks; retries `/retest` on failure until success or max retries |
| `bundle_sync_sha` | virt-v2v probe + snapshot→SHA mapping and Containerfile ARG edits for the `bundle_sync` pipeline |
| `branching` | release.conf rendering + `.tekton` retargeting transforms for the `branching` pipeline |
| `konflux_stream` | prod stream / btrfs PDS / ReleasePlanAdmission transforms for the `konflux_stream` pipeline |

---

## Wrappers

| Wrapper | External tool | Purpose |
|---------|--------------|---------|
| `slack.py` | `slack-sdk` | Block Kit message builder + `send_build` / `send_ci_status` methods |
| `git.py` | `GitPython` | Clone (incl. shallow), fetch, checkout, rebase/merge, add (incl. dirs/renames), commit, push (incl. force/with-lease), remotes |
| `gh_cli.py` | `gh` CLI | List/create PRs, list/trigger checks, post comments |
| `skopeo.py` | `skopeo` | `inspect`, `copy`, `login` for stage/prod registries |
| `oc.py` | `oc` CLI | Latest release, snapshot-from-release, snapshot content (for `bundle_sync`) |
| `jenkins.py` | `python-jenkins` | Trigger jobs, wait for queue/build, MTV-specific job helpers |
| `jenkins_analyzer.py` | HTTP service | POST failed job info to analyzer, return structured `JenkinsJobAnalysisDTO` |

### Slack build message structure

When a build completes, `send_build` posts a threaded message with the following blocks:

1. **Header** — `IIB <version> | <date> UTC`
2. **OCP Versions** — per-OCP FBC fragment URLs
3. **Changes introduced since previous build** — previous build context
4. **Jira Issues** — deduplicated list of all Jira keys found across all repos (linked)
5. **\<repo\> changes** _(one block per origin)_ — table of Jira / commit message / SHA
6. **Konflux** — bundle URL and image commit SHAs

---

## Models

| Model | Purpose |
|-------|---------|
| `Bundle` | Operator bundle image metadata (version, OCPs, channel, CPE, RHEL) |
| `Component` | Bundle component (image ref, upstream commit SHA) |
| `IIB` | IIB image reference + semver version |
| `FBCRepo` | Cloned FBC git workspace, OPM operations, catalog helpers |
| `GitRepo` | Named upstream clone in a temp dir |
| `dto.py` | All Pydantic DTOs used between pipelines/tasks |

---

## Shell scripts (`scripts/`)

Legacy and complementary shell-based automation. See [`scripts/README.md`](scripts/README.md) for bundle sync details.

Key scripts:

| Script | Purpose |
|--------|---------|
| `automatic_iib.sh` | End-to-end IIB automation shell entry point |
| `iib_notify.sh` | Posts IIB diff to Slack |
| `extract_diff.sh` / `extract_info.sh` | IIB diff extraction |
| `branching.sh` | MTV operator branching helper |
| `jenkins_*.sh` | Jenkins trigger/watch/report |
| `bundle_sync.sh` | Syncs bundles between registries |
| `snapshot_*.sh` | Snapshot management |
| `create_release_cr.sh` | Creates release CR |
| `btrfs_sync.sh` | BTRFS-based image sync |

### Legacy script flow of automatic IIB builds

```
automatic_iib
├── authenticate
├── latest_stage_bundle
│   └── verify_versions
├── create_iib_pr
│   └── verify_versions
├── wait_for_build
├── extract_diff
│   ├── extract_info (previous IIB)
│   │   ├── iib → replace_for_quay → bundle → replace_for_quay → component
│   └── extract_info (next IIB)
│   └── commit_history
└── iib_notify
```

---

## Branching helper

Automates branching of the MTV operator across all relevant repositories.

> **Note:** there are now Python equivalents — the `branching` pipeline (MTV repos)
> and `konflux_stream` pipeline (Konflux releng side). Both are dry-run by default
> (`--apply` to push). They use the automation bot's direct push + PR model rather
> than personal forks. The interactive shell script below remains for manual runs.

```bash
scripts/branching.sh
```

**What it does:**
- Creates `release-X.Y` branches for `forklift`, `forklift-console-plugin`, and `forklift-must-gather`
- Modifies version files with correct values
- Pushes changes to user forks
- Creates Konflux releng files and pushes them to the releng repo

**Prerequisites:** Forks of all repositories to be branched.

---

## Configuration

All tunable settings live in [`mtv_pipelines/config/config.yaml`](mtv_pipelines/config/config.yaml):

- Operator name, cutoff version, CPE version init
- OCP cluster mappings (version → cluster name)
- Tracked MTV versions and branches
- FBC repo URL and image URL templates
- Slack channel IDs and failure mention users
- Jira base URL and allowed project keys
- Jenkins URL, analyzer URL, retry intervals
- Registry namespaces
- Component mappings (`cmp_mappings`) — downstream name → upstream + git origin
- Commit character limit for Slack messages
- Storage offload cluster mappings (`storage_offload_clusters`) — MTV x.y → cluster name, API URL, OCP version, and `password_env`
- Saturday tier1 job map (`tier1_jobs`) — MTV x.y → OCP version for `mtv-{xy}-ocp-{ocp}-test-tier1`, plus `latest_iib_state_path`

### Storage offload clusters

The `storage_offload_clusters` map in `config.yaml` controls which cluster is used for copyoffload tests per MTV minor version:

```yaml
storage_offload_clusters:
  "2.11":
    custom_cluster_name: "ocp-edge112"
    ocp_api_url: "https://api.ocp-edge112-0.lab.eng.tlv2.redhat.com:6443"
    ocp_version: "4.20"
    password_env: "STORAGE_OFFLOAD_CLUSTER_EDGE112"
  "2.12":
    custom_cluster_name: "ocp-edge113"
    ocp_api_url: "https://api.ocp-edge113-0.lab.eng.tlv2.redhat.com:6443"
    ocp_version: "4.21"
    password_env: "STORAGE_OFFLOAD_CLUSTER_EDGE113"
```

Each entry's `password_env` names the environment variable holding the kubeadmin password for that cluster. Add a new entry here (and the corresponding env var to `.env`) to onboard additional clusters.
