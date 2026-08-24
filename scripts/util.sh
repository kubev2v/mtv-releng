#!/usr/bin/env bash

# singleton env var to prevent reexporting during multiple "source scripts/util.sh" calls
if [[ -z "${CMD_OUTPUT_PATH:-}" ]]; then
  export CMD_OUTPUT_PATH=$(pwd)
fi

if [[ -z "${MAIN_WORKER_PID:-}" ]]; then
  export MAIN_WORKER_PID=$$
fi

# Prevent multiple sourcing of readonly variables
if [[ -z "${UTIL_SOURCED:-}" ]]; then
  export UTIL_SOURCED=1

  # Ensure common tool locations are in PATH (script may run with minimal PATH in VM/cron/non-interactive)
  for dir in /usr/local/bin "$HOME/bin" "$HOME/.local/bin"; do
    if [[ -d "$dir" && ":$PATH:" != *":$dir:"* ]]; then
      export PATH="$dir:$PATH"
    fi
  done
  
  # Constants
  export JENKINS_BASE_URL="https://jenkins-csb-mtv-qe-main.dno.corp.redhat.com"
  
  # Status constants
  export STATUS_SUCCESS="SUCCESS"
  export STATUS_FAILURE="FAILURE"
  export STATUS_RUNNING="RUNNING"
  export STATUS_QUEUED="QUEUED"
  export STATUS_ABORTED="ABORTED"
  export STATUS_UNKNOWN="UNKNOWN"
  
  # Colors for output
  export RED='\033[0;31m'
  export GREEN='\033[0;32m'
  export YELLOW='\033[1;33m'
  export BLUE='\033[0;34m'
  export NC='\033[0m' # No Color
fi

