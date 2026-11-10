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
# Seed an empty credential store: some skopeo builds refuse to merge login
# credentials into a pre-created zero-byte authfile ("unexpected end of JSON
# input"), while all of them read "{}" as no credentials.
printf '{}' >"$AUTHFILE"
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
# python3-pip, not pip: RHEL 8 has no package named "pip"
dnf install -y -q python3-pip skopeo >/dev/null
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
  args=""
  # A lock in BARE_COMPONENTS carries the base image's own packages; keep it
  # a full closure (see the detection in refresh_batch).
  case " $BARE_COMPONENTS " in *" $COMPONENT "*) args="--bare" ;; esac
  if (cd "/source/$COMPONENT" && rpm-lockfile-prototype $args rpms.in.yaml); then
    :
  else
    failed="$failed $COMPONENT"
  fi
done
echo "RLP-FAILED:${failed}"
EOF

read -r -d '' RELEASEVER_STEPS <<'EOF' || true
# Repo files written inside UBI containers keep $releasever in their baseurls,
# and dnf inside UBI expands it to the bare major: the rolling current-GA
# stream. Pin it to the base image's RHEL minor so the lock matches the base
# (9.6 EUS content for a ubi9:9.6 base, not today's 9.8). RHEL 7 repo files
# need the 7Server stream instead; RHEL 8 files already use literal URLs.
if [ -n "$RHEL_MINOR" ]; then
  for COMPONENT in $GROUP_COMPONENTS; do
    sed -i "s/\$releasever/$RHEL_MINOR/g" "/source/$COMPONENT/redhat.repo"
  done
elif [ "$RHEL_MAJOR" = 7 ]; then
  for COMPONENT in $GROUP_COMPONENTS; do
    sed -i "s/\$releasever/7Server/g" "/source/$COMPONENT/redhat.repo"
  done
fi
EOF

read -r -d '' ENTITLEMENT_STEPS <<'EOF' || true
ls /etc/pki/entitlement/ | grep -v -- '-key.pem' | head -1 | sed 's/\.pem$//'
EOF

read -r -d '' SWAP_STEPS <<'EOF' || true
new=$(ls /etc/pki/entitlement/ | grep -v -- '-key.pem' | head -1 | sed 's/\.pem$//')
for COMPONENT in $GROUP_COMPONENTS; do
  sed -i "s/$OLD_ENTITLEMENT/$new/g" "/source/$COMPONENT/redhat.repo"
done
EOF

