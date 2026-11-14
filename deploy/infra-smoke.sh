#!/usr/bin/env bash
# Start a one-off infrastructure check using the CronJob's pod settings.
# Requires bash, jq, and kubectl (or --cli oc). Uses the CLI's current context.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: infra-smoke.sh -n NAMESPACE [--cronjob llmdv] [--cli kubectl]

Create a Job that checks non-root execution, writable storage and S3 publication.
Prints the Job name; follow its logs and completion with your cluster CLI.
The fleet profile is not read, and the CronJob's schedule is not changed.
EOF
}

fail() {
    printf 'infra-smoke.sh: %s\n' "$1" >&2
    exit 2
}

namespace=""
cronjob="llmdv"
cli="kubectl"
while [ $# -gt 0 ]; do
    case "$1" in
        -h | --help) usage; exit 0 ;;
        -n | --namespace | --cronjob | --cli)
            [ $# -ge 2 ] && [ -n "$2" ] && [[ "$2" != -* ]] || fail "$1 requires a value"
            case "$1" in
                -n | --namespace) namespace="$2" ;;
                --cronjob) cronjob="$2" ;;
                --cli) cli="$2" ;;
            esac
            shift 2 ;;
        *) fail "unknown option '$1' (try --help)" ;;
    esac
done
[ -n "$namespace" ] || fail "-n NAMESPACE is required"

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
job="llmdv-infra-$(date -u +%Y%m%d%H%M%S)-$$"

# Finish rendering before creating anything: a missing llmdv container must never start
# the unmodified functional-test command by accident.
job_json=$(
    "$cli" -n "$namespace" create job "$job" --from="cronjob/$cronjob" --dry-run=client -o json |
        jq -e --rawfile probe "$script_dir/infra-smoke.py" '
            if ([.spec.template.spec.containers[] | select(.name == "llmdv")] | length) != 1
            then error("expected exactly one llmdv container in the CronJob")
            else . end |
            .metadata.labels["llmdv-run-kind"] = "infrastructure-smoke" |
            .spec.activeDeadlineSeconds = 300 |
            .spec.template.metadata.labels["llmdv-run-kind"] = "infrastructure-smoke" |
            (.spec.template.spec.containers[] | select(.name == "llmdv")) |=
                (.command = ["python", "-c"] | .args = [$probe])
        '
)
printf '%s\n' "$job_json" | "$cli" -n "$namespace" create -f - >/dev/null
printf '%s\n' "$job"
