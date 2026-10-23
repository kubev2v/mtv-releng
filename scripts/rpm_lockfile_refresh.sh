#!/usr/bin/env bash
#
# Regenerate redhat.repo + rpms.lock.yaml for components whose base image
# changed (e.g. after a dep_bump PR). Automates the "Instructional Guideline
# for Running RPM Update Scripts" steps, one podman container (and one RHSM
# registration) per group of components sharing a solver image.
#
# Run on an x86_64 VM, from anywhere:
#
#   REGISTRY_AUTH_USER='11009103|migration-toolkit-virtualization' \
#   REGISTRY_AUTH_PASS=... \
#     scripts/rpm_lockfile_refresh.sh /path/to/checkout [component-dir ...]
#
# component-dir is the directory holding rpms.in.yaml, relative to the
# checkout (".konflux/forklift-api", ".konflux/virt-v2v/appliance", "." for
# must-gather). Without any, every rpms.in.yaml in the checkout is refreshed.
#
# Optional env:
#   RLP_VERSION     rpm-lockfile-prototype release (default 0.18.0)
#   ACTIVATION_KEY  activation key prefix (default mtv-konflux-rhel; the RHEL
#                   major is appended)
#   RHSM_ORG        subscription org (default 11009103)
#   FORCE_ARCH=1    run on a non-x86_64 host anyway (emulated, slow)
#   NO_RETRY=1      don't retry failed components after the cooldown
#
# Use the shared MTV registry service account, not personal credentials. The
# credentials are passed to the containers through the environment only.
# Nothing is committed: review `git status` and commit the results yourself.

set -euo pipefail

RLP_VERSION="${RLP_VERSION:-0.18.0}"
ACTIVATION_KEY="${ACTIVATION_KEY:-mtv-konflux-rhel}"
RHSM_ORG="${RHSM_ORG:-11009103}"
RLP_URL="https://github.com/konflux-ci/rpm-lockfile-prototype/archive/refs/tags/v${RLP_VERSION}.tar.gz"

die() { echo "error: $*" >&2; exit 1; }

