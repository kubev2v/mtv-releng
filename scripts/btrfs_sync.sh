#!/usr/bin/env bash

# Script to sync the btrfs filesystem

set -e

# Source utility functions
source scripts/util.sh
source scripts/auth.sh

# ============================================================================
# Configuration Variables (can be overridden externally)
# ============================================================================

GIT_BRANCH=$1

if [ -z "$GIT_BRANCH" ]; then
  log_error "Usage: $0 <git_branch>"
  log_error "Example: $0 main"
  exit 1
fi

forklift_repo="https://github.com/kubev2v/forklift.git"
forklift_internal_repo="https://gitlab.cee.redhat.com/mtv/forklift.git"

# ============================================================================
# GitLab Authentication Setup
# ============================================================================
# For private GitLab repositories, authentication is required.
# Options:
# 1. Set GITLAB_TOKEN environment variable (Personal Access Token)
# 2. Set GITLAB_USER and GITLAB_TOKEN environment variables
# 3. Use SSH URLs instead (requires SSH keys configured)
# ============================================================================

# Build authenticated URL if token is provided
if [ -n "${GITLAB_TOKEN:-}" ]; then
  if [ -n "${GITLAB_USER:-}" ]; then
    # Use username:token format
    forklift_internal_repo="https://${GITLAB_USER}:${GITLAB_TOKEN}@gitlab.cee.redhat.com/mtv/forklift.git"
  else
    # Use oauth2:token format (GitLab accepts oauth2 as username with token)
    forklift_internal_repo="https://oauth2:${GITLAB_TOKEN}@gitlab.cee.redhat.com/mtv/forklift.git"
  fi
  log_info "Using GitLab authentication token"
elif [ -z "${GITLAB_TOKEN:-}" ]; then
  log_warning "GITLAB_TOKEN not set. Git operations on private repository may fail."
  log_warning "Set GITLAB_TOKEN environment variable with your GitLab Personal Access Token."
  log_warning "Alternatively, set GITLAB_USER and GITLAB_TOKEN for username:token authentication."
fi

# ============================================================================
# Main Sync Process
# ============================================================================

tmp_dir=$(mktemp -d)
log_info "Cloning repository to temporary directory: $tmp_dir"

git clone $forklift_repo $tmp_dir
cd $tmp_dir

# If branch doesn't exist on origin, create it from main
if git rev-parse --verify "origin/$GIT_BRANCH" >/dev/null 2>&1; then
  log_info "Branch $GIT_BRANCH exists on origin, checking out"
  git checkout $GIT_BRANCH
else
  log_info "Branch $GIT_BRANCH does not exist on origin, creating from main"
  git checkout main
  git checkout -b $GIT_BRANCH
fi

log_info "Disable SSL verification"
git config http.sslVerify "false"

log_info "Adding internal GitLab remote"
git remote add internal $forklift_internal_repo
# Always fetch internal so we can pull/rebase when internal has this branch (including when we created branch from main)
git fetch internal

if git rev-parse --verify "internal/$GIT_BRANCH" >/dev/null 2>&1; then
  log_info "Pulling from internal repository (branch: $GIT_BRANCH)"
  git pull internal $GIT_BRANCH --rebase
else
  log_info "Branch $GIT_BRANCH does not exist on internal, skipping internal pull"
fi

if git rev-parse --verify "origin/$GIT_BRANCH" >/dev/null 2>&1; then
  log_info "Pulling from origin repository (branch: $GIT_BRANCH)"
  git pull origin $GIT_BRANCH --rebase
else
  log_info "Branch created from main, skipping origin pull"
fi

log_info "Pushing to internal repository (branch: $GIT_BRANCH)"
git push -f internal $GIT_BRANCH

log_success "Sync completed successfully"
