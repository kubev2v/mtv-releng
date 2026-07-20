# Jenkins Scripts - Bespoke Execution Guide

This guide explains how to run the Jenkins scripts individually for debugging, partial runs, or custom workflows.

## Overview

The Jenkins scripts are now modular and support both:
- **Integrated execution**: Run all stages together via `jenkins_call.sh`
- **Bespoke execution**: Run individual stages with data handoff between stages

## Data Handoff Files

The scripts use JSON files to pass data between stages:

- **`job_tracking.json`**: Contains job information after triggering (job names, numbers, URLs, OCP versions)
- **`job_status.json`**: Contains job information + final status after monitoring

## Script Commands

### 1. jenkins_trigger.sh - Job Triggering

```bash
# Trigger jobs and export data
./scripts/jenkins_trigger.sh trigger <IIB> <MTV_VERSION> <OCP_VERSIONS> <RC> [CLUSTER_NAME] [JOB_SUFFIX] [MATRIX_TYPE]

# Import job data from file
./scripts/jenkins_trigger.sh import <job_data_file>

# Export current job data
./scripts/jenkins_trigger.sh export [output_file]
```

**Arguments:**
- `JOB_SUFFIX`: Job name suffix (default: `gate`, can be comma-separated, e.g., `gate`, `non-gate`, `gate,non-gate`)
- `MATRIX_TYPE`: Matrix type (default: `RELEASE`)
  - Single value: applies to all job suffixes (e.g., `RELEASE`, `RELEASE_NON_GATE`, `FULL`, `STAGE`, `TIER1`, `UPGRADE`)
  - Mapping format: different matrix types per suffix (e.g., `gate:RELEASE,non-gate:RELEASE_NON_GATE`)

**Examples:**
```bash
# Trigger jobs for OCP 4.20 (default cluster: qemtv-01, default suffix: gate, default matrix: RELEASE)
./scripts/jenkins_trigger.sh trigger 'forklift-fbc-prod-v420:on-pr-abc123' '2.10.0' '4.20' 'false'

# Trigger jobs for OCP 4.20 on specific cluster
./scripts/jenkins_trigger.sh trigger 'forklift-fbc-prod-v420:on-pr-abc123' '2.10.0' '4.20' 'false' 'qemtv-02'

# Trigger non-gate jobs with FULL matrix type
./scripts/jenkins_trigger.sh trigger 'forklift-fbc-prod-v420:on-pr-abc123' '2.10.0' '4.20' 'false' 'qemtv-01' 'non-gate' 'FULL'

# Trigger both gate and non-gate jobs with same matrix type
./scripts/jenkins_trigger.sh trigger 'forklift-fbc-prod-v420:on-pr-abc123' '2.10.0' '4.20' 'false' 'qemtv-01' 'gate,non-gate' 'RELEASE'

# Trigger both gate and non-gate jobs with different matrix types
./scripts/jenkins_trigger.sh trigger 'forklift-fbc-prod-v420:on-pr-abc123' '2.10.0' '4.20' 'false' 'qemtv-01' 'gate,non-gate' 'gate:RELEASE,non-gate:RELEASE_NON_GATE'

# Import jobs from previous run
./scripts/jenkins_trigger.sh import job_tracking.json

# Export current jobs to custom file
./scripts/jenkins_trigger.sh export my_jobs.json
```

### 2. jenkins_watch.sh - Job Monitoring

```bash
# Watch jobs until completion and export status
./scripts/jenkins_watch.sh watch <job_data_file>

# Check current status without waiting
./scripts/jenkins_watch.sh status <job_data_file>

# Import job data from file
./scripts/jenkins_watch.sh import <job_data_file>

# Export current status data
./scripts/jenkins_watch.sh export [output_file]
```

**Examples:**
```bash
# Wait for jobs to complete
./scripts/jenkins_watch.sh watch job_tracking.json

# Check current status only
./scripts/jenkins_watch.sh status job_tracking.json

# Import jobs and export status
./scripts/jenkins_watch.sh import job_tracking.json
./scripts/jenkins_watch.sh export current_status.json
```

### 3. jenkins_report.sh - Result Reporting

