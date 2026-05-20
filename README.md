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
make run PIPELINE=<pipeline_name> ARGS="..."
# or directly:
poetry run python mtv_pipelines/main.py <pipeline> [options]
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
| `GH_TOKEN` | GitHub personal access token |
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

---

## Wrappers

| Wrapper | External tool | Purpose |
|---------|--------------|---------|
| `slack.py` | `slack-sdk` | Block Kit message builder + `send_build` / `send_ci_status` methods |
| `git.py` | `GitPython` | Clone, log, checkout, commit, push |
| `gh_cli.py` | `gh` CLI | List/create PRs, list/trigger checks, post comments |
| `skopeo.py` | `skopeo` | `inspect`, `copy`, `login` for stage/prod registries |
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
