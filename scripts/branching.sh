# Before running the script, read it through and understand what it does.
# Also, modify the forks of the repositories to your own!

# This script is intended to help and automate tasks related to branching of MTV operator.
# It's "modular" so you can choose what steps you want to execute and what to skip.

# What exactly it does?
# - creates release-X.Y branches for forklift, UI plugin and must gather
# - modifies neccessary files with correct values
# - pushes the changes into user's fork of the repository
# - creates neccessary files for konflux with correct values
# - pushes the changes to konflux releng repo

konflux_releng="git@gitlab.cee.redhat.com:releng/konflux-release-data.git"

forklift_url="git@github.com:kubev2v/forklift.git"
forklift_console_plugin_url="git@github.com:kubev2v/forklift-console-plugin.git"
forklift_must_gather_url="git@github.com:kubev2v/forklift-must-gather.git"

# Update to your repository forks
fork_forklift_url="git@github.com:solenoci/forklift.git"
fork_forklift_console_plugin_url="git@github.com:solenoci/forklift-console-plugin.git"
fork_forklift_must_gather_url="git@github.com:solenoci/forklift-must-gather.git"

# If the script should delete the working directory at the end
cleanup="true"

# Portable in-place sed (GNU and BSD/macOS differ on -i)
if sed --version >/dev/null 2>&1; then
  sedi() { sed -i "$@"; }
else
  sedi() { sed -i '' "$@"; }
fi

# Modify release.conf file with user input values
release_conf_forklift() {
  cat <<EOF >build/release.conf
# Global version specifying version for every component and for bundle, format "x.y.z"
MTV_VERSION=$version

# Release version, format "vX.Y"
RELEASE=$release

# This setting must mirror the Y stream version specified above, e.g. if RELEASE == v2.10 then CPE == 2.10 (without the "v")
CPE=${version%.*}

# Operator channel where the version will be deployed, e.g. dev-preview, release-v2.9 ...
CHANNEL=$channel

# Default operator channel for other operators to pull from, if they depend on MTV
DEFAULT_CHANNEL=$def_channel

# Registry where all components should be released to, for dev-preview -> mtv-candidate, for release-X.Y -> migration-toolkit-virtualization
REGISTRY=$registry

# Which OCP versions are supported by this release
OCP_VERSIONS=$ocp_versions
EOF
}

# Modify release.conf file with user input values
release_conf_console_plugin() {
  cat <<EOF >build/release.conf
# Global version specifying version for every component and for bundle, format "x.y.z"
RVERSION=$version

# Release version, format "vX.Y"
RELEASE=$release

# This setting must mirror the Y stream version specified above, e.g. if RELEASE == v2.10 then CPE == 2.10 (without the "v")
CPE=${version%.*}

# Operator channel where the version will be deployed, e.g. dev-preview, release-v2.9 ...
CHANNEL=$channel

# Default operator channel for other operators to pull from, if they depend on MTV
DEFAULT_CHANNEL=$def_channel

# Registry where all components should be released to, for dev-preview -> mtv-candidate, for release-X.Y -> migration-toolkit-virtualization
REGISTRY=$registry
EOF
}

# Modify release.conf file with user input values
release_conf_must_gather() {
  cat <<EOF >build/release.conf
# Global version specifying version for every component and for bundle, format "x.y.z"
VERSION=$version

# Release version, format "vX.Y"
RELEASE=$release

# This setting must mirror the Y stream version specified above, e.g. if RELEASE == v2.10 then CPE == 2.10 (without the "v")
CPE=${version%.*}

# Operator channel where the version will be deployed, e.g. dev-preview, release-v2.9 ...
CHANNEL=$channel

# Default operator channel for other operators to pull from, if they depend on MTV
DEFAULT_CHANNEL=$def_channel

# Registry where all components should be released to, for dev-preview -> mtv-candidate, for release-X.Y -> migration-toolkit-virtualization
REGISTRY=$registry
EOF
}

