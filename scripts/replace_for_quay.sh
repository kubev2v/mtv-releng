#!/usr/bin/env bash
source scripts/util.sh

# Replaces prod and stage image urls with quay urls

img=$1
version=$2

# Print Usage if img URL or version is missing
if [[ -z $1 || -z $2 ]]; then
  echo "Usage: ./replace_for_quay.sh <url to img> <version>, examples:"
  echo "./replace_for_quay.sh registry.stage.redhat.io/migration-toolkit-virtualization/mtv-operator-bundle@sha256:165142389fd7af082eea8873f24317199462376f2b600c871c1e30b63fef0295 2.9.1"
  echo "./replace_for_quay.sh registry.redhat.io/mtv-candidate/mtv-operator-bundle@sha256:165142389fd7af082eea8873f24317199462376f2b600c871c1e30b63fef0295 2.10.0"
  echo "./replace_for_quay.sh registry.redhat.io/migration-toolkit-virtualization/mtv-operator-bundle@sha256:165142389fd7af082eea8873f24317199462376f2b600c871c1e30b63fef0295 2.8.6"
  exit 0
fi

# if image is not from redhat.io or stage.redhat.io exit
if ! [[ "$img" == *"redhat.io"* ]]; then
  echo $img
  exit 0
fi

# check version, quay images are only supported after 2.8.6 (including)
if [ "$(printf '%s\n' \"$version\" \"2.8.5\" | sort -rV | head -n 1)" == "2.8.5" ]; then
  echo $img
  exit 0
fi

cmps='{"mtv-controller-rhel9": "forklift-controller", "mtv-must-gather-rhel9": "forklift-must-gather", "mtv-validation-rhel9":"validation", "mtv-api-rhel9":"forklift-api", "mtv-populator-controller-rhel9":"populator-controller", "mtv-rhv-populator-rhel8":"ovirt-populator", "mtv-virt-v2v-rhel9":"virt-v2v-rhel9", "mtv-openstack-populator-rhel9":"openstack-populator", "mtv-console-plugin-rhel9":"forklift-console-plugin", "mtv-ova-provider-server-rhel9":"ova-provider-server", "mtv-vsphere-copy-offload-populator-rhel9":"vsphere-copy-offload-populator", "mtv-vsphere-xcopy-volume-populator-rhel9":"vsphere-xcopy-volume-populator", "mtv-rhel9-operator":"forklift-operator", "mtv-operator-bundle": "forklift-operator-bundle", "mtv-cli-download-rhel9": "forklift-cli-download", "mtv-ova-proxy-rhel9": "forklift-ova-proxy", "mtv-virt-v2v-rhel10": "virt-v2v", "mtv-deep-inspection-rhel10": "deep-inspection", "mtv-deep-inspection-rhel9": "deep-inspection-rhel9", "mtv-hyperv-provider-server-rhel9": "hyperv-provider-server", "mtv-copy-appliance-builder-rhel9": "copy-appliance-builder", "mtv-copy-appliance-nbd-container-rhel10": "copy-appliance-nbd-container"}'

# get only the image name
img_sha=${img##*/}
repo=${img#*/}
repo=${repo%%/*}

# Check if the image has sha or just tag
if [[ "$img_sha" == *"@"* ]]; then
  cmp=${img_sha%%@*}
  sha="@sha256:${img_sha##*\:}"
else
  cmp=${img_sha%%\:*}
  sha=":${img_sha##*\:}"
fi

# save original component name before mapping
orig_cmp=$cmp

# pair the prod cmp name with quay cmp name
cmp=$(echo $cmps | jq ".\"$cmp\"" -r)

# for mtv-candidate repo, the version is always dev-preview
if [[ $repo == "mtv-candidate" ]]; then
  cmp_ver="dev-preview"
else
  # get only x-y from x.y.z version and replace dot for dash
  cmp_ver=${version%.*}
  cmp_ver=${cmp_ver/./-}
fi

# Use different registry and format for virt-v2v rhel10 component
if [[ "$orig_cmp" == "mtv-virt-v2v-rhel10" ]]; then
  registry="quay.io/redhat-user-workloads/rh-mtv-btrfs-tenant"
  # Calculate version format for rhel10 virt-v2v (int-prefixed)
  if [[ $repo == "mtv-candidate" ]]; then
    int_ver="int-dev-preview"
  else
    int_ver="int-"$cmp_ver
  fi
  echo $registry"/forklift-operator-"$int_ver"/virt-v2v-"$int_ver$sha
else
  registry="quay.io/redhat-user-workloads/rh-mtv-1-tenant"
  echo $registry"/"forklift-operator-$cmp_ver"/"$cmp"-"$cmp_ver$sha
fi