# Function to parse job info from JOB_TRACKING
parse_job_info() {
    local job_info="$1"
    local job_number="${job_info%%|*}"
    local remaining="${job_info#*|}"
    
    local parts=($(echo "$remaining" | tr '|' '\n'))
    local num_parts=${#parts[@]}
    
    if [ $num_parts -eq 2 ]; then
        local job_url="${parts[0]}"
        local ocp_version="${parts[1]}"
        echo "$job_number|$job_url|$ocp_version"
    elif [ $num_parts -eq 3 ]; then
        local job_url="${parts[0]}"
        local ocp_version="${parts[1]}"
        local trigger_time="${parts[2]}"
        echo "$job_number|$job_url|$ocp_version|$trigger_time"
    else
        local job_url="${remaining%|*}"
        local last_part="${remaining##*|}"
        if [[ "$last_part" =~ ^[0-9]+\.[0-9]+$ ]]; then
            local ocp_version="$last_part"
            echo "$job_number|$job_url|$ocp_version"
        else
            local ocp_version="${last_part%|*}"
            local trigger_time="${last_part##*|}"
            echo "$job_number|$job_url|$ocp_version|$trigger_time"
        fi
    fi
}

# Function to make Jenkins API calls with proper authentication and error handling
jenkins_api_call() {
    local url="$1"
    local data="$2"
    local method="${3:-GET}"  # Default to GET, allow POST to be specified
    local include_headers="${4:-false}"  # Whether to include headers in response
    
    # Debug: Check if authentication is available
    if [ -z "${JENKINS_USER:-}" ] || [ -z "${JENKINS_TOKEN:-}" ]; then
        log_error "JENKINS_USER or JENKINS_TOKEN not set for API call to: $url" >&2
        return 1
    fi
    
    if [ -n "$data" ] && [ "$method" = "POST" ]; then
        # For POST requests with data (job triggering)
        if [ "$include_headers" = "true" ]; then
            curl -s -S -f -i --insecure --connect-timeout 15 --max-time 60 -X POST \
                --user "$JENKINS_USER:$JENKINS_TOKEN" \
                -H "Content-Type: application/x-www-form-urlencoded" \
                --data "$data" \
                "$url"
        else
            curl -s -S -f --insecure --connect-timeout 15 --max-time 60 -X POST \
                --user "$JENKINS_USER:$JENKINS_TOKEN" \
                -H "Content-Type: application/x-www-form-urlencoded" \
                --data "$data" \
                "$url"
        fi
    else
        # For GET requests (status checks)
        curl -s -S -f --insecure --connect-timeout 15 --max-time 60 \
            --user "$JENKINS_USER:$JENKINS_TOKEN" \
            "$url"
    fi
}

# Function to validate build ownership
validate_build_ownership() {
    local job_name="$1"
    local build_number="$2"
    local trigger_timestamp="$3"
    
    log_info "Validating build ownership for ${job_name}#${build_number} (trigger: ${trigger_timestamp})" >&2
    
    local build_url="${JENKINS_BASE_URL}/job/${job_name}/${build_number}/api/json"
    local build_response
    
    if ! build_response=$(jenkins_api_call "$build_url" ""); then
        log_warning "Failed to get build info for ${job_name}#${build_number}" >&2
        return 1
    fi
    
    local build_timestamp
    build_timestamp=$(echo "$build_response" | jq -r '.timestamp // empty' 2>/dev/null)
    
    if [ -z "$build_timestamp" ] || [ "$build_timestamp" = "null" ]; then
        log_warning "No timestamp found for build ${job_name}#${build_number}" >&2
        return 1
    fi
    
    # Convert timestamps to seconds for comparison
    local trigger_seconds=$((trigger_timestamp / 1000))
    local build_seconds=$((build_timestamp / 1000))
    
    # Allow a 5-minute window for build to start after trigger, with small negative tolerance for clock differences
    local time_diff=$((build_seconds - trigger_seconds))
    
    if [ $time_diff -ge -30 ] && [ $time_diff -le 300 ]; then
        return 0  # Build is within expected timeframe
    else
        log_warning "Build #${build_number} timestamp (${build_seconds}s) doesn't match expected trigger time (${trigger_seconds}s), diff: ${time_diff}s" >&2
        return 1
    fi
}

# Function to extract OCP version from parsed job info string
# Takes the "remaining" part from parse_job_info (after job_number|)
extract_ocp_version_from_parsed_info() {
    local remaining="$1"
    
    # Format: job_url|ocp_version|trigger_time (3 parts) - extract middle
    if [[ "$remaining" =~ \|[0-9]+\.[0-9]+\| ]]; then
        echo "$remaining" | cut -d'|' -f2
    else
        # Format: job_url|ocp_version (2 parts) - extract last
        echo "${remaining##*|}"
    fi
}

# Function to extract job components from parsed job info
# Returns job_number|job_url|ocp_version
extract_job_components() {
    local job_info="$1"
    local parsed_info=$(parse_job_info "$job_info")
    local job_number="${parsed_info%%|*}"
    local remaining="${parsed_info#*|}"
    local job_url="${remaining%%|*}"
    local ocp_version=$(extract_ocp_version_from_parsed_info "$remaining")
    
    echo "$job_number|$job_url|$ocp_version"
}

# Function to import job tracking data from JSON file
# Used by multiple scripts for consistency
import_job_data() {
    local input_file="$1"
    
    if [ ! -f "$input_file" ]; then
        log_error "Input file not found: $input_file"
        return 1
    fi
    
    # Clear existing tracking data
    JOB_TRACKING=()
    
    # Parse JSON and populate JOB_TRACKING
    local job_count=$(jq '.jobs | length' "$input_file" 2>/dev/null)
    
    if [ -z "$job_count" ] || [ "$job_count" = "null" ]; then
        log_error "Invalid JSON format in: $input_file"
        return 1
    fi
    
    for ((i=0; i<job_count; i++)); do
        local job_name=$(jq -r ".jobs[$i].job_name" "$input_file" 2>/dev/null)
        local job_number=$(jq -r ".jobs[$i].job_number" "$input_file" 2>/dev/null)
        local job_url=$(jq -r ".jobs[$i].job_url" "$input_file" 2>/dev/null)
        local ocp_version=$(jq -r ".jobs[$i].ocp_version" "$input_file" 2>/dev/null)
        
        if [ "$job_name" != "null" ] && [ "$job_number" != "null" ] && [ "$job_url" != "null" ] && [ "$ocp_version" != "null" ]; then
            JOB_TRACKING["$job_name"]="${job_number}|${job_url}|${ocp_version}"
            log_info "Imported job: $job_name (build $job_number)"
        fi
    done
    
    log_success "Imported $job_count jobs from: $input_file"
    return 0
}

# Function to update JOB_TRACKING with a resolved build number
# Preserves existing structure (job_url, ocp_version, trigger_time)
# Usage: update_job_tracking_with_build <job_name> <new_build_number>
update_job_tracking_with_build() {
    local job_name="$1"
    local new_build_number="$2"
    
    # Get current job info
    local job_info="${JOB_TRACKING[$job_name]}"
    if [ -z "$job_info" ]; then
        log_warning "No existing job info found for $job_name" >&2
        return 1
    fi
    
    # Parse existing structure
    local parsed_info=$(parse_job_info "$job_info")
    local remaining="${parsed_info#*|}"
    local job_url_only="${remaining%%|*}"
    local ocp_version=$(extract_ocp_version_from_parsed_info "$remaining")
    
    # Determine if trigger_time exists and preserve it
    if [[ "$remaining" =~ \|[0-9]+$ ]]; then
        # Format: job_url|ocp_version|trigger_time (3 parts)
        local trigger_time=$(echo "$remaining" | cut -d'|' -f3)
        JOB_TRACKING["$job_name"]="${new_build_number}|${JENKINS_BASE_URL}/job/${job_name}/${new_build_number}/|${ocp_version}|${trigger_time}"
    else
        # Format: job_url|ocp_version (2 parts)
        JOB_TRACKING["$job_name"]="${new_build_number}|${JENKINS_BASE_URL}/job/${job_name}/${new_build_number}/|${ocp_version}"
    fi
}

# Function to check job API for latest build and validate ownership
# Returns the build number if found and validated, empty string otherwise
# This is a single check (no polling) - callers handle retry logic
check_job_for_latest_build() {
    local job_name="$1"
    local trigger_timestamp="$2"
    
    local job_info_url="${JENKINS_BASE_URL}/job/${job_name}/api/json"
    local job_info_response
    
    if job_info_response=$(jenkins_api_call "$job_info_url" "" 2>/dev/null); then
        local latest_build
        latest_build=$(echo "$job_info_response" | jq -r '.lastBuild.number // empty' 2>/dev/null)
        
        if [ -n "$latest_build" ] && [ "$latest_build" != "null" ]; then
            if validate_build_ownership "$job_name" "$latest_build" "$trigger_timestamp" 2>/dev/null; then
                echo "$latest_build"
                return 0
            fi
        fi
    fi
    # Could not find or validate build
    return 1
}

# Function to check if a queue item has resolved to a build number
# Returns the build number if found and validated, empty string otherwise
# This is a single check (no polling) - callers handle retry logic
check_queue_item_for_build() {
    local job_name="$1"
    local queue_item="$2"
    local trigger_timestamp="$3"
    
    # Check queue item API for executable build number
    local queue_url="${JENKINS_BASE_URL}/queue/item/${queue_item}/api/json"
    local queue_response
    
    if queue_response=$(jenkins_api_call "$queue_url" "" 2>/dev/null); then
        # Queue item exists - check if job has started (has executable)
        local executable_build
        executable_build=$(echo "$queue_response" | jq -r '.executable.number // empty' 2>/dev/null)
        
        if [ -n "$executable_build" ] && [ "$executable_build" != "null" ]; then
            # Validate build ownership
            if validate_build_ownership "$job_name" "$executable_build" "$trigger_timestamp" 2>/dev/null; then
                echo "$executable_build"
                return 0
            fi
        fi
        # Build not ready or validation failed
        return 1
    else
        # Queue item no longer exists (404) - try to get latest build from job
        check_job_for_latest_build "$job_name" "$trigger_timestamp"
        return $?
    fi
}

# Generic polling wrapper function
# Polls a check function until it succeeds or times out
# Usage: poll_until_success <check_function> <wait_message> <success_message> <timeout_message> <max_wait_time> <poll_interval> [check_function_args...]
#   - check_function: Name of function to call for checking
#   - wait_message: Message to log when starting to wait
#   - success_message: Message to log when check succeeds (should include %s for result)
#   - timeout_message: Message to log on timeout
#   - max_wait_time: Maximum time to wait in seconds
#   - poll_interval: Time between polls in seconds (0 = single check mode)
#   - check_function_args: Arguments to pass to check function
poll_until_success() {
    local check_function="$1"
    local wait_message="$2"
    local success_message="$3"
    local timeout_message="$4"
    local max_wait_time="$5"
    local poll_interval="$6"
    shift 6  # Remove first 6 args, remaining are for check function
    
    # Single check mode (poll_interval = 0)
    if [ "$poll_interval" -eq 0 ]; then
        $check_function "$@"
        return $?
    fi
    
    # Polling mode
    log_info "$wait_message" >&2
    
    local start_time=$(date +%s)
    local result=""
    
    while [ -z "$result" ]; do
        local current_time=$(date +%s)
        local elapsed=$((current_time - start_time))
        
        if [ $elapsed -gt $max_wait_time ]; then
            # Replace %d in timeout message with elapsed time if present, otherwise append
            if [[ "$timeout_message" =~ %d ]]; then
                log_warning "$(printf "$timeout_message" "$elapsed")" >&2
            else
                log_warning "${timeout_message} (waited ${elapsed}s)" >&2
            fi
            return 1
        fi
        
        # Call the check function with provided arguments
        if result=$($check_function "$@" 2>/dev/null); then
            log_success "$(printf "$success_message" "$result")" >&2
            echo "$result"
            return 0
        fi
        
        # Wait before next poll
        sleep $poll_interval
    done
    
    return 1
}

# Function to resolve pending job to build number (polls job API until resolved)
# Supports both single-check and polling modes based on parameters
# Usage: resolve_pending_job_to_build <job_name> <trigger_timestamp> [max_wait_time] [poll_interval]
#   - If poll_interval is 0: performs single check (no polling)
#   - Otherwise: polls until resolved or timeout
resolve_pending_job_to_build() {
    local job_name="$1"
    local trigger_timestamp="$2"
    local max_wait_time="${3:-300}"  # Default 5 minutes
    local poll_interval="${4:-30}"    # Default 30 seconds
    
    poll_until_success \
        "check_job_for_latest_build" \
        "Waiting for pending job ${job_name} to resolve to build number..." \
        "Job started with build number: %s" \
        "Timeout waiting for job to start (waited %ds)" \
        "$max_wait_time" \
        "$poll_interval" \
        "$job_name" \
        "$trigger_timestamp"
}

# Function to resolve queue item to build number (polls until resolved)
# Supports both single-check and polling modes based on parameters
# Usage: resolve_queue_item_to_build <job_name> <queue_item> <trigger_timestamp> [max_wait_time] [poll_interval]
#   - If poll_interval is 0: performs single check (no polling)
#   - Otherwise: polls until resolved or timeout
resolve_queue_item_to_build() {
    local job_name="$1"
    local queue_item="$2"
    local trigger_timestamp="$3"
    local max_wait_time="${4:-300}"  # Default 5 minutes
    local poll_interval="${5:-30}"    # Default 30 seconds
    
    poll_until_success \
        "check_queue_item_for_build" \
        "Waiting for queue item ${queue_item} to resolve to build number for ${job_name}..." \
        "Queue item ${queue_item} resolved to build number: %s" \
        "Timeout waiting for queue item ${queue_item} to resolve (waited %ds)" \
        "$max_wait_time" \
        "$poll_interval" \
        "$job_name" \
        "$queue_item" \
        "$trigger_timestamp"
}

# Function to escape strings for JSON output
escape_json_string() {
    local str="$1"
    # Escape backslashes, quotes, and newlines
    echo "$str" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\n/\\n/g; s/\r/\\r/g; s/\t/\\t/g'
}

# log message with worker id
function log {
  if [[ -z $mtv_worker_id ]]; then
    export mtv_worker_id="main"
  fi
  # kill runaway child process if parent is dead
  if [[ -z $(ps -e | grep $MAIN_WORKER_PID) ]]; then
    echo "Parent process exited, killing child..."
    kill -s 2 $$
  fi

  # just pretty printing
  if [[ $mtv_worker_id == "main" ]]; then
    w="[w] $mtv_worker_id"
  else
    w="[w] $mtv_worker_id"
  fi

  if [[ -n $mtv_parent_worker_id ]]; then
    if [[ $mtv_parent_worker_id == "main" ]]; then
      p="[p] $mtv_parent_worker_id"
    else
      p="[p] $mtv_parent_worker_id"
    fi
    p+=" $(date +"%T.%3N")"
    echo -e "\n┏$w $p\n$@\n"
  else
    w+=" $(date +"%T.%3N")"
    echo -e "\n┏$w\n$@\n"
  fi
}

# Color-coded logging functions
function log_info {
  echo -e "${BLUE}[INFO]${NC} $*"
}

function log_success {
  echo -e "${GREEN}[SUCCESS]${NC} $*"
}

function log_warning {
  echo -e "${YELLOW}[WARNING]${NC} $*"
}

function log_error {
  echo -e "${RED}[ERROR]${NC} $*" >&2
}

function w_output {
  echo $@ | jq | tee -a "$CMD_OUTPUT_PATH/cmd_output"
}

function r_output {
  cat "$CMD_OUTPUT_PATH/cmd_output"
}

function cl_output {
  log "Clearing output file..."
  truncate -s 0 "$CMD_OUTPUT_PATH/cmd_output"
}

# yaml to json helper func
function ytj {
  echo $@ | yq -p yaml -o json
}

# execute asynchronously, only if main process is running
function async {
  if [[ -z $(ps -e | grep $MAIN_WORKER_PID) ]]; then
    exit
  fi
  (
    export mtv_parent_worker_id=$mtv_worker_id
    export mtv_worker_id=$(uuidgen | cut -d '-' -f 1)
    $@
  ) &
}

# wait for processes to finish, is used in conjuction with async
function process_sync {
  while true; do
    if [[ -n $(jobs | grep Done) ]]; then
      break
    fi
    sleep 1
  done
}

# prepare the temporary working directory
function temp_dir {
  if [[ -n $(ls | grep "temp-$mtv_worker_id") ]]; then
    rm -rf temp-$mtv_worker_id
  fi
  mkdir temp-$mtv_worker_id
  cd temp-$mtv_worker_id
}

# remove the temp working directory
function rm_temp_dir {
  cd ..
  if [[ -n $(ls | grep "temp-$mtv_worker_id") ]]; then
    rm -rf temp-$mtv_worker_id
  fi
}

# Ensure we're logged into the given OpenShift cluster before running `oc`.
# Preference order:
#   1. Reuse an existing session already pointed at this cluster
#   2. Non-interactive login with a token from <token_var> — the exported env
#      var if present, otherwise sourced from scripts/auth.sh (standalone runs)
#   3. Interactive `oc login --web` as a last resort
# Usage: ensure_oc_login <cluster_url> [token_var_name]
function ensure_oc_login {
    local cluster_url="$1"
    local token_var="${2:-OCP_TOKEN}"

    # 1. Reuse an existing session for this exact cluster 
    if [[ "$(oc whoami --show-server 2>/dev/null)" == "$cluster_url" ]] \
        && oc whoami >/dev/null 2>&1; then
        return 0
    fi

    # 2. Token login. Prefer an already-exported env var (auth.sh sourced once
    #    up front); fall back to sourcing auth.sh here for standalone runs.
    local token="${!token_var:-}"
    if [[ -z "$token" && -f scripts/auth.sh ]]; then
        source scripts/auth.sh
        token="${!token_var:-}"
    fi

    if [[ -n "$token" ]]; then
        log_info "Logging into $cluster_url with \$$token_var"
        if oc login --token="$token" --server="$cluster_url" >/dev/null 2>&1; then
            return 0
        fi
        log_warning "Token login to $cluster_url failed; falling back to interactive login"
    fi

    # 3. Interactive fallback — only viable with a terminal. In non-interactive
    #    contexts (cron, the automatic_iib pipeline) `oc login --web` would hang
    #    forever waiting on a browser, so fail fast with a clear message instead.
    if [[ ! -t 0 ]]; then
        log_error "Not logged into $cluster_url and no usable \$$token_var; cannot prompt for interactive login in a non-interactive shell"
        exit 1
    fi
    oc login --web "$cluster_url"
}

# Function to validate required tools
function validate_tools {
    local missing_tools=()
    
    for tool in oc jq yq gh git skopeo; do
        if ! command -v "$tool" &> /dev/null; then
            missing_tools+=("$tool")
        fi
    done
    
    if [ ${#missing_tools[@]} -ne 0 ]; then
        log "ERROR: Missing required tools: ${missing_tools[*]}"
        log "Current PATH (when script ran): $PATH"
        for tool in "${missing_tools[@]}"; do
            found_in=""
            for dir in $(echo "$PATH" | tr ':' ' '); do
                [[ -z "$dir" ]] && continue
                if [[ -x "$dir/$tool" ]]; then
                    found_in="$dir/$tool"
                    break
                fi
            done
            if [[ -n "$found_in" ]]; then
                log "  $tool: 'command -v' did not find it, but executable exists at $found_in (possible shell/alias or PATH timing issue)"
            else
                log "  $tool: not found in any PATH directory"
            fi
        done
        exit 1
    fi
}