# Process git repository and modify needed things
process_repo() {
  # Create a release branch, example: release-2.9
  git checkout -b release-${version%.*}
  echo "Will create a new release branch from main and push it to remote without any changes..."
  read -p "Continue? (Y/N): " confirm
  if [[ $confirm == [yY] || $confirm == [yY][eE][sS] ]]; then
    git push origin release-${version%.*}
    echo "release-${version%.*} branch was created in remote repository..."
  else
    echo "Skipped push to remote repository, you can still create the branch manually..."
  fi

  git checkout -b CF-$version

  # Modify release.conf file
  release_conf=$1
  $release_conf

  # Rename all files in .tekton/ to include the version instead of dev-preview, example: virt-v2v-dev-preview-on-push.yaml -> virt-v2v-2-9-on-push.yaml
  for i in .tekton/*; do
    mv "$i" "$(echo $i | sed "s/dev-preview/$version_name/")"
  done

  # The "pipelinesascode.tekton.dev/on-cel-expression" annotation in .tekton/ files should be adjusted to specify and filter by the right branch name, example: main -> release-2.9
  sedi -e "s/\"main\"/\"release-${version%.*}\"/g" .tekton/*

  # The appstudio.openshift.io/application and appstudio.openshift.io/component labels in .tekton/ files must be adjusted to specify the right Application and Component respectively. Failing to do this will cause builds of the pipeline to be associated with the wrong application or component. example: forklift-operator-dev-preview -> forklift-operator-2-9
  sedi -e "s/dev-preview/$version_name/g" .tekton/*

  if [[ "$release_conf" == "forklift" ]]; then
    sedi -e "s/dev-preview/$version_name/g" build/forklift-operator-bundle/images.conf
    git add build/forklift-operator-bundle/images.conf
  fi
  git add .tekton/ build/release.conf
  git commit -sm "Code freeze for ${version%.*}"

  read -p "Do you want to see what changes were made? (Y/N): " confirm
  if [[ $confirm == [yY] || $confirm == [yY][eE][sS] ]]; then
    git show $(git rev-parse HEAD)
  fi

  echo "Will push the changes to your fork's CF-$version branch..."
  read -p "Continue? (Y/N): " confirm
  if [[ $confirm == [yY] || $confirm == [yY][eE][sS] ]]; then
    git push fork CF-$version
    echo "You can now create a PR from the 'CF-$version' into release-${version%.*} branch"
  else
    echo "Skipped push to remote repository, you can still push manually from working dir..."
  fi
}

mkdir tmp
tmp_dir="$(pwd)/tmp"
echo "Using '$tmp_dir' directory as working directory..."
cd $tmp_dir

echo -e "\n### To continue, specify release parameters asked... ###\n"

echo "Global version, specifying version for every component and for bundle, format: x.y.z, example: 2.9.0"
read -p "VERSION: " version && [[ -n $version ]] || exit 1
echo ""

echo "Release version, usually an abbreviation of version, format: vX.Y, example: v2.9"
read -p "RELEASE: " release && [[ -n $release ]] || exit 1
echo ""

echo "Operator channel where this version should be deployed, example: release-v2.9"
read -p "CHANNEL: " channel && [[ -n $channel ]] || exit 1
echo ""

echo "Default operator channel for other operators to pull from, if they depend on MTV, example: release-v2.9"
read -p "DEFAULT_CHANNEL: " def_channel && [[ -n $def_channel ]] || exit 1
echo ""

echo "Registry where all components should be released to, for dev-preview use mtv-candidate, for release-X.Y use migration-toolkit-virtualization"
read -p "REGISTRY: " registry && [[ -n $registry ]] || exit 1
echo ""

echo "Which OCP versions are supported by this release, example: v4.17-v4.19"
read -p "OCP_VERSIONS: " ocp_versions && [[ -n $ocp_versions ]] || exit 1
echo ""

echo -e "### Please check the input data if it's correct before proceeding ###\n"

echo "VERSION: $version"
echo "RELEASE: $release"
echo "CHANNEL: $channel"
echo "DEFAULT_CHANNEL: $def_channel"
echo "REGISTRY: $registry"
echo "OCP_VERSIONS: $ocp_versions"

read -p "Continue? (Y/N): " confirm && [[ $confirm == [yY] || $confirm == [yY][eE][sS] ]] || exit 1

version_name=${version/./-}
version_name=${version_name%.*}

### MTV side ###

read -p "Start branching of forklift? (Y/N): " confirm
if [[ $confirm == [yY] || $confirm == [yY][eE][sS] ]]; then
  git clone $forklift_url
  cd forklift
  git remote add fork $fork_forklift_url
  process_repo release_conf_forklift
  cd ..
fi

read -p "Start branching of forklift-console-plugin? (Y/N): " confirm
if [[ $confirm == [yY] || $confirm == [yY][eE][sS] ]]; then
  git clone $forklift_console_plugin_url
  cd forklift-console-plugin
  git remote add fork $fork_forklift_console_plugin_url
  process_repo release_conf_console_plugin
  cd ..
fi

read -p "Start branching of forklift-must-gather? (Y/N): " confirm
if [[ $confirm == [yY] || $confirm == [yY][eE][sS] ]]; then
  git clone $forklift_must_gather_url
  cd forklift-must-gather
  git remote add fork $fork_forklift_must_gather_url
  process_repo release_conf_must_gather
  cd ..
fi

### Konflux side ###

read -p "Start configuring konflux? (Y/N): " confirm
if [[ $confirm != [yY] && $confirm != [yY][eE][sS] ]]; then
  # Clean up
  echo "Clean up (remove) working tmp dir ..."
  read -p "Continue? (Y/N): " confirm
  if [[ $confirm == [yY] || $confirm == [yY][eE][sS] ]]; then
    rm -rf $tmp_dir
  fi
  exit 0
fi

git clone $konflux_releng --depth=1
cd konflux-release-data
git checkout -b mtv_add_stream

# Prod tenant (rh-mtv-1) now uses one rendered manifest per version under
# operator/streams/, snapshotted from main (operator/dev-preview.yaml) so it picks up
# any component changes since the last release. Transform dev-preview -> this version,
# retarget components to the release branch, add the configure-pac-no-mr annotation
# (Components only) release streams carry, then register it. Old template left as-is.
prod_operator_dir="tenants-config/cluster/stone-prd-rh01/tenants/rh-mtv-1-tenant/operator"
cp "$prod_operator_dir/dev-preview.yaml" "$prod_operator_dir/streams/$version_name.yaml"
sedi -e "s/dev-preview/$version_name/g" \
     -e "s/revision: \"main\"/revision: \"release-${version%.*}\"/g" \
     -e "s#\(build.appstudio.openshift.io/pipeline: .*\)#\1\\"$'\n'"    build.appstudio.openshift.io/request: configure-pac-no-mr#" \
     "$prod_operator_dir/streams/$version_name.yaml"
echo "  - $version_name.yaml" >>"$prod_operator_dir/streams/kustomization.yaml"

# Standalone *-bundle / *-api ReleasePlans + ReleasePlanAdmissions are manual one-offs.
# To automate later:
#   prev=<previous version_name, e.g. 2-12>
#   for flavour in bundle api; do
#     # rp lives in the tenant dir, rpa in config/.../ReleasePlanAdmission/rh-mtv-1
#     cp forklift-operator-rp-prod-$prev-$flavour.yaml   forklift-operator-rp-prod-$version_name-$flavour.yaml
#     cp forklift-operator-rpa-prod-$prev-$flavour.yaml  forklift-operator-rpa-prod-$version_name-$flavour.yaml
#     sedi -e "s/$prev/$version_name/g" forklift-operator-r*-prod-$version_name-$flavour.yaml
#     echo "  - forklift-operator-rp-prod-$version_name-$flavour.yaml" >> <tenant>/kustomization.yaml
#   done

cat <<EOF >>tenants-config/cluster/stone-prod-p02/tenants/rh-mtv-btrfs-tenant/streams.yaml
---
apiVersion: projctl.konflux.dev/v1beta1
kind: ProjectDevelopmentStream
metadata:
  name: forklift-operator-int-pds-$version_name
  namespace: rh-mtv-btrfs-tenant
spec:
  project: forklift-operator-int-project
  template:
    name: forklift-operator-int-template
    values:
      - name: versionName
        value: "$version_name"
      - name: revision
        value: "release-${version%.*}"
EOF

# Go to our tenant
cd config/stone-prd-rh01.pg1f.p1/product/ReleasePlanAdmission/rh-mtv-1

# Update registry and replace dev-preview for version
for i in forklift-operator-rpa-stage-dev-preview-*; do
  cp "$i" "$(echo $i | sed "s/dev-preview/$version_name/")"
done
sedi -e "s/dev-preview/$version_name/g" forklift-operator-rpa-stage-$version_name-*
sedi -e "s/mtv-candidate/$registry/g" forklift-operator-rpa-stage-$version_name-*

# Update registry and replace dev-preview for version
for i in forklift-operator-rpa-prod-dev-preview-*; do
  cp "$i" "$(echo $i | sed "s/dev-preview/$version_name/")"
done
sedi -e "s/dev-preview/$version_name/g" forklift-operator-rpa-prod-$version_name-*
sedi -e "s/mtv-candidate/$registry/g" forklift-operator-rpa-prod-$version_name-*

# Update product_version in stage RPAs
for i in forklift-operator-rpa-stage-$version_name-*; do
  sed "/product_version/s/      product_version: \"[0-9][0-9]*\.[0-9][0-9]*\"/      product_version: \"${version%.*}\"/" $i >$i-replace
  rm $i
  mv $i-replace $i
done

# Update product_version in prod RPAs
for i in forklift-operator-rpa-prod-$version_name-*; do
  sed "/product_version/s/      product_version: \"[0-9][0-9]*\.[0-9][0-9]*\"/      product_version: \"${version%.*}\"/" $i >$i-replace
  rm $i
  mv $i-replace $i
done

# Return to the root of the repo
cd $tmp_dir/konflux-release-data

# Go to our tenant
cd config/stone-prod-p02.hjvn.p1/product/ReleasePlanAdmission/rh-mtv-btrfs

# Update registry and replace dev-preview for version
for i in forklift-operator-int-rpa-stage-dev-preview-*; do
  cp "$i" "$(echo $i | sed "s/dev-preview/$version_name/")"
done
sedi -e "s/dev-preview/$version_name/g" forklift-operator-int-rpa-stage-$version_name-*
sedi -e "s/mtv-candidate/$registry/g" forklift-operator-int-rpa-stage-$version_name-*

# Update registry and replace dev-preview for version
for i in forklift-operator-int-rpa-prod-dev-preview-*; do
  cp "$i" "$(echo $i | sed "s/dev-preview/$version_name/")"
done
sedi -e "s/dev-preview/$version_name/g" forklift-operator-int-rpa-prod-$version_name-*
sedi -e "s/mtv-candidate/$registry/g" forklift-operator-int-rpa-prod-$version_name-*

# Update product_version in stage RPAs
for i in forklift-operator-int-rpa-stage-$version_name-*; do
  sed "/product_version/s/      product_version: \"[0-9][0-9]*\.[0-9][0-9]*\"/      product_version: \"${version%.*}\"/" $i >$i-replace
  rm $i
  mv $i-replace $i
done

# Update product_version in prod RPAs
for i in forklift-operator-int-rpa-prod-$version_name-*; do
  sed "/product_version/s/      product_version: \"[0-9][0-9]*\.[0-9][0-9]*\"/      product_version: \"${version%.*}\"/" $i >$i-replace
  rm $i
  mv $i-replace $i
done

# Return to the root of the repo
cd $tmp_dir/konflux-release-data

# Build MTV manifests
tenants-config/build-single.sh rh-mtv-1

# Build MTV manifests
tenants-config/build-single.sh rh-mtv-btrfs

# Run tests
echo "Running tox tests. They are required to pass if you want to be able to merge anything into the konflux repo, but they take around 5 minutes to run."
read -p "Do you want to run tox? (Y/N): " confirm
if [[ $confirm == [yY] || $confirm == [yY][eE][sS] ]]; then
  tox
  # Check if tests failed
  if [[ $? != 0 ]]; then
    echo "'tox' failed. Fix the issues and then create a PR manually."
    echo "You can find the repo and changes at: $(pwd)"
    exit 1
  fi
fi

git add config/ tenants-config/
git commit -sm "MTV: Add new PDS for ${version%.*}"

read -p "Do you want to see what changes were made? (Y/N): " confirm
if [[ $confirm == [yY] || $confirm == [yY][eE][sS] ]]; then
  git show $(git rev-parse HEAD)
fi

echo "Will push the changes to origin's 'mtv_add_stream' branch..."
read -p "Continue? (Y/N): " confirm
if [[ $confirm == [yY] || $confirm == [yY][eE][sS] ]]; then
  git push origin mtv_add_stream
  echo "You can now create a PR from the 'mtv_add_stream' into main branch..."
else
  echo "Skipped push to remote repository, you can still push manually from working dir..."
fi

# Clean up
echo "Removing working tmp dir ..."
read -p "Continue? (Y/N): " confirm
if [[ $confirm == [yY] || $confirm == [yY][eE][sS] ]]; then
  rm -rf $tmp_dir
fi
