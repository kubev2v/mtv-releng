#!/usr/bin/env bash
source scripts/util.sh

# Get commit history from the specified IIB

iib_url=$1
version=$2

# Print Usage if IIB URL is missing
if [[ -z $1 || -z $2 ]]; then
  echo "Usage: ./extract_info.sh <url to iib> <version>, examples:"
  echo "./extract_info.sh quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-fbc-prod-v418:on-pr-76657e65fa4e6ff445965976200aed1ad7adbb7d 2.9.0"
  echo "./extract_info.sh registry-proxy.engineering.redhat.com/rh-osbs/iib:985689 2.8.5"
  echo "./extract_info.sh registry.redhat.io/redhat/redhat-operator-index:v4.18 2.8.5"
  exit 0
fi

# get bundle image url present in the IIB
scripts/iib.sh $iib_url $version
bundle_img=$(r_output | jq '.BUNDLE_IMAGE' -r)
log_success "# Bundle image extracted from IIB #"
log_info "$bundle_img"

# keep original URL
bundle_original_url=$bundle_img

# try to replace registry with quay
bundle_img=$(scripts/replace_for_quay.sh $bundle_img $version)

# get components from bundle
scripts/bundle.sh $bundle_img
cmps=$(r_output | jq '.' -r)

# component origins
origins='{"mtv-controller-rhel9": "forklift", "mtv-must-gather-rhel9": "forklift-must-gather", "mtv-validation-rhel9":"forklift", "mtv-api-rhel9":"forklift", "mtv-populator-controller-rhel9":"forklift", "mtv-rhv-populator-rhel8":"forklift", "mtv-virt-v2v-rhel9":"forklift", "mtv-openstack-populator-rhel9":"forklift", "mtv-console-plugin-rhel9":"forklift-console-plugin", "mtv-ova-provider-server-rhel9":"forklift", "mtv-vsphere-copy-offload-populator-rhel9":"forklift", "mtv-vsphere-xcopy-volume-populator-rhel9":"forklift", "mtv-rhel9-operator":"forklift", "mtv-operator-bundle": "forklift", "mtv-cli-download-rhel9": "forklift", "mtv-ova-proxy-rhel9": "forklift", "mtv-virt-v2v-rhel10": "forklift", "mtv-deep-inspection-rhel10": "forklift", "mtv-deep-inspection-rhel9": "forklift", "mtv-hyperv-provider-server-rhel9": "forklift", "mtv-copy-appliance-builder-rhel9": "forklift", "mtv-copy-appliance-nbd-container-rhel10": "forklift", "mtv-copy-appliance-nbd-container-rhel9": "forklift"}'

commits="[]"

# Commits from which components are built
# for each component image do commit extraction
for cmp_name in $(echo $cmps | jq '. | keys.[]' -r); do
  cmp=$(echo $cmps | jq ".$cmp_name" -r)
  # ^ registry.redhat.io/migration-toolkit-virtualization/mtv-controller-rhel9@sha256:5957554...

  # get only the image name
  img_sha=${cmp##*/}  # result: mtv-controller-rhel9@sha256:5957554....
  img=${img_sha%%@*}  # result: mtv-controller-rhel9
  sha=${img_sha%%*\:} # result: 5957554...

  # pair the prod cmp name with quay cmp name
  origin=$(echo $origins | jq ".\"$img\"" -r)

  # try to replace for quay
  cmp=$(scripts/replace_for_quay.sh $cmp $version)

  # get commit from component image
  scripts/component.sh $cmp
  commit=$(r_output | jq '.COMMIT' -r)

  commits=$(echo $commits | jq ".+=[{\"cmp\":\"$cmp_name\",\"commit\":\"$commit\",\"origin\":\"$origin\"}]")
done

# also get bundle commit
scripts/component.sh $bundle_img
commit=$(r_output | jq '.[]' -r)

# aggregate commits per origin
declare -A by_origin

# clear cmd output file
cl_output

construct_json="{\"commits\":{\"BUNDLE_IMAGE\":\"$commit\"},\"latest_commits\":{}, \"bundle_url\": \"$bundle_original_url\"}"

for commit in $(echo $commits | jq '.[]' -rc); do
  # extract origins and shas
  origin=$(echo $commit | jq '.origin' -r)
  sha=$(echo $commit | jq '.commit' -r)
  cmp=$(echo $commit | jq '.cmp' -r)

  construct_json=$(echo $construct_json | jq ".commits += {\"$cmp\":\"$sha\"}")
  log_info $cmp": "$sha

  if ! [[ ${by_origin[$origin]} == *"$sha"* ]]; then
    # group by origin
    by_origin[$origin]+="$sha "
  fi
done

if [[ -n $(ls | grep temp) ]]; then
  rm -rf temp
fi
mkdir temp
cd temp

# Latest commits per origin
for origin in ${!by_origin[@]}; do
  git init $origin
  cd $origin
  # add origin fo the repo
  if [[ -n $(git remote -v | grep origin) ]]; then
    git remote remove origin
  fi

  git remote add origin https://github.com/kubev2v/$origin.git

  xy=$(echo $version | cut -d '.' -f 1).$(echo $version | cut -d '.' -f 2)

  if [[ $(curl -s https://raw.githubusercontent.com/kubev2v/$origin/refs/heads/release-$xy/build/release.conf) == *"404"* ]]; then
    branch=main
  else
    branch=release-$xy
  fi

  git fetch origin $branch

  # 200 = number of commits
  history=$(git --no-pager log --remotes --format=format:'%H%n' -n 200 origin/$branch)

  readarray -t history_commits <<<$history

  # get the latest commits per origin
  latest_commit=""
  latest_commit_id=1000000

  commits=${by_origin[$origin]}
  commits=$(echo -e "${commits// /\\n}")
  for commit in ${commits[@]}; do
    declare -i idx=0
    for hist_commit in ${history_commits[@]}; do
      if [[ $commit == $hist_commit ]]; then
        if [[ $idx -lt $latest_commit_id ]]; then
          latest_commit=$hist_commit
          latest_commit_id=$idx
          break
        fi
      fi
      idx+=1
    done
  done

  construct_json=$(echo $construct_json | jq ".latest_commits += $(ytj $origin: $latest_commit)")
  cd ..
done

log_success "# Build info from IIB #"
w_output $(echo $construct_json | jq '.')

cd ..
rm -rf temp
