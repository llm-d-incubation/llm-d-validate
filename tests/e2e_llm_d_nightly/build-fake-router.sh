#!/usr/bin/env bash
# Build the stand-in router image on top of an llm-d-validate image (which has llmdv, the
# fake fleet's only dependency, installed).
#
#   build-fake-router.sh <docker|podman> <llm-d-validate image> <tag for the stand-in>
#
# The build context is a copy of tests/ alone: the repository's .containerignore leaves tests/
# out of image builds, and Docker and Podman do not read the same ignore file.
set -euo pipefail

engine=${1:?docker or podman}
base=${2:?the llm-d-validate image to build on}
tag=${3:?the tag for the stand-in image}
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)

context=$(mktemp -d)
trap 'rm -rf "$context"' EXIT
cp -R "$root/tests" "$context/tests"
find "$context/tests" -name __pycache__ -prune -exec rm -rf {} +
cat > "$context/Containerfile" <<EOF
FROM $base
COPY tests /opt/fake/tests
ENTRYPOINT ["python", "/opt/fake/tests/e2e_llm_d_nightly/fake_router.py", "serve"]
EOF
"$engine" build -q -f "$context/Containerfile" -t "$tag" "$context"