```bash
# Generate comprehensive report (display + JSON)
./scripts/jenkins_report.sh report <status_data_file> <MTV_VERSION> <DEV_PREVIEW> <RC> <IIB> [format]

# Display results only
./scripts/jenkins_report.sh display <status_data_file>

# Generate JSON only
./scripts/jenkins_report.sh json <status_data_file> <MTV_VERSION> <DEV_PREVIEW> <RC> <IIB>

# Import status data from file
./scripts/jenkins_report.sh import <status_data_file>
```

**Examples:**
```bash
# Full report with both display and JSON
./scripts/jenkins_report.sh report job_status.json '2.10.0' 'false' 'true' 'forklift-fbc-prod-v420:on-pr-abc123'

# Display only
./scripts/jenkins_report.sh display job_status.json

# JSON only
./scripts/jenkins_report.sh json job_status.json '2.10.0' 'false' 'true' 'forklift-fbc-prod-v420:on-pr-abc123'

# Different output formats
./scripts/jenkins_report.sh report job_status.json '2.10.0' 'false' 'true' 'forklift-fbc-prod-v420:on-pr-abc123' 'display'
./scripts/jenkins_report.sh report job_status.json '2.10.0' 'false' 'true' 'forklift-fbc-prod-v420:on-pr-abc123' 'json'
```

## Common Workflows

### 1. Full Pipeline (Traditional)
```bash
# Default cluster (qemtv-01), default suffix (gate), default matrix (RELEASE)
./scripts/jenkins_call.sh 'forklift-fbc-prod-v420:on-pr-abc123' '2.10.0' '4.20' 'false'

# Specific cluster
./scripts/jenkins_call.sh 'forklift-fbc-prod-v420:on-pr-abc123' '2.10.0' '4.20' 'false' 'qemtv-02'

# Both gate and non-gate jobs with different matrix types
./scripts/jenkins_call.sh 'forklift-fbc-prod-v420:on-pr-abc123' '2.10.0' '4.20' 'false' 'qemtv-01' 'gate,non-gate' 'gate:RELEASE,non-gate:RELEASE_NON_GATE'

# Different clusters and matrix types for different job types
./scripts/jenkins_call.sh 'forklift-fbc-prod-v420:on-pr-abc123' '2.10.0' '4.20' 'false' 'gate:qemtv-01,non-gate:qemtv-02' 'gate,non-gate' 'gate:RELEASE,non-gate:RELEASE_NON_GATE'
```

### 2. Trigger Only
```bash
# Default cluster (qemtv-01), default suffix (gate), default matrix (RELEASE)
./scripts/jenkins_trigger.sh trigger 'forklift-fbc-prod-v420:on-pr-abc123' '2.10.0' '4.20' 'false'

# Specific cluster
./scripts/jenkins_trigger.sh trigger 'forklift-fbc-prod-v420:on-pr-abc123' '2.10.0' '4.20' 'false' 'qemtv-02'
# Creates: job_tracking.json

# Non-gate jobs with FULL matrix type
./scripts/jenkins_trigger.sh trigger 'forklift-fbc-prod-v420:on-pr-abc123' '2.10.0' '4.20' 'false' 'qemtv-01' 'non-gate' 'FULL'
# Creates: job_tracking.json

# Different clusters for different job types
./scripts/jenkins_trigger.sh trigger 'forklift-fbc-prod-v420:on-pr-abc123' '2.10.0' '4.20' 'false' 'gate:qemtv-01,non-gate:qemtv-02' 'gate,non-gate' 'RELEASE'
# Creates: job_tracking.json
```

### 3. Watch Previously Triggered Jobs
```bash
./scripts/jenkins_watch.sh watch job_tracking.json
# Creates: job_status.json
```

### 4. Report on Completed Jobs
```bash
./scripts/jenkins_report.sh report job_status.json '2.10.0' 'false' 'true' 'forklift-fbc-prod-v420:on-pr-abc123'
```

### 5. Debugging Workflow
```bash
# Step 1: Trigger jobs (with optional job suffix and matrix type)
./scripts/jenkins_trigger.sh trigger 'forklift-fbc-prod-v420:on-pr-abc123' '2.10.0' '4.20' 'false' 'qemtv-01' 'gate,non-gate' 'gate:RELEASE,non-gate:RELEASE_NON_GATE'

# Step 2: Check what was triggered
cat job_tracking.json

# Step 3: Watch jobs
./scripts/jenkins_watch.sh watch job_tracking.json

# Step 4: Check final status
cat job_status.json

# Step 5: Generate report
./scripts/jenkins_report.sh report job_status.json '2.10.0' 'false' 'true' 'forklift-fbc-prod-v420:on-pr-abc123'
```

