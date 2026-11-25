#!/usr/bin/env bash
# Exercise an installed image without a model, credentials or network access.
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
image=${1:?usage: smoke-image.sh IMAGE PLATFORM}
platform=${2:?usage: smoke-image.sh IMAGE PLATFORM}

docker run --rm --platform "$platform" --network none --read-only --user 10001:0 \
  --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /work:rw,mode=1777 --tmpfs /tmp:rw,mode=1777 \
  --mount "type=bind,source=$root/profiles/examples,target=/examples,readonly" \
  --entrypoint /bin/bash "$image" -e -o pipefail -c '
    test "$(id -u)" = 10001
    mkdir -p "$LLMDV_WORK_DIR"
    touch "$LLMDV_WORK_DIR/smoke" /tmp/smoke
    python -m pip check
    python -c "import boto3, ibm_boto3, llmdv.plugin"
    llmdv --version
    llmdv job --help
    llmdv profile validate /examples/example-gateway-only.yaml
    llmdv plan --profile /examples/example-gateway-only.yaml -q
    claude --version
  '