run_in() {  # run_in <image> <script>
  podman run --rm --platform linux/amd64 --tmpfs /run/secrets \
    -v "$CHECKOUT:/source:Z" \
    -e COMPONENT -e RHEL_MAJOR -e RHEL_MINOR -e ACTIVATION_KEY -e RHSM_ORG \
    -e GROUP_COMPONENTS -e BARE_COMPONENTS -e RLP_URL -e REGISTRY_AUTH_USER -e REGISTRY_AUTH_PASS -e OLD_ENTITLEMENT \
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
$RELEASEVER_STEPS
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

# RHEL 7 and 8: the solver can't be the base image's own RHEL major.
# rpm-lockfile-prototype needs Python >= 3.8 together with the dnf bindings,
# and those bindings exist only for the system python (3.6 on RHEL 8, older
# still on RHEL 7). Stage 1 registers a ubi<major> container and writes the
# repo files; stage 2 registers ubi9 and solves there, with its own
# entitlement ids swapped into the repo files (the CDN checks the certs, not
# which RHEL wrote the file).
refresh_two_stage() {  # refresh_two_stage <major> <component>...
  local major=$1 log entitlement failed_list c
  shift
  local components="$*"
  export GROUP_COMPONENTS="$components"
  RHEL_MAJOR="$major" RHEL_MINOR=""
  export RHEL_MAJOR RHEL_MINOR
  # ubi7's subscription-manager refuses to run while /etc/rhsm-host (a
  # symlink into /run/secrets/rhsm) resolves; removing it is harmless on 8.
  echo "  stage 1: registry.access.redhat.com/ubi${major}:latest (repo files, $# components)"
  entitlement=""
  if log=$(run_in "registry.access.redhat.com/ubi${major}:latest" "rm -f /etc/rhsm-host
$REGISTER_STEPS
$EUS_STEPS
$COPY_STEPS
$RELEASEVER_STEPS
$ENTITLEMENT_STEPS" 2>&1 | tee /dev/stderr); then
    entitlement="$(tail -1 <<<"$log")"
  fi
  if [[ -z "$entitlement" ]]; then
    echo "  stage 1 failed for:$components"
    failed+=($components)
    return
  fi
  export OLD_ENTITLEMENT="$entitlement"
  echo "  stage 2: registry.access.redhat.com/ubi9:latest (lockfiles)"
  RHEL_MAJOR=9 RHEL_MINOR=""
  export RHEL_MAJOR RHEL_MINOR
  if log=$(run_in registry.access.redhat.com/ubi9:latest "$REGISTER_STEPS
$EUS_STEPS
$SWAP_STEPS
$DEPS_STEPS
$SOLVE_STEPS" 2>&1 | tee /dev/stderr); then
    failed_list="$(sed -n 's/^RLP-FAILED://p' <<<"$log" | tail -1)"
  else
    failed_list="$components"
  fi
  for c in $components; do
    if [[ " $failed_list " == *" $c "* ]]; then
      failed+=("$c")
    else
      ok+=("$c")
    fi
  done
}

export RLP_URL ACTIVATION_KEY RHSM_ORG BARE_COMPONENTS
ok=() failed=()

# Resolve every component to its solver image, then refresh each group in
# one container. RHEL 7 and 8 components keep their own two-stage flow:
# their system pythons can't run rpm-lockfile-prototype.
refresh_batch() {  # refresh_batch <component>...
  local -A groups=() two_stage=()
  # Components whose existing lock is a full rootfs closure (detected below);
  # their refresh keeps --bare. Reset per batch, including the retry.
  BARE_COMPONENTS=""
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
    # glibc is in every RHEL base image and never installed explicitly, so a
    # lock that contains it was generated without subtracting the base image's
    # packages -- a full rootfs closure. Some builds (copy-appliance's bake
    # step installs into an empty root) need that; keep them in --bare mode.
    # Self-consistent across re-runs: a --bare lock keeps glibc, a default
    # lock doesn't.
    if [[ -f "$CHECKOUT/$COMPONENT/rpms.lock.yaml" ]] \
      && grep -q '^    name: glibc$' "$CHECKOUT/$COMPONENT/rpms.lock.yaml"; then
      BARE_COMPONENTS+="$COMPONENT "
    fi
    file=$(yaml_value "$spec" file)
    stage=$(yaml_value "$spec" stageName)
    containerfile=$(realpath -m "$CHECKOUT/$COMPONENT/$file")
    if ! image=$(stage_image "$containerfile" "$stage"); then
      echo "  no FROM for stage '${stage:-<last>}' in $file"; failed+=("$COMPONENT"); continue
    fi
    if grep -qE '\.el7([^0-9]|$)' "$spec"; then
      echo "  base: $image (RHEL 7 packages)"
      two_stage[7]+="$COMPONENT "
      continue
    fi
    if ! version=$(rhel_version "$image"); then
      echo "  can't tell the RHEL version of $image"; failed+=("$COMPONENT"); continue
    fi
    major="${version%%.*}"
    minor=""
    [[ "$version" == *.* ]] && minor="$version"
    echo "  base: $image (RHEL $version)"
    if (( major <= 8 )); then
      # RHEL 8's system python is too old for rpm-lockfile-prototype too;
      # same two-stage flow as the el7 package sets.
      two_stage[$major]+="$COMPONENT "
    else
      groups["registry.access.redhat.com/ubi${major}:${minor:-latest}"]+="$COMPONENT "
    fi
  done

  for major in $(printf '%s\n' "${!two_stage[@]}" | sort); do
    refresh_two_stage "$major" ${two_stage[$major]}
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
