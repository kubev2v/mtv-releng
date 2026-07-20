#!/usr/bin/env bash
source scripts/util.sh

# This script does all the neccessary tasks to create a new IIBs from latest released stage bundles
# New IIB is created only when there are new changes (script is idempotent)

# If filter_version is specified, only process that version
# e.g. ./automatic_iib.sh 2.9.4
filter_version=$1

# authenticate to required services
source scripts/auth.sh

### Change this once we modify the clusters (if you want to disable a OCP version then use ["4.17"]="none")
declare -A ocp_to_cluster_mappings=(
  ["4.21"]="qemtv-01"
  ["4.20"]="qemtv-02"
  # ["4.19"]="qemtv-03"
  # ["4.18"]="qemtv-04"
)

# Get shas of the bundles in registry
scripts/latest_stage_bundles.sh $filter_version
latest_shas=$(r_output)

for version in $(echo $latest_shas | jq '. | keys.[]' -r); do
  sha=$(echo $latest_shas | jq ".\"$version\"" -r)
  log "Processing $version with $sha bundle..."

  # create PR with the bundle
  scripts/create_iib_pr.sh main $version $sha true
  if (($? != 0)); then
    log "Error occured, exiting..."
    exit 1
  fi

  pr_info=$(r_output | jq ".\"$version\"")
  pr_url=$(echo $pr_info | jq ".pr_url" -r)
  ocp_vers=$(echo $pr_info | jq ".ocp_versions" -r)
  prev_version=$(echo $pr_info | jq ".prev_version" -r)
  prev_iib=$(echo $pr_info | jq ".prev_iib" -r)
  current_iib=$(echo $pr_info | jq ".current_iib" -r)
  ver_suffix=$(echo $pr_info | jq ".ver_suffix" -r)
  prev_ver_suffix=$(echo $pr_info | jq ".prev_ver_suffix" -r)

  # if url is missing, the PR was not created/updated and thus catalogs not updated
  if [[ -z $pr_url ]]; then
    log "PR for $version was not created/updated, skipping..."
    continue
  fi

  # wait little bit for konflux webhooks to pickup the new builds, if any
  sleep 10

  scripts/wait_for_pr.sh $pr_url "$ocp_vers"
  # only continue if all pipelines succeeded
  if [[ $? == 1 ]]; then
    log "Error occured, exiting..."
    exit 1
  fi

  if [[ $prev_iib == "none" ]]; then
    log "Could not find previous IIB build. Skipping diff extraction..."
    scripts/extract_info.sh $current_iib $version
  else
    scripts/extract_diff.sh $current_iib $version $prev_iib $prev_version
    echo "Get diff"
  fi
  #read -p "Modify the cmd_output, Continue?"
  iib_info=$(r_output)

  # prepare vars for sending a message
  export iib_version=$ver_suffix
  export ocp_urls="{}"
  for ocp in ${ocp_vers[@]}; do
    ocp_init_dot=$(echo $ocp_vers | cut -d ' ' -f 1)
    ocp_init_dot=${ocp_init_dot//./}
    ocp_dot=${ocp//./}
    ocp_url=${current_iib/$ocp_init_dot/$ocp_dot}
    ocp_urls=$(echo $ocp_urls | jq ". += {\"$ocp\": \"$ocp_url\"}")
  done
  export bundle_url=$(echo $iib_info | jq ".bundle_url" -r)
  export snapshot="Not implemented yet"
  export commits=$(echo $iib_info | jq ".commits")
  # if the diff was not created, send special message
  if [[ $(echo $iib_info | jq '.diffs') == "null" ]]; then
    export last_build="No previous build found, as this is the first build for $version"
    export changes='{"forklift":{},"forklift-console-plugin":{},"forklift-must-gather":{}}'
  else
    export last_build="$prev_ver_suffix $prev_iib"
    export changes=$(echo $iib_info | jq ".diffs")
  fi
  # read -p "Continue to send slack msg?"
  scripts/iib_notify.sh

  ocp=$(echo $ocp_urls | jq -r '. | keys.[]')
  ocp_ver=${ocp#*v}
  ocp_url=$(echo "$ocp_urls" | jq -r ".\"$ocp\"")
  iib_short=${ocp_url##*/}

  if [[ $version == "2.11.0" ]]; then
    scripts/jenkins_trigger.sh trigger "$iib_short" "$version" '4.21' 'false' 'qemtv-01' 'gate' 'RELEASE'
    scripts/jenkins_trigger.sh trigger "$iib_short" "$version" '4.20' 'false' 'qemtv-02' 'non-gate' 'RELEASE_NON_GATE'
  elif [[ $version == *"2.10"* ]]; then
    scripts/jenkins_trigger.sh trigger "$iib_short" "$version" '4.20' 'true' 'qemtv-02' 'gate' 'RELEASE'
    scripts/jenkins_trigger.sh trigger "$iib_short" "$version" '4.19' 'false' 'qemtv-03' 'non-gate' 'RELEASE_NON_GATE'
  fi

  # for ocp in $(echo $ocp_urls | jq -r '. | keys.[]'); do
  #   ocp_ver=${ocp#*v}
  #   ocp_url=$(echo "$ocp_urls" | jq -r ".\"$ocp\"")
  #   iib_short=${ocp_url##*/}
  #   cluster=${ocp_to_cluster_mappings[$ocp_ver]}
  #   if [[ $cluster == "" || $cluster == "none" ]]; then
  #     echo "skipping jenkins_call" "$iib_short" "$version" "$ocp_ver" 'true' "$cluster"
  #     continue
  #   else
  #     echo "jenkins_call" "$iib_short" "$version" "$ocp_ver" 'true' "$cluster"
  #     read -p "Continue to trigger jenkins testing?"
  #     scripts/jenkins_call.sh "$iib_short" "$version" "$ocp_ver" 'true' "$cluster"
  #   fi
  # done

done

log "Done"