### 6. Re-run Failed Jobs
```bash
# If jobs failed, you can re-trigger just the failed ones
# First, check what failed
./scripts/jenkins_report.sh display job_status.json

# Then trigger new jobs for failed OCP versions
./scripts/jenkins_trigger.sh trigger 'forklift-fbc-prod-v420:on-pr-abc123' '2.10.0' '4.20' 'false'
```

### 7. Status Check Only (No Waiting)
```bash
# Check current status without waiting for completion
./scripts/jenkins_watch.sh status job_tracking.json
```

## Data File Formats

### job_tracking.json
```json
{
  "jobs": [
    {
      "job_name": "mtv-2.10-ocp-4.20-test-release-gate",
      "job_number": "65",
      "job_url": "https://jenkins-csb-mtv-qe-main.dno.corp.redhat.com/job/mtv-2.10-ocp-4.20-test-release-gate/65/",
      "ocp_version": "4.20"
    },
    {
      "job_name": "mtv-2.10-ocp-4.20-test-release-non-gate",
      "job_number": "42",
      "job_url": "https://jenkins-csb-mtv-qe-main.dno.corp.redhat.com/job/mtv-2.10-ocp-4.20-test-release-non-gate/42/",
      "ocp_version": "4.20"
    }
  ]
}
```

### job_status.json
```json
{
  "jobs": [
    {
      "job_name": "mtv-2.10-ocp-4.20-test-release-gate",
      "job_number": "65",
      "job_url": "https://jenkins-csb-mtv-qe-main.dno.corp.redhat.com/job/mtv-2.10-ocp-4.20-test-release-gate/65/",
      "ocp_version": "4.20",
      "status": "SUCCESS"
    },
    {
      "job_name": "mtv-2.10-ocp-4.20-test-release-non-gate",
      "job_number": "42",
      "job_url": "https://jenkins-csb-mtv-qe-main.dno.corp.redhat.com/job/mtv-2.10-ocp-4.20-test-release-non-gate/42/",
      "ocp_version": "4.20",
      "status": "SUCCESS"
    }
  ]
}
```

## Job Types, Clusters, and Matrix Types

### Job Suffixes

The scripts support different job types through the `JOB_SUFFIX` parameter:

- **`gate`**: Standard gate jobs (default)
  - Job name pattern: `mtv-{version}-ocp-{ocp_version}-test-release-gate`
  - Example: `mtv-2.10-ocp-4.19-test-release-gate`

- **`non-gate`**: Non-gate jobs
  - Job name pattern: `mtv-{version}-ocp-{ocp_version}-test-release-non-gate`
  - Example: `mtv-2.10-ocp-4.19-test-release-non-gate`

You can trigger multiple job types simultaneously by providing comma-separated values:
```bash
# Trigger both gate and non-gate jobs
./scripts/jenkins_call.sh <IIB> <MTV_VERSION> <OCP_VERSIONS> <RC> <CLUSTER> 'gate,non-gate'
```

### Cluster Names

The `CLUSTER_NAME` parameter specifies which Jenkins cluster to use. It supports multiple mapping formats:

#### Single Cluster (All Jobs)
When you provide a single value, it applies to all jobs:
```bash
# All jobs use qemtv-01 cluster
./scripts/jenkins_call.sh <IIB> <MTV_VERSION> <OCP_VERSIONS> <RC> 'qemtv-01' 'gate,non-gate'
```

#### Different Clusters Per Job Suffix
You can specify different clusters for different job suffixes:
```bash
# Gate jobs use qemtv-01, non-gate jobs use qemtv-02
./scripts/jenkins_call.sh <IIB> <MTV_VERSION> <OCP_VERSIONS> <RC> 'gate:qemtv-01,non-gate:qemtv-02' 'gate,non-gate'
```

#### Different Clusters Per OCP Version
You can specify different clusters for different OCP versions:
```bash
# OCP 4.19 jobs use qemtv-01, OCP 4.20 jobs use qemtv-02
./scripts/jenkins_call.sh <IIB> <MTV_VERSION> '4.19,4.20' <RC> '4.19:qemtv-01,4.20:qemtv-02' 'gate,non-gate'
```

