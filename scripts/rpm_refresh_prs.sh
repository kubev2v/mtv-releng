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

# Open same-repository PRs whose body still carries the refresh request.
# Cross-repository PRs are skipped: their head branch lives in a fork, so
# fetching it from $REPO would either fail or hit an unrelated same-named
# branch. The generous limit keeps requests from falling outside the
# discovery window of open PRs.
list_prs() {
  gh pr list --repo "$REPO" --state open --limit 200 \
    --json number,headRefName,isCrossRepository,body --jq \
    '.[] | select(.isCrossRepository | not)
        | select(.body | test("RPM lockfiles need a refresh"))
        | "\(.number) \(.headRefName)"'
}

# Component list from the PR body's fenced refresh command; empty when the
# request is malformed (section present, command line missing or edited).
components_of() { # $1 = pr number -> stdout, rc 0
  gh pr view "$1" --repo "$REPO" --json body --jq .body \
    | grep 'rpm_lockfile_refresh.sh /path/to/checkout' | head -1 \
    | sed 's/.*rpm_lockfile_refresh.sh \/path\/to\/checkout //' || true
}

# Drop the refresh-request section from the PR body, keeping any section
# that follows it. Once the lockfiles are pushed (or proven current), the
# request is fulfilled — without this, every later run re-clones and
# re-resolves the same PR just to find no diff.
clear_refresh_request() { # $1 = pr number
  local body
  body="$(mktemp)"
  gh pr view "$1" --repo "$REPO" --json body --jq .body | awk '
    /^## RPM lockfiles need a refresh/ { drop = 1; next }
    drop && /^## / { drop = 0 }
    !drop { print }
  ' | sed -e :a -e '/^$/N;/^\n$/D' > "$body"
  gh pr edit "$1" --repo "$REPO" --body-file "$body" >/dev/null
  rm -f "$body"
}

# The remote head is still the sha this refresh started from. Guards the
# request-clearing: if dep_bump re-rendered the PR mid-refresh, the branch
# moved and the (newer) request must survive for the next run.
head_unchanged() { # $1 = repo dir, $2 = head branch, $3 = sha
  local remote
  remote="$(git -C "$1" ls-remote origin "refs/heads/$2" | cut -f1)"
  [ "$remote" = "$3" ]
}

# Discovery runs before the loop so its failure is the script's failure,
# not an empty-but-successful zero-PR run.
prs="$(list_prs)" || die "PR discovery failed"
count=0
while read -r number head; do
  [ -n "${number:-}" ] || continue
  comps="$(components_of "$number")"
  [ -n "$comps" ] || { log "PR $number: no component list in body; skipping"; continue; }
  log "PR $number ($head): refreshing $comps"

  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  if ! { git clone -q --depth 1 "https://github.com/$REPO.git" "$tmp/repo" \
       && git -C "$tmp/repo" fetch -q origin \
            "+refs/heads/$head:refs/remotes/origin/$head"; }; then
    log "PR $number: clone/fetch failed; skipping"
    rm -rf "$tmp"
    continue
  fi
  sha="$(git -C "$tmp/repo" rev-parse "refs/remotes/origin/$head")"
  git -C "$tmp/repo" checkout -q -B "$head" "$sha"

  if ! bash "$SCRIPT_DIR/rpm_lockfile_refresh.sh" "$tmp/repo" $comps; then
    log "PR $number: refresh failed; leaving the branch untouched"
    rm -rf "$tmp"
    continue
  fi

  if [ -z "$(git -C "$tmp/repo" status --porcelain)" ]; then
    log "PR $number: lockfiles already current; nothing to push"
    if head_unchanged "$tmp/repo" "$head" "$sha"; then
      clear_refresh_request "$number"
    else
      log "PR $number: branch moved during refresh; keeping the request"
    fi
    rm -rf "$tmp"
    continue
  fi
  git -C "$tmp/repo" add -A
  git -C "$tmp/repo" -c user.name="MTV Bot - IIB Automation" \
    -c user.email="mtv-automation@redhat.com" commit -q -m \
    "chore(deps): refresh RPM lockfiles for the bumped base images

Co-Authored-By: Claude Code <noreply@anthropic.com>"
  if ! git -C "$tmp/repo" push -q --force-with-lease="$head:$sha" \
       origin "HEAD:refs/heads/$head"; then
    log "PR $number: push rejected (branch moved?); keeping the request"
    rm -rf "$tmp"
    continue
  fi
  clear_refresh_request "$number"
  log "PR $number: lockfiles pushed"
  count=$((count + 1))
  rm -rf "$tmp"
done <<< "$prs"

trap - EXIT
log "done: $count PR(s) refreshed"
