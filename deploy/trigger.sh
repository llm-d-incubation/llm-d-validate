#!/bin/sh
# trigger.sh: start a one-off llmdv run from the CronJob's own pod spec, e.g. after a rollout.
#
#   trigger.sh -n NAMESPACE [--cronjob llmdv] [--cli kubectl] [--trigger rollout] [--id ID]
#              [--profile-file PATH]
#              [-- llmdv verify arguments...]
#
# The Job is the CronJob's job template with only its arguments replaced: those after `--`,
# preceded by `--label trigger=<trigger> --label <trigger>=<id>`. Prints the Job's name; wait on
# it with the caller's own tooling. Deciding the rollout is ready is the caller's job too.
#
# Needs kubectl permissions: get cronjobs, create jobs and configmaps (the llmdv-trigger Role).
set -eu

namespace=""
cronjob="llmdv"
trigger="rollout"
id=""
cli="kubectl"
profile_file=""
while [ $# -gt 0 ]; do
    case "$1" in
        -n | --namespace) namespace="$2"; shift 2 ;;
        --cronjob) cronjob="$2"; shift 2 ;;
        --cli) cli="$2"; shift 2 ;;
        --trigger) trigger="$2"; shift 2 ;;
        --id) id="$2"; shift 2 ;;
        --profile-file) profile_file="$2"; shift 2 ;;
        --) shift; break ;;
        -h | --help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "trigger.sh: unknown option '$1' (try --help)" >&2; exit 2 ;;
    esac
done
if [ -z "$namespace" ]; then
    echo "trigger.sh: -n NAMESPACE is required" >&2
    exit 2
fi
case "$cli" in
    kubectl | oc) ;;
    *) echo "trigger.sh: --cli must be kubectl or oc" >&2; exit 2 ;;
esac
if [ -n "$profile_file" ]; then
    if [ ! -f "$profile_file" ] || [ ! -r "$profile_file" ]; then
        echo "trigger.sh: profile file is missing or unreadable: $profile_file" >&2
        exit 2
    fi
    if ! grep -Eq '^vantage:[[:space:]]*in-cluster([[:space:]]*#.*)?[[:space:]]*$' "$profile_file"; then
        echo "trigger.sh: profile file must declare top-level vantage: in-cluster" >&2
        exit 2
    fi
fi
id="${id:-$(date -u +%Y%m%d%H%M%S)}"

# Job names are at most 63 characters; a longer one keeps its start plus a checksum of the whole.
suffix=$(printf '%s' "$id" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-' |
    sed 's/^-*//; s/-*$//')
job="${cronjob}-${trigger}-${suffix:-run}"
if [ "${#job}" -gt 63 ]; then
    sum=$(printf '%s' "$job" | cksum | cut -d' ' -f1)
    job="$(printf '%s' "$job" | cut -c1-52 | sed 's/-*$//')-${sum}"
fi

json_string() {
    printf '"%s"' "$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g')"
}
args="$(json_string --label),$(json_string "trigger=$trigger")"
args="$args,$(json_string --label),$(json_string "$trigger=$id")"
for arg in "$@"; do
    args="$args,$(json_string "$arg")"
done
patch="[{\"op\":\"add\",\"path\":\"/spec/template/spec/containers/0/args\",\"value\":[$args]}]"

if [ -n "$profile_file" ]; then
    if ! configmap=$(
        "$cli" -n "$namespace" create configmap "$job" \
            --from-file="profile.yaml=$profile_file" --dry-run=client -o json
    ); then
        echo "trigger.sh: could not render ConfigMap for Job $job" >&2
        exit 1
    fi
fi

job_json=$("$cli" -n "$namespace" create job "$job" --from="cronjob/$cronjob" --dry-run=client -o json |
    "$cli" patch --local -f - --type=json -p "$patch" -o json)
if [ -n "$profile_file" ]; then
    volume_patch="{\"spec\":{\"template\":{\"spec\":{\"volumes\":[{\"name\":\"profile\",\"configMap\":{\"\$patch\":\"replace\",\"name\":\"$job\"}}]}}}}"
    job_json=$(printf '%s\n' "$job_json" |
        "$cli" patch --local -f - --type=strategic -p "$volume_patch" -o json)
    if ! uid=$(
        printf '%s\n' "$job_json" |
            "$cli" -n "$namespace" create -f - -o jsonpath='{.metadata.uid}'
    ) || [ -z "$uid" ]; then
        echo "trigger.sh: Job $job may have been created, but its UID was not returned" >&2
        echo "trigger.sh: delete it with: $cli -n $namespace delete job $job" >&2
        exit 1
    fi
else
    printf '%s\n' "$job_json" | "$cli" -n "$namespace" create -f - >/dev/null
fi

if [ -n "$profile_file" ]; then
    configmap_patch="{\"immutable\":true,\"metadata\":{\"labels\":{\"app.kubernetes.io/name\":\"llmdv\"},\"ownerReferences\":[{\"apiVersion\":\"batch/v1\",\"kind\":\"Job\",\"name\":\"$job\",\"uid\":\"$uid\"}]}}"
    if ! configmap=$(printf '%s\n' "$configmap" |
        "$cli" patch --local -f - --type=merge -p "$configmap_patch" -o json
    ); then
        echo "trigger.sh: could not create ConfigMap for Job $job" >&2
        echo "trigger.sh: delete it with: $cli -n $namespace delete job $job" >&2
        exit 1
    fi
    if ! printf '%s\n' "$configmap" | "$cli" -n "$namespace" create -f - >/dev/null; then
        echo "trigger.sh: could not create ConfigMap for Job $job" >&2
        echo "trigger.sh: delete it with: $cli -n $namespace delete job $job" >&2
        exit 1
    fi
fi
echo "$job"