[[ $# -ge 1 ]] || die "usage: $0 <checkout> [component-dir ...]"
CHECKOUT="$(cd "$1" && pwd)" || die "no such checkout: $1"
shift
: "${REGISTRY_AUTH_USER:?set REGISTRY_AUTH_USER}"
: "${REGISTRY_AUTH_PASS:?set REGISTRY_AUTH_PASS}"
export REGISTRY_AUTH_USER REGISTRY_AUTH_PASS
command -v podman >/dev/null || die "podman is required"
command -v skopeo >/dev/null || die "skopeo is required"
if [[ "$(uname -m)" != "x86_64" && -z "${FORCE_ARCH:-}" ]]; then
  die "run this on an x86_64 host (set FORCE_ARCH=1 to emulate)"
fi

# Base image labels are read on the host; reuse the same credentials. Log in
# with a throwaway authfile so the service-account credential doesn't persist
# in the host's default auth.json after this script exits.
AUTHFILE="$(mktemp)"
trap 'rm -f "$AUTHFILE"' EXIT
printf '%s' "$REGISTRY_AUTH_PASS" \
  | skopeo login --authfile "$AUTHFILE" -u "$REGISTRY_AUTH_USER" \
      --password-stdin registry.redhat.io >/dev/null

if [[ $# -eq 0 ]]; then
  mapfile -t COMPONENTS < <(
    cd "$CHECKOUT" && find . -name rpms.in.yaml -not -path './vendor/*' \
      -printf '%h\n' | sed 's|^\./||; s|^$|.|' | sort
  )
else
  COMPONENTS=("$@")
fi

# yaml_value <file> <key>: first "key: value" under context.containerfile
yaml_value() {
  sed -n "s/^[[:space:]]*$2:[[:space:]]*//p" "$1" | head -1 | tr -d "\"'"
}

# stage_image <containerfile> [stage]: FROM image of the stage (or the last)
stage_image() {
  local file=$1 stage=${2:-} line
  if [[ -n "$stage" ]]; then
    line=$(grep -iE "^FROM[[:space:]].*[[:space:]]AS[[:space:]]+${stage}([[:space:]]|$)" "$file" | head -1)
  else
    line=$(grep -iE '^FROM[[:space:]]' "$file" | tail -1)
  fi
  [[ -n "$line" ]] || return 1
  # drop FROM, --platform style flags and "AS name"
  awk '{for (i = 2; i <= NF; i++) if ($i !~ /^--/) { print $i; exit }}' <<<"$line"
}

# rhel_version <image>: "10.1" / "9" from the cpe label, falling back to the name
rhel_version() {
  local cpe
  cpe=$(skopeo inspect --authfile "$AUTHFILE" --override-os linux \
          --override-arch amd64 --no-tags \
          --format '{{index .Labels "cpe"}}' "docker://$1" 2>/dev/null || true)
  if [[ "$cpe" =~ enterprise_linux:([0-9]+(\.[0-9]+)?) ]]; then
    echo "${BASH_REMATCH[1]}"
  elif [[ "$1" =~ (ubi|rhel)([0-9]+) ]]; then
    echo "${BASH_REMATCH[2]}"
  else
    return 1
  fi
}

# Steps run inside the container. These are quoted heredocs, so every $ and
# quote below reaches the container shell literally; run_in passes the
# variables they reference through the environment.
read -r -d '' REGISTER_STEPS <<'EOF' || true
registered=0
for attempt in 1 2 3; do
  if subscription-manager register --activationkey="${ACTIVATION_KEY}${RHEL_MAJOR}" --org="$RHSM_ORG" >/dev/null; then
    registered=1
    break
  fi
  sleep 30
done
[[ $registered -eq 1 ]]
# RHSM rate limits rapid registrations; unregister when the container exits
# so it doesn't leave a stale registration on the org.
trap 'subscription-manager unregister >/dev/null 2>&1 || true' EXIT
EOF

read -r -d '' EUS_STEPS <<'EOF' || true
# EUS repos 404 unless the minor is an EUS release; disable them on every
# RHEL major (the manual guide does this for RHEL 10) so dnf and the
# lockfile don't try to fetch them.
arch=$(uname -m)
subscription-manager repos \
  --disable="rhel-${RHEL_MAJOR}-for-$arch-baseos-eus-rpms" \
  --disable="rhel-${RHEL_MAJOR}-for-$arch-appstream-eus-rpms" \
  --disable="rhel-${RHEL_MAJOR}-for-$arch-baseos-eus-source-rpms" \
  --disable="rhel-${RHEL_MAJOR}-for-$arch-appstream-eus-source-rpms" >/dev/null || true
yum clean all -q
EOF

read -r -d '' DEPS_STEPS <<'EOF' || true
dnf install -y -q pip skopeo >/dev/null
printf '%s' "$REGISTRY_AUTH_PASS" \
  | skopeo login -u "$REGISTRY_AUTH_USER" --password-stdin registry.redhat.io >/dev/null
python3 -m pip install -q --user "$RLP_URL"
export PATH="$HOME/.local/bin:$PATH"
EOF

read -r -d '' COPY_STEPS <<'EOF' || true
for COMPONENT in $GROUP_COMPONENTS; do
  cp /etc/yum.repos.d/redhat.repo "/source/$COMPONENT/redhat.repo"
  sed -i "s/$(uname -m)/\$basearch/g" "/source/$COMPONENT/redhat.repo"
done
EOF

read -r -d '' SOLVE_STEPS <<'EOF' || true
failed=""
for COMPONENT in $GROUP_COMPONENTS; do
  echo "== solving $COMPONENT"
  if (cd "/source/$COMPONENT" && rpm-lockfile-prototype rpms.in.yaml); then
    :
  else
    failed="$failed $COMPONENT"
  fi
done
echo "RLP-FAILED:${failed}"
EOF

run_in() {  # run_in <image> <script>
  podman run --rm --platform linux/amd64 --tmpfs /run/secrets \
    -v "$CHECKOUT:/source:Z" \
    -e COMPONENT -e RHEL_MAJOR -e RHEL_MINOR -e ACTIVATION_KEY -e RHSM_ORG \
    -e GROUP_COMPONENTS -e RLP_URL -e REGISTRY_AUTH_USER -e REGISTRY_AUTH_PASS -e OLD_ENTITLEMENT \
    "$1" bash -euo pipefail -c "$2"
}

# One container per solver image: components whose bases run the same RHEL
# version share a registration, a dependency install and a repo file.
refresh_group() {  # refresh_group <image> <component>...
  local image=$1 log failed_list c
  shift
  local components="$*"
  echo "  container: $image ($# components)"
  export GROUP_COMPONENTS="$components"
  if log=$(run_in "$image" "$REGISTER_STEPS
$EUS_STEPS
$DEPS_STEPS
$COPY_STEPS
$SOLVE_STEPS" 2>&1 | tee /dev/stderr); then
    failed_list="$(sed -n 's/^RLP-FAILED://p' <<<"$log" | tail -1)"
  else
    failed_list="$components"  # registration or setup failed for the group
  fi
  for c in $components; do
    if [[ " $failed_list " == *" $c "* ]]; then
      failed+=("$c")
    else
      ok+=("$c")
    fi
  done
}

# winlegacyiso: RHEL 7 repo file, locked from a RHEL 9 container whose
# entitlement certs replace the RHEL 7 container's in redhat.repo.
refresh_rhel7() {
  # Stage 2 solves a single component, but it reuses SOLVE_STEPS, which
  # iterates GROUP_COMPONENTS.
  export GROUP_COMPONENTS="$COMPONENT"
  echo "  stage 1: registry.access.redhat.com/ubi7 (repo file)"
  OLD_ENTITLEMENT=$(run_in registry.access.redhat.com/ubi7:latest "
# ubi7's subscription-manager refuses to run while /etc/rhsm-host (a
# symlink into /run/secrets/rhsm) resolves; remove it to register.
rm -f /etc/rhsm-host
registered=0
for attempt in 1 2 3; do
  if subscription-manager register --activationkey=\"\${ACTIVATION_KEY}7\" --org=\"\$RHSM_ORG\" >/dev/null; then
    registered=1
    break
  fi
  sleep 30
done
[[ \$registered -eq 1 ]]
trap 'subscription-manager unregister >/dev/null 2>&1 || true' EXIT
cp /etc/yum.repos.d/redhat.repo \"/source/\$COMPONENT/redhat.repo\"
sed -i \"s/\$(uname -m)/\\\$basearch/g; s/\\\$releasever/7Server/g\" \"/source/\$COMPONENT/redhat.repo\"
ls /etc/pki/entitlement/ | grep -v -- '-key.pem' | head -1 | sed 's/\\.pem\$//'" | tail -1)
  export OLD_ENTITLEMENT
  [[ -n "$OLD_ENTITLEMENT" ]] || { echo "  could not read the RHEL 7 entitlement id"; return 1; }
  echo "  stage 2: registry.access.redhat.com/ubi9 (lockfile)"
  run_in registry.access.redhat.com/ubi9:latest "
registered=0
for attempt in 1 2 3; do
  if subscription-manager register --activationkey=\"\${ACTIVATION_KEY}9\" --org=\"\$RHSM_ORG\" >/dev/null; then
    registered=1
    break
  fi
  sleep 30
done
[[ \$registered -eq 1 ]]
trap 'subscription-manager unregister >/dev/null 2>&1 || true' EXIT
# Same EUS 404s as the standard flow: the activation key enables RHEL 9 EUS
# repos that only exist on EUS minors.
arch=\$(uname -m)
subscription-manager repos \\
  --disable=\"rhel-9-for-\$arch-baseos-eus-rpms\" \\
  --disable=\"rhel-9-for-\$arch-appstream-eus-rpms\" \\
  --disable=\"rhel-9-for-\$arch-baseos-eus-source-rpms\" \\
  --disable=\"rhel-9-for-\$arch-appstream-eus-source-rpms\" >/dev/null || true
new=\$(ls /etc/pki/entitlement/ | grep -v -- '-key.pem' | head -1 | sed 's/\\.pem\$//')
sed -i \"s/\$OLD_ENTITLEMENT/\$new/g\" \"/source/\$COMPONENT/redhat.repo\"
$DEPS_STEPS
$SOLVE_STEPS"
}

export RLP_URL ACTIVATION_KEY RHSM_ORG
ok=() failed=()

# Resolve every component to its solver image, then refresh each group in
# one container. RHEL 7 components (el7 package sets) keep their own
# two-stage flow.
refresh_batch() {  # refresh_batch <component>...
  local -A groups=()
  local COMPONENT spec file stage containerfile image version major minor
  for COMPONENT in "$@"; do
    COMPONENT="${COMPONENT%/}"
    # Accept a bare component name; the ".konflux/" prefix is optional
    [[ -f "$CHECKOUT/$COMPONENT/rpms.in.yaml" ]] || COMPONENT=".konflux/$COMPONENT"
    export COMPONENT
    spec="$CHECKOUT/$COMPONENT/rpms.in.yaml"
    echo "== $COMPONENT"
    if [[ ! -f "$spec" ]]; then
      echo "  no rpms.in.yaml"; failed+=("$COMPONENT"); continue
    fi
    # Only the stageName selector is supported; rather than silently locking
    # against the wrong stage, fail loudly on the others.
    if grep -qE '^[[:space:]]*(stageNum|imagePattern):' "$spec"; then
      echo "  unsupported stage selector in rpms.in.yaml (stageNum/imagePattern)"
      failed+=("$COMPONENT"); continue
    fi
    file=$(yaml_value "$spec" file)
    stage=$(yaml_value "$spec" stageName)
    containerfile=$(realpath -m "$CHECKOUT/$COMPONENT/$file")
    if ! image=$(stage_image "$containerfile" "$stage"); then
      echo "  no FROM for stage '${stage:-<last>}' in $file"; failed+=("$COMPONENT"); continue
    fi
    if grep -qE '\.el7([^0-9]|$)' "$spec"; then
      RHEL_MAJOR=7 RHEL_MINOR=""
      export RHEL_MAJOR RHEL_MINOR
      echo "  base: $image (RHEL 7 packages)"
      if refresh_rhel7; then ok+=("$COMPONENT"); else failed+=("$COMPONENT"); fi
      continue
    fi
    if ! version=$(rhel_version "$image"); then
      echo "  can't tell the RHEL version of $image"; failed+=("$COMPONENT"); continue
    fi
    major="${version%%.*}"
    minor=""
    [[ "$version" == *.* ]] && minor="$version"
    echo "  base: $image (RHEL $version)"
    groups["registry.access.redhat.com/ubi${major}:${minor:-latest}"]+="$COMPONENT "
  done

  for image in $(printf '%s\n' "${!groups[@]}" | sort); do
    RHEL_MAJOR="${image##*ubi}"; RHEL_MAJOR="${RHEL_MAJOR%%:*}"
    RHEL_MINOR="${image##*:}"
    [[ "$RHEL_MINOR" == latest ]] && RHEL_MINOR=""
    export RHEL_MAJOR RHEL_MINOR
    refresh_group "$image" ${groups[$image]}
  done
}

refresh_batch "${COMPONENTS[@]}"

# A large batch can trip RHSM's registration rate limit near the end; give
# failed components one more attempt after a cooldown.
if (( ${#failed[@]} )) && [[ -z "${NO_RETRY:-}" ]]; then
  echo
  echo "retrying after 60s: ${failed[*]}"
  sleep 60
  retry=("${failed[@]}")
  failed=()
  refresh_batch "${retry[@]}"
fi

echo
echo "Refreshed: ${ok[*]:-none}"
echo "Failed:    ${failed[*]:-none}"
echo
git -C "$CHECKOUT" status --short -- '*redhat.repo' '*rpms.lock.yaml' || true
[[ ${#failed[@]} -eq 0 ]]
