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
| `JIRA_EMAIL` / `JIRA_API_TOKEN` | Jira Cloud REST credentials for `cve_triage` (Browse + Add Comments + Edit on MTV security issues) |

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

### `cve_triage`
Weekly triage of open MTV `SecurityTracking` Jira tickets. For each ticket it:

1. Parses the branch (`[mtv-2.11]` → `release-2.11`), CVE and `pscomponent` label
2. Resolves the image digest the branch's operator bundle pins (`cmp_mappings` ARG) and pulls it from the Konflux quay tenant. A component the bundle doesn't ship (e.g. `mtv-must-gather-rhel8`) is `NOT_BUILT`
3. Reads the shipped version of the affected package from the Go buildinfo / Python `dist-info` in the image, or from the branch lockfile for the console plugin. Layers shared with the final `FROM` image mark the package as inherited from the base image
4. Gets vulnerable ranges and import paths from OSV, falling back to the flaw text
5. For packages inherited from an OpenShift base image, looks up OpenShift's OCPBUGS tracker for the same CVE and stream
6. Classifies the ticket: `FIXED_IN_BUILD`, `NOT_AFFECTED_VERSION`, `NOT_AFFECTED_CODE_ABSENT`, `NOT_AFFECTED_OCP`, `NOT_BUILT`, `BLOCKED_ON_OCP`, `BASE_BUMP_NEEDED`, `AFFECTED`, `NEEDS_REVIEW` or `SKIPPED` (embargoed)
7. Writes `report-<timestamp>.md` / `.json` to `cve_triage.report_dir` and posts a Slack summary (counts + Jira links only) to `cve_triage.slack_channel`

The triage is deterministic: API lookups, file parsing and a fixed rule order, with no AI involved. The only heuristics are the `package_aliases` title keywords and the flaw-text version regex, used when OSV has no record; when they find nothing the ticket is `NEEDS_REVIEW`. Any failure while scanning a component (registry, network, archive, OCPBUGS lookup) also lands its tickets in `NEEDS_REVIEW` rather than aborting the run or guessing.

```text
 1. COLLECT
    Jira REST: open SecurityTracking tickets (MTV)
      [mtv-2.11] -> release-2.11 | CVE-* label -> CVE | pscomponent -> component
      skip tickets that already carry a triage:* label
                                  |
 2. ANALYZE  (once per branch + component; once per CVE)
    OSV: vulnerable ranges + import paths    Operator bundle on the branch:
      (fallback: flaw-text regex,              ARG <cmp>_IMAGE = digest
       title -> package keywords)              bundle ships another image -> NOT_BUILT
                                                     |
                                               Konflux quay image @ digest
                                                 Go binaries   -> buildinfo
                                                 Python        -> dist-info
                                                 console plugin -> branch lockfile
                                                     |
                                               Component Containerfile, final FROM:
                                                 version label  -> OpenShift stream
                                                 layer digests  -> "inherited"
                                                     |
                                               inherited from openshift4/*?
                                                 -> OCPBUGS tracker (CVE + image + stream)
                                  |
 3. CLASSIFY  (first match wins)
    embargoed ....................................... SKIPPED
    bundle ships a different image .................. NOT_BUILT
    package not found / scan failed ................. NEEDS_REVIEW
    shipped version outside the range:
      older than the vulnerable range ............... NOT_AFFECTED_VERSION
      at or above the fix ........................... FIXED_IN_BUILD
    vulnerable paths not linked into the binary ..... NOT_AFFECTED_CODE_ABSENT
    vulnerable copy built by MTV .................... AFFECTED
    vulnerable copy inherited from OpenShift, tracker:
      Not a Bug ..................................... NOT_AFFECTED_OCP
      Done / Errata ................................. BASE_BUMP_NEEDED
      still open .................................... BLOCKED_ON_OCP
      not found ..................................... NEEDS_REVIEW
                                  |
 4. ACT & REPORT
    report-<ts>.md / .json -> ./data/cve_triage (private host volume)
    --apply: evidence comment ([cve-triage:X] marker) + triage:* label
             (never changes the ticket status)
    Slack (private channel only): counts + Jira links
```

The findings describe unfixed vulnerabilities on non-public trackers, so evidence and comment bodies only go to the report file on the host's `./data/` volume, never to logs or Slack. Set `slack_channel` to a private channel; when it's empty no summary is posted.

**Dry-run by default.** With `--apply` it posts the structured assessment comment and a `triage:*` label (`ready-for-assigned`, `ready-to-close`, `blocked-upstream`, `needs-fix`). It never changes a ticket's status; a person verifies the label and transitions or closes the ticket. Tickets that already carry a `triage:*` label are skipped unless listed with `--keys`. A ticket with an existing `[cve-triage:...]` comment is never commented on twice, but still gets its label, so a run that commented and then failed to label is completed by the next one.

Curated `vulnerable_paths` in `config.yaml` take precedence over OSV's import list for the "vulnerable code not present" check; add an entry when OSV lists shared packages that every binary links.

```bash
make run ARGS="cve_triage --keys MTV-6839,MTV-6519"   # dry run, specific tickets
make run ARGS="cve_triage --apply"                    # weekly run
```

Host crontab example (ops, not stored in this repo; Monday 07:00):

```cron
0 7 * * 1 cd /path/to/mtv-releng && make run ARGS="cve_triage --apply"
```

**Key flags:** `--apply`, `--keys`, `--jql`, `--branches`, `--skip-slack`, `--report-dir`

**Env:** `JIRA_EMAIL`, `JIRA_API_TOKEN`, `SLACK_AUTH_TOKEN`, `REGISTRY_PROD_USER` / `REGISTRY_PROD_TOKEN` (to inspect base images on registry.redhat.io; stage credentials are not needed)

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