#### Combined Mapping (OCP Version + Job Suffix)
You can specify different clusters for each combination of OCP version and job suffix:
```bash
# OCP 4.19 gate -> qemtv-01, OCP 4.19 non-gate -> qemtv-02
# OCP 4.20 gate -> qemtv-03, OCP 4.20 non-gate -> qemtv-04
./scripts/jenkins_call.sh <IIB> <MTV_VERSION> '4.19,4.20' <RC> \
  '4.19:gate:qemtv-01,4.19:non-gate:qemtv-02,4.20:gate:qemtv-03,4.20:non-gate:qemtv-04' \
  'gate,non-gate'
```

**Mapping Format Priority:**
1. Combined mapping (most specific): `ocp-version:suffix:cluster`
2. OCP version mapping: `ocp-version:cluster`
3. Suffix mapping: `suffix:cluster`
4. Single value (least specific): `cluster`

### Matrix Types

The `MATRIX_TYPE` parameter controls which test matrix is executed. Supported values include:
- `RELEASE`: Release test matrix (default) - tier0 tests on all platforms (gate jobs)
- `RELEASE_NON_GATE`: Release non-gate test matrix - tier0 tests on RHV, remote, and OpenStack platforms (non-gate release testing)
- `FULL`: Full test matrix
- `STAGE`: Stage test matrix
- `TIER1`: Tier 1 test matrix (tier1 feature testing)
- `UPGRADE`: Upgrade test matrix (cluster locking + major upgrade/minor update testing)

#### Single Matrix Type (All Jobs)
When you provide a single value, it applies to all job suffixes:
```bash
# All jobs (gate and non-gate) use RELEASE matrix
./scripts/jenkins_call.sh <IIB> <MTV_VERSION> <OCP_VERSIONS> <RC> <CLUSTER> 'gate,non-gate' 'RELEASE'
```

#### Different Matrix Types Per Job Suffix
You can specify different matrix types for different job suffixes using mapping format:
```bash
# Gate jobs use RELEASE, non-gate jobs use RELEASE_NON_GATE
./scripts/jenkins_call.sh <IIB> <MTV_VERSION> <OCP_VERSIONS> <RC> <CLUSTER> 'gate,non-gate' 'gate:RELEASE,non-gate:RELEASE_NON_GATE'
```

The mapping format is: `suffix1:type1,suffix2:type2`

### IIB Values

The `IIB_MAP` parameter allows you to specify different IIB (Image Index Bundle) values for different jobs. It supports the same mapping formats as clusters:

#### Single IIB Value (All Jobs)
When you provide a single IIB value (or omit IIB_MAP), it applies to all jobs:
```bash
# All jobs use the same IIB
./scripts/jenkins_call.sh 'forklift-fbc-prod-v420:on-pr-abc123' <MTV_VERSION> <OCP_VERSIONS> <RC> <CLUSTER> 'gate,non-gate'
```

#### Different IIB Values Per Job Suffix
You can specify different IIB values for different job suffixes:
```bash
# Gate jobs use one IIB, non-gate jobs use another
./scripts/jenkins_call.sh 'forklift-fbc-prod-v420:on-pr-abc123' <MTV_VERSION> <OCP_VERSIONS> <RC> <CLUSTER> 'gate,non-gate' <MATRIX> \
  'gate:forklift-fbc-prod-v420:on-pr-abc123,non-gate:forklift-fbc-prod-v420:on-pr-xyz789'
```

#### Different IIB Values Per OCP Version
You can specify different IIB values for different OCP versions:
```bash
# OCP 4.19 uses one IIB, OCP 4.20 uses another
./scripts/jenkins_call.sh 'forklift-fbc-prod-v420:on-pr-abc123' <MTV_VERSION> '4.19,4.20' <RC> <CLUSTER> 'gate,non-gate' <MATRIX> \
  '4.19:forklift-fbc-prod-v420:on-pr-abc123,4.20:forklift-fbc-prod-v420:on-pr-xyz789'
```

#### Combined IIB Mapping (OCP Version + Job Suffix)
You can specify different IIB values for each combination of OCP version and job suffix:
```bash
# OCP 4.19 gate -> IIB1, OCP 4.19 non-gate -> IIB2
# OCP 4.20 gate -> IIB3, OCP 4.20 non-gate -> IIB4
./scripts/jenkins_call.sh 'forklift-fbc-prod-v420:on-pr-abc123' <MTV_VERSION> '4.19,4.20' <RC> <CLUSTER> 'gate,non-gate' <MATRIX> \
  '4.19:gate:forklift-fbc-prod-v420:on-pr-abc123,4.19:non-gate:forklift-fbc-prod-v420:on-pr-xyz789,4.20:gate:forklift-fbc-prod-v420:on-pr-def456,4.20:non-gate:forklift-fbc-prod-v420:on-pr-ghi789'
```

