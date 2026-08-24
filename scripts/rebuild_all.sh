#!/usr/bin/env bash
source scripts/util.sh

cluster="https://api.stone-prd-rh01.pg1f.p1.openshiftapps.com:6443"
version=$1

# Print Usage if argument is missing
if [[ -z $1 ]]; then
  echo "Usage: "
  echo "./rebuild_all.sh 2-9"
  echo "./rebuild_all.sh dev-preview"
  exit 0
fi

ensure_oc_login "$cluster" KONFLUX_SERVICE_TOKEN

oc project rh-mtv-1-tenant

oc annotate components/forklift-api-$version build.appstudio.openshift.io/request=trigger-pac-build
oc annotate components/forklift-cli-download-$version build.appstudio.openshift.io/request=trigger-pac-build
oc annotate components/forklift-console-plugin-$version build.appstudio.openshift.io/request=trigger-pac-build
oc annotate components/forklift-controller-$version build.appstudio.openshift.io/request=trigger-pac-build
oc annotate components/forklift-must-gather-$version build.appstudio.openshift.io/request=trigger-pac-build
oc annotate components/forklift-operator-$version build.appstudio.openshift.io/request=trigger-pac-build
oc annotate components/forklift-ova-proxy-$version build.appstudio.openshift.io/request=trigger-pac-build
oc annotate components/hyperv-provider-server-$version build.appstudio.openshift.io/request=trigger-pac-build
oc annotate components/openstack-populator-$version build.appstudio.openshift.io/request=trigger-pac-build
oc annotate components/ova-provider-server-$version build.appstudio.openshift.io/request=trigger-pac-build
oc annotate components/ovirt-populator-$version build.appstudio.openshift.io/request=trigger-pac-build
oc annotate components/populator-controller-$version build.appstudio.openshift.io/request=trigger-pac-build
oc annotate components/validation-$version build.appstudio.openshift.io/request=trigger-pac-build
oc annotate components/virt-v2v-rhel9-$version build.appstudio.openshift.io/request=trigger-pac-build
oc annotate components/vsphere-copy-offload-populator-$version build.appstudio.openshift.io/request=trigger-pac-build
oc annotate components/vsphere-xcopy-volume-populator-$version build.appstudio.openshift.io/request=trigger-pac-build
oc annotate components/deep-inspection-$version build.appstudio.openshift.io/request=trigger-pac-build
oc annotate components/deep-inspection-rhel9-$version build.appstudio.openshift.io/request=trigger-pac-build
scripts/rebuild_btrfs.sh $version
