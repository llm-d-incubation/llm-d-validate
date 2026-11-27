#!/usr/bin/env bash
# CI orchestration lives outside llmdv's read-only core. See README.md.
set -uo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
exec python3 "$HERE/smoke_step.py"