**Note:** The first IIB argument is still required and serves as the default value if no mapping is provided or if a mapping doesn't match.

### Combining All Options

You can combine cluster mapping and matrix type mapping for maximum flexibility:
```bash
# Example 1: Different clusters per suffix, different matrix types per suffix
# Gate jobs: qemtv-01 cluster with RELEASE matrix
# Non-gate jobs: qemtv-02 cluster with RELEASE_NON_GATE matrix
./scripts/jenkins_call.sh <IIB> <MTV_VERSION> <OCP_VERSIONS> <RC> \
  'gate:qemtv-01,non-gate:qemtv-02' \
  'gate,non-gate' \
  'gate:RELEASE,non-gate:RELEASE_NON_GATE'

# Example 2: Different clusters per OCP version, same matrix type
# OCP 4.19: qemtv-01, OCP 4.20: qemtv-02
./scripts/jenkins_call.sh <IIB> <MTV_VERSION> '4.19,4.20' <RC> \
  '4.19:qemtv-01,4.20:qemtv-02' \
  'gate,non-gate' \
  'RELEASE'

# Example 3: Combined mapping with different matrix types
# OCP 4.19 gate -> qemtv-01/RELEASE, OCP 4.19 non-gate -> qemtv-02/RELEASE_NON_GATE
# OCP 4.20 gate -> qemtv-03/RELEASE, OCP 4.20 non-gate -> qemtv-04/RELEASE_NON_GATE
./scripts/jenkins_call.sh <IIB> <MTV_VERSION> '4.19,4.20' <RC> \
  '4.19:gate:qemtv-01,4.19:non-gate:qemtv-02,4.20:gate:qemtv-03,4.20:non-gate:qemtv-04' \
  'gate,non-gate' \
  'gate:RELEASE,non-gate:RELEASE_NON_GATE'

# Example 4: Different IIB values per job
# Gate jobs use one IIB, non-gate jobs use another
./scripts/jenkins_call.sh 'forklift-fbc-prod-v420:on-pr-abc123' <MTV_VERSION> <OCP_VERSIONS> <RC> <CLUSTER> 'gate,non-gate' <MATRIX> \
  'gate:forklift-fbc-prod-v420:on-pr-abc123,non-gate:forklift-fbc-prod-v420:on-pr-xyz789'

# Example 5: Full combination - different clusters, matrix types, and IIB values
./scripts/jenkins_call.sh 'forklift-fbc-prod-v420:on-pr-abc123' <MTV_VERSION> '4.19,4.20' <RC> \
  '4.19:gate:qemtv-01,4.19:non-gate:qemtv-02,4.20:gate:qemtv-03,4.20:non-gate:qemtv-04' \
  'gate,non-gate' \
  'gate:RELEASE,non-gate:RELEASE_NON_GATE' \
  '4.19:gate:forklift-fbc-prod-v420:on-pr-abc123,4.19:non-gate:forklift-fbc-prod-v420:on-pr-xyz789,4.20:gate:forklift-fbc-prod-v420:on-pr-def456,4.20:non-gate:forklift-fbc-prod-v420:on-pr-ghi789'
```

## Benefits of Bespoke Execution

1. **Debugging**: Run individual stages to isolate issues
2. **Partial Runs**: Resume from any stage if previous stages completed
3. **Custom Workflows**: Mix and match stages as needed
4. **Data Inspection**: Examine intermediate data files
5. **Re-runs**: Re-run specific stages without starting over
6. **Integration**: Use individual scripts in CI/CD pipelines

## Environment Variables

### Required Variables

All scripts require:
- `JENKINS_USER`: Jenkins username
- `JENKINS_TOKEN`: Jenkins API token

### Optional Configuration Variables

You have three flexible ways to configure cluster and matrix mappings:

#### 1. JSON Configuration File (Recommended for Complex Configurations)

Create a JSON config file (see `jenkins_config.example.json` for format):

