#!/usr/bin/env bash
# Shared by the PyPI and image workflows before publishing a release tag.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

permission=$(gh api "repos/$GITHUB_REPOSITORY/collaborators/$GITHUB_ACTOR/permission" --jq .permission)
if [ "$permission" != "admin" ]; then
  echo "::error::$GITHUB_ACTOR has $permission on this repository; only an admin can publish a release"
  exit 1
fi

# Callers check out the event commit with full history. Check HEAD, not a tag that
# could have moved since this run started. Fetch main so old release commits work too.
git fetch --no-tags origin +refs/heads/main:refs/remotes/origin/main
if ! git merge-base --is-ancestor HEAD origin/main; then
  echo "::error::tag $GITHUB_REF_NAME points to a commit that is not on main; merge the release changes before tagging"
  exit 1
fi

version=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' src/llmdv/__init__.py)
if [ -z "$version" ] || [ "$GITHUB_REF_NAME" != "v$version" ]; then
  echo "::error::tag $GITHUB_REF_NAME does not match __version__ $version in src/llmdv/__init__.py"
  exit 1
fi
