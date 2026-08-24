#!/usr/bin/env bash
source scripts/util.sh

cluster="https://api.stone-prod-p02.hjvn.p1.openshiftapps.com:6443"
version=$1

# Print Usage if argument is missing
if [[ -z $1 ]]; then
  echo "Usage: "
  echo "./rebuild_btrfs.sh 2-9"
  echo "./rebuild_btrfs.sh dev-preview"
  exit 0
fi

ensure_oc_login "$cluster" KONFLUX_BTRFS_TOKEN

oc project rh-mtv-btrfs-tenant
oc annotate components/virt-v2v-int-$version build.appstudio.openshift.io/request=trigger-pac-build