```json
{
  "clusters": {
    "by_ocp_and_suffix": {
      "4.19": {
        "gate": "qemtv-01",
        "non-gate": "qemtv-02"
      },
      "4.20": {
        "gate": "qemtv-03",
        "non-gate": "qemtv-04"
      }
    }
  },
  "matrix_types": {
    "by_suffix": {
      "gate": "RELEASE",
      "non-gate": "RELEASE_NON_GATE"
    }
  },
  "job_suffixes": {
    "common": ["gate", "non-gate"]
  }
}
```

**Usage:**
```bash
# Direct reference
./scripts/jenkins_call.sh <IIB> <MTV_VERSION> '4.19,4.20' <RC> '@jenkins_config.json'

# Via environment variable
export JENKINS_CONFIG_FILE=jenkins_config.json
./scripts/jenkins_call.sh <IIB> <MTV_VERSION> '4.19,4.20' <RC>
```

#### 2. Environment Variables (Good for Simple Cases)

Set environment variables for cleaner calls:

- `JENKINS_CONFIG_FILE`: Path to JSON config file (auto-used if CLUSTER_NAME not provided)
- `JENKINS_CLUSTER_MAP`: Cluster mapping string (overrides CLUSTER_NAME argument if not provided)
- `JENKINS_JOB_SUFFIX`: Job suffix (overrides JOB_SUFFIX argument if not provided)
- `JENKINS_MATRIX_MAP`: Matrix type mapping string (overrides MATRIX_TYPE argument if not provided)

**Example:**
```bash
export JENKINS_CLUSTER_MAP='4.19:gate:qemtv-01,4.19:non-gate:qemtv-02,4.20:gate:qemtv-03,4.20:non-gate:qemtv-04'
export JENKINS_MATRIX_MAP='gate:RELEASE,non-gate:RELEASE_NON_GATE'
./scripts/jenkins_call.sh <IIB> <MTV_VERSION> '4.19,4.20' <RC> '' 'gate,non-gate'
```

#### 3. Inline Arguments (Best for Automation/Scripts)

Pass mappings directly as arguments - perfect for automation where you want explicit control:

```bash
./scripts/jenkins_call.sh <IIB> <MTV_VERSION> '4.19,4.20' <RC> \
  '4.19:gate:qemtv-01,4.19:non-gate:qemtv-02,4.20:gate:qemtv-03,4.20:non-gate:qemtv-04' \
  'gate,non-gate' \
  'gate:RELEASE,non-gate:RELEASE_NON_GATE'
```

**Priority Order (highest to lowest):**
1. Inline arguments (explicit)
2. Environment variables
3. JSON config file (if `JENKINS_CONFIG_FILE` is set or `@path` is used)
4. Defaults

## Jenkins Job Parameters

The scripts send the following parameters to Jenkins jobs:

- `BRANCH`: Git branch (master)
- `CLUSTER_NAME`: Jenkins cluster name (default: qemtv-01)
- `DEPLOY_MTV`: Deploy MTV flag (true)
- `GIT_BRANCH`: Git branch (main)
- `IIB_NO`: Image Index Bundle
- `MATRIX_TYPE`: Matrix type (default: RELEASE)
  - Can be a single value (applies to all job suffixes): `RELEASE`, `RELEASE_NON_GATE`, `FULL`, `STAGE`, `TIER1`, `UPGRADE`
  - Can be a mapping format (different types per suffix): `gate:RELEASE,non-gate:RELEASE_NON_GATE`
- `MTV_API_TEST_GIT_USER`: MTV API test git user (RedHatQE)
- `MTV_SOURCE`: MTV source (KONFLUX)
- `MTV_VERSION`: MTV version
- `MTV_XY_VERSION`: MTV XY version (e.g., 2.10 from 2.10.0)
- `NFS_SERVER_IP`: NFS server IP
- `NFS_SHARE_PATH`: NFS share path
- `OCP_VERSION`: OCP version
- `OCP_XY_VERSION`: OCP XY version
- `OPENSHIFT_PYTHON_WRAPPER_GIT_BRANCH`: Python wrapper branch (main)
- `PYTEST_EXTRA_PARAMS`: Pytest extra parameters
- `RC`: Release candidate flag
- `REMOTE_CLUSTER_NAME`: Remote cluster name (same as CLUSTER_NAME)
- `RUN_TESTS_IN_PARALLEL`: Run tests in parallel (true)

## Error Handling

- Each script validates inputs and provides clear error messages
- Data files are validated for proper JSON format
- Scripts exit with appropriate error codes for automation
