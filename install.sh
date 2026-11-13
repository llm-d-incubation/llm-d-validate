#!/usr/bin/env bash
#
# Set up a virtualenv and install llmdv into it.
#
#   ./install.sh              install the library
#   ./install.sh --s3         also install boto3, for --remote-output s3://...
#   ./install.sh --dev        also install ruff, pre-commit, pytest-xdist and both publishing backends
#   ./install.sh --recreate   throw the virtualenv away and start again
#
# Idempotent: re-running is the normal way to pick up a dependency change.
set -euo pipefail

cd "$(dirname "$0")"

VENV=".venv"
EXTRAS=""
INSTALL_DEV=0
INSTALL_S3=0
RECREATE=0
PYTHON="${PYTHON:-python3}"

for arg in "$@"; do
  case "$arg" in
    --s3)       INSTALL_S3=1 ;;
    --dev)      INSTALL_DEV=1 ;;
    --recreate) RECREATE=1 ;;
    -h|--help)  sed -n '3,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)          echo "install.sh: unknown option '$arg' (try --help)" >&2; exit 2 ;;
  esac
done

if [ "$INSTALL_DEV" = 1 ]; then
  # Everything, so the offline suite can exercise both publishing backends.
  EXTRAS="[dev,s3,ibm-cos]"
elif [ "$INSTALL_S3" = 1 ]; then
  EXTRAS="[s3]"
fi

# The floor is checked before anything else. On an older interpreter the failure would
# otherwise surface as a syntax or typing error from inside the package, which tells you
# nothing about the actual problem.
REQUIRED_MAJOR=3
REQUIRED_MINOR=10
if ! "$PYTHON" -c "import sys; sys.exit(0 if sys.version_info[:2] >= ($REQUIRED_MAJOR, $REQUIRED_MINOR) else 1)" 2>/dev/null; then
  have="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "not found")"
  echo "install.sh: need Python >= ${REQUIRED_MAJOR}.${REQUIRED_MINOR}, found ${have}" >&2
  echo "            set PYTHON=/path/to/python3 to choose a different interpreter" >&2
  exit 1
fi

if [ "$RECREATE" = 1 ] && [ -d "$VENV" ]; then
  echo "removing $VENV"
  rm -rf "$VENV"
fi

if [ ! -d "$VENV" ]; then
  echo "creating $VENV with $("$PYTHON" -V)"
  "$PYTHON" -m venv "$VENV"
else
  echo "reusing existing $VENV"
fi

PIP="$VENV/bin/pip"
"$PIP" install --quiet --upgrade pip
echo "installing llmdv${EXTRAS:-} (editable)"
"$PIP" install --quiet --editable ".${EXTRAS}"

# Verify rather than assume. An installer that exits 0 without checking has told you nothing.
echo
failed=0
if version="$("$VENV/bin/llmdv" --version 2>/dev/null)"; then
  echo "  ok    $version"
else
  echo "  FAIL  the llmdv entry point does not run" >&2; failed=1
fi

if "$VENV/bin/python" -c "import llmdv.plugin" 2>/dev/null; then
  echo "  ok    pytest plugin imports"
else
  echo "  FAIL  llmdv.plugin does not import" >&2; failed=1
fi

if [ "$INSTALL_S3" = 1 ] || [ "$INSTALL_DEV" = 1 ]; then
  if "$VENV/bin/python" -c "import boto3" 2>/dev/null; then
    echo "  ok    boto3 available"
  else
    echo "  FAIL  boto3 was requested but is not available" >&2; failed=1
  fi
fi

if [ "$INSTALL_DEV" = 1 ]; then
  if "$VENV/bin/ruff" --version >/dev/null 2>&1; then
    echo "  ok    ruff available"
  else
    echo "  FAIL  ruff was requested but is not available" >&2; failed=1
  fi
fi

if [ "$failed" != 0 ]; then
  echo >&2
  echo "install.sh: the install completed but verification failed; see above" >&2
  exit 1
fi

cat <<'NEXT'

Installed. Next:

  source .venv/bin/activate              # or call .venv/bin/llmdv directly

  llmdv --help
  llmdv profile validate profiles/examples/example-two-ingress.yaml

  # against a fleet: copy an example, fill in the placeholders, then
  llmdv profile show <your-profile>.local.yaml --setup     # prints the port-forwards
  llmdv verify --profile <your-profile>.local.yaml

  # offline, no cluster needed
  PYTHONPATH=src .venv/bin/python -m pytest tests/ -q

Docs: README.md, ARCHITECTURE.md, profiles/README.md, COVERAGE.md
NEXT
