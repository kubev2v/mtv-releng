#!/usr/bin/env bash

# Applies IDMS for testing the MTV in a cluster before release

version=$1

# Print Usage if argument is missing
if [[ -z $1 ]]; then
  echo "Usage: ./create_devel_idms.sh <version>, examples:"
  echo "./create_devel_idms.sh 2-9"
  echo "./create_devel_idms.sh dev-preview"
  exit 0
fi

repo="migration-toolkit-virtualization"
if [[ $version == "dev-preview" ]]; then
  repo="mtv-candidate"
fi

cat <<EOF >devel-idms.yaml
apiVersion: config.openshift.io/v1
kind: ImageDigestMirrorSet
metadata:
  name: devel-testing-for-${version}
spec:
  imageDigestMirrors:
    - mirrors:
        - registry.stage.redhat.io/${repo}/mtv-controller-rhel9
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/forklift-controller-${version}
      source: registry.redhat.io/${repo}/mtv-controller-rhel9
    - mirrors:
        - registry.stage.redhat.io/${repo}/mtv-cli-download-rhel9
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/forklift-cli-download-${version}
      source: registry.redhat.io/${repo}/mtv-cli-download-rhel9
    - mirrors:
        - registry.stage.redhat.io/${repo}/mtv-api-rhel9
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/forklift-api-${version}
      source: registry.redhat.io/${repo}/mtv-api-rhel9
    - mirrors:
        - registry.stage.redhat.io/${repo}/mtv-must-gather-rhel9
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/forklift-must-gather-${version}
      source: registry.redhat.io/${repo}/mtv-must-gather-rhel9
    - mirrors:
        - registry.stage.redhat.io/${repo}/mtv-console-plugin-rhel9
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/forklift-console-plugin-${version}
      source: registry.redhat.io/${repo}/mtv-console-plugin-rhel9
    - mirrors:
        - registry.stage.redhat.io/${repo}/mtv-hyperv-provider-server-rhel9
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/hyperv-provider-server-${version}
      source: registry.redhat.io/${repo}/mtv-hyperv-provider-server-rhel9
    - mirrors:
        - registry.stage.redhat.io/${repo}/mtv-validation-rhel9
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/validation-${version}
      source: registry.redhat.io/${repo}/mtv-validation-rhel9
    - mirrors:
        - registry.stage.redhat.io/${repo}/mtv-virt-v2v-rhel10
        - quay.io/redhat-user-workloads/rh-mtv-btrfs-tenant/forklift-operator-int-${version}/virt-v2v-int-${version}
      source: registry.redhat.io/${repo}/mtv-virt-v2v-rhel10
    - mirrors:
        - registry.stage.redhat.io/${repo}/mtv-virt-v2v-rhel9
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/virt-v2v-${version}
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/virt-v2v-rhel9-${version}
      source: registry.redhat.io/${repo}/mtv-virt-v2v-rhel9
    - mirrors:
        - registry.stage.redhat.io/${repo}/mtv-populator-controller-rhel9
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/populator-controller-${version}
      source: registry.redhat.io/${repo}/mtv-populator-controller-rhel9
    - mirrors:
        - registry.stage.redhat.io/${repo}/mtv-rhv-populator-rhel8
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/ovirt-populator-${version}
      source: registry.redhat.io/${repo}/mtv-rhv-populator-rhel8
    - mirrors:
        - registry.stage.redhat.io/${repo}/mtv-openstack-populator-rhel9
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/openstack-populator-${version}
      source: registry.redhat.io/${repo}/mtv-openstack-populator-rhel9
    - mirrors:
        - registry.stage.redhat.io/${repo}/mtv-ova-provider-server-rhel9
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/ova-provider-server-${version}
      source: registry.redhat.io/${repo}/mtv-ova-provider-server-rhel9
    - mirrors:
        - registry.stage.redhat.io/${repo}/mtv-vsphere-copy-offload-populator-rhel9
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/vsphere-copy-offload-populator-${version}
      source: registry.redhat.io/${repo}/mtv-vsphere-copy-offload-populator-rhel9
    - mirrors:
        - registry.stage.redhat.io/${repo}/mtv-vsphere-xcopy-volume-populator-rhel9
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/vsphere-xcopy-volume-populator-${version}
      source: registry.redhat.io/${repo}/mtv-vsphere-xcopy-volume-populator-rhel9
    - mirrors:
        - registry.stage.redhat.io/${repo}/mtv-ova-proxy-rhel9
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/forklift-ova-proxy-${version}
      source: registry.redhat.io/${repo}/mtv-ova-proxy-rhel9
    - mirrors:
        - registry.stage.redhat.io/${repo}/mtv-rhel9-operator
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/forklift-operator-${version}
      source: registry.redhat.io/${repo}/mtv-rhel9-operator
    - mirrors:
        - registry.stage.redhat.io/${repo}/mtv-operator-bundle
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/forklift-operator-bundle-${version}
      source: registry.redhat.io/${repo}/mtv-operator-bundle
    - mirrors:
        - registry.stage.redhat.io/${repo}/mtv-deep-inspection-rhel10
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/deep-inspection-${version}
      source: registry.redhat.io/${repo}/mtv-deep-inspection-rhel10
    - mirrors:
        - registry.stage.redhat.io/${repo}/mtv-deep-inspection-rhel9
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/deep-inspection-rhel9-${version}
      source: registry.redhat.io/${repo}/mtv-deep-inspection-rhel9
    - mirrors:
        - registry.stage.redhat.io/${repo}/mtv-copy-appliance-builder-rhel9
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/copy-appliance-builder-${version}
      source: registry.redhat.io/${repo}/mtv-copy-appliance-builder-rhel9
    - mirrors:
        - registry.stage.redhat.io/${repo}/mtv-copy-appliance-nbd-container-rhel10
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/copy-appliance-nbd-container-${version}
      source: registry.redhat.io/${repo}/mtv-copy-appliance-nbd-container-rhel10
    - mirrors:
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/forklift-controller-${version}
      source: registry.stage.redhat.io/${repo}/mtv-controller-rhel9
    - mirrors:
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/forklift-cli-download-${version}
      source: registry.stage.redhat.io/${repo}/mtv-cli-download-rhel9
    - mirrors:
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/forklift-api-${version}
      source: registry.stage.redhat.io/${repo}/mtv-api-rhel9
    - mirrors:
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/forklift-must-gather-${version}
      source: registry.stage.redhat.io/${repo}/mtv-must-gather-rhel9
    - mirrors:
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/forklift-console-plugin-${version}
      source: registry.stage.redhat.io/${repo}/mtv-console-plugin-rhel9
    - mirrors:
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/hyperv-provider-server-${version}
      source: registry.stage.redhat.io/${repo}/mtv-hyperv-provider-server-rhel9
    - mirrors:
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/validation-${version}
      source: registry.stage.redhat.io/${repo}/mtv-validation-rhel9
    - mirrors:
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/virt-v2v-${version}
      source: registry.stage.redhat.io/${repo}/mtv-virt-v2v-rhel9
    - mirrors:
        - quay.io/redhat-user-workloads/rh-mtv-btrfs-tenant/forklift-operator-int-${version}/virt-v2v-int-${version}
      source: registry.stage.redhat.io/${repo}/mtv-virt-v2v-rhel10
    - mirrors:
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/populator-controller-${version}
      source: registry.stage.redhat.io/${repo}/mtv-populator-controller-rhel9
    - mirrors:
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/ovirt-populator-${version}
      source: registry.stage.redhat.io/${repo}/mtv-rhv-populator-rhel8
    - mirrors:
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/openstack-populator-${version}
      source: registry.stage.redhat.io/${repo}/mtv-openstack-populator-rhel9
    - mirrors:
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/ova-provider-server-${version}
      source: registry.stage.redhat.io/${repo}/mtv-ova-provider-server-rhel9
    - mirrors:
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/vsphere-copy-offload-populator-${version}
      source: registry.stage.redhat.io/${repo}/mtv-vsphere-copy-offload-populator-rhel9
    - mirrors:
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/vsphere-xcopy-volume-populator-${version}
      source: registry.stage.redhat.io/${repo}/mtv-vsphere-xcopy-volume-populator-rhel9
    - mirrors:
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/forklift-operator-${version}
      source: registry.stage.redhat.io/${repo}/mtv-rhel9-operator
    - mirrors:
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/forklift-operator-bundle-${version}
      source: registry.stage.redhat.io/${repo}/mtv-operator-bundle
    - mirrors:
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/forklift-ova-proxy-${version}
      source: registry.stage.redhat.io/${repo}/mtv-ova-proxy-rhel9
    - mirrors:
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/deep-inspection-${version}
      source: registry.stage.redhat.io/${repo}/mtv-deep-inspection-rhel10
    - mirrors:
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/deep-inspection-rhel9-${version}
      source: registry.stage.redhat.io/${repo}/mtv-deep-inspection-rhel9
    - mirrors:
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/copy-appliance-builder-${version}
      source: registry.stage.redhat.io/${repo}/mtv-copy-appliance-builder-rhel9
    - mirrors:
        - quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-${version}/copy-appliance-nbd-container-${version}
      source: registry.stage.redhat.io/${repo}/mtv-copy-appliance-nbd-container-rhel10

EOF

oc apply -f devel-idms.yaml
rm devel-idms.yaml
