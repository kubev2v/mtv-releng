#!/usr/bin/env bash
# Refresh RPM lockfiles on every open dep_bump PR that asks for it.
#
# dep_bump runs containerized (no podman), so it can't run the refresh
# itself; this runs on the host where podman lives. For each open PR whose
# body has the "RPM lockfiles need a refresh" section, it parses the
# component list from that section, checks out the PR branch, runs
# rpm_lockfile_refresh.sh, and pushes the result with a lease pinned to the
# sha it fetched — a hand-pushed commit on the branch is never clobbered
# silently.
#
# PRs are processed strictly one at a time: the registry rate budget is
# account-wide, not per branch, so parallel refreshes would multiply it.
#
# Run on the x86_64 host, from anywhere:
#   scripts/rpm_refresh_prs.sh [repo]     # default kubev2v/forklift

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${1:-kubev2v/forklift}"

die() { echo "error: $*" >&2; exit 1; }

command -v gh >/dev/null || die "gh is required"
command -v git >/dev/null || die "git is required"

log() { echo "[rpm-refresh] $*"; }

# Open PRs whose body still carries the refresh request
list_prs() {
  gh pr list --repo "$REPO" --state open --limit 30 \
    --json number,headRefName,body --jq \
    '.[] | select(.body | test("RPM lockfiles need a refresh")) | "\(.number) \(.headRefName)"'
}

# Component list from the PR body's fenced refresh command
components_of() { # $1 = pr number
  gh pr view "$1" --repo "$REPO" --json body --jq .body \
    | grep 'rpm_lockfile_refresh.sh /path/to/checkout' | head -1 \
    | sed 's/.*rpm_lockfile_refresh.sh \/path\/to\/checkout //'
}

# Drop the refresh-request section from the PR body, keeping any section
# that follows it. Once the lockfiles are pushed (or proven current), the
# request is fulfilled — without this, every later run re-clones and
# re-resolves the same PR just to find no diff.
clear_refresh_request() { # $1 = pr number
  gh pr view "$1" --repo "$REPO" --json body --jq .body | awk '
    /^## RPM lockfiles need a refresh/ { drop = 1; next }
    drop && /^## / { drop = 0 }
    !drop { print }
  ' | sed -e :a -e '/^$/N;/^\n$/D' > /tmp/pr-body.$$
  gh pr edit "$1" --repo "$REPO" --body-file /tmp/pr-body.$$ >/dev/null
  rm -f /tmp/pr-body.$$
}

count=0
while read -r number head; do
  [ -n "${number:-}" ] || continue
  comps="$(components_of "$number")"
  [ -n "$comps" ] || { log "PR $number: no component list in body; skipping"; continue; }
  log "PR $number ($head): refreshing $comps"

  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  git clone -q --depth 1 "https://github.com/$REPO.git" "$tmp/repo"
  git -C "$tmp/repo" fetch -q origin "+refs/heads/$head:refs/remotes/origin/$head"
  sha="$(git -C "$tmp/repo" rev-parse "refs/remotes/origin/$head")"
  git -C "$tmp/repo" checkout -q -B "$head" "$sha"

  if ! bash "$SCRIPT_DIR/rpm_lockfile_refresh.sh" "$tmp/repo" $comps; then
    log "PR $number: refresh failed; leaving the branch untouched"
    continue
  fi

  if [ -z "$(git -C "$tmp/repo" status --porcelain)" ]; then
    log "PR $number: lockfiles already current; nothing to push"
    clear_refresh_request "$number"
    continue
  fi
  git -C "$tmp/repo" add -A
  git -C "$tmp/repo" -c user.name="MTV Bot - IIB Automation" \
    -c user.email="mtv-automation@redhat.com" commit -q -m \
    "chore(deps): refresh RPM lockfiles for the bumped base images

Co-Authored-By: Claude Code <noreply@anthropic.com>"
  git -C "$tmp/repo" push -q --force-with-lease="$head:$sha" \
    origin "HEAD:refs/heads/$head"
  clear_refresh_request "$number"
  log "PR $number: lockfiles pushed"
  count=$((count + 1))
done < <(list_prs)

log "done: $count PR(s) refreshed"
