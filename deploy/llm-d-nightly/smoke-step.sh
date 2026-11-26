#!/usr/bin/env bash
# llm-d-validate smoke tests against a guide's router, from inside the stack's namespace.
#
# Run by llm-d-infra's reusable nightly workflow, after its benchmark and before teardown, from
# a clone of the llm-d-validate release tag (deploy/llm-d-nightly/README.md):
#
#   git clone --depth 1 --branch "v$V" https://github.com/llm-d-incubation/llm-d-validate.git "$D"
#   LLMDV_VERSION="$V" bash "$D/deploy/llm-d-nightly/smoke-step.sh"
#
# It reads only what that job already sets -- LLMDBENCH_CICD_{NS,SCENARIO,ACCELERATOR,BACKEND,
# CONNECTOR,OFFLOADING_TARGET,INFRA_PROVIDER} and RUNNER_TEMP -- and takes the overlay map
# (overlays.yaml, beside it) and the profiles from the checkout it sits in, so the data, this
# script and the image share one version.
#
# The router is reached as llm-d-benchmark reaches it: from a pod in the namespace, through the
# <guide>-epp Service. The runner only drives kubectl (create, apply, wait, exec, cp, logs,
# delete); llmdv itself changes nothing in the cluster.
#
# Exit status: 1 when llmdv found the deployment not behaving as its profile declares, and 0
# otherwise: a pass, a skip (no profile for this overlay), and anything that is not the guide's
# fault (llmdv's exit 2, a profile problem; 3, the environment; a missing Service; a pod that
# does not start; a timeout), which are reported as warnings.
set -uo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
V="${LLMDV_VERSION:?set LLMDV_VERSION to the llm-d-validate release to run}"
IMAGE="${LLMDV_IMAGE:-ghcr.io/llm-d-incubation/llm-d-validate:v$V}"
NS="${LLMDBENCH_CICD_NS:?}"
SCENARIO="${LLMDBENCH_CICD_SCENARIO:?}"
GUIDE="${SCENARIO##*/}"
RUNS="${RUNNER_TEMP:?}/llmdv-runs"
POD="llmdv-smoke-${GITHUB_RUN_ID:-local}-$RANDOM"
TIMEOUT="${LLMDV_TIMEOUT:-900}"
READY_TIMEOUT="${LLMDV_READY_TIMEOUT:-600}"

note() {
  echo "llmdv: $*"
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then echo "- $*" >> "$GITHUB_STEP_SUMMARY"; fi
}
warn() {
  echo "::warning::llmdv: $*"
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then echo "- **warning:** $*" >> "$GITHUB_STEP_SUMMARY"; fi
}

# 1. The overlay this lane deployed, and its row in the map: the profile (relative to
#    profiles/llm-d/) and the --identity flags its README lists. No row: skip.
overlay=$(printf '%s/' "${LLMDBENCH_CICD_ACCELERATOR:-}" "${LLMDBENCH_CICD_BACKEND:-}" \
  "${LLMDBENCH_CICD_CONNECTOR:-}" "${LLMDBENCH_CICD_OFFLOADING_TARGET:-}" \
  "${LLMDBENCH_CICD_INFRA_PROVIDER:-}" | tr -s '/' | sed 's#^/##; s#/$##')
row=".lanes.\"$SCENARIO\".\"$overlay\""
profile=$(yq "$row.profile // \"\"" "$HERE/overlays.yaml" 2>/dev/null || true)
note "scenario \`$SCENARIO\`, overlay \`$overlay\`, profile \`${profile:-none}\`"
if [ -z "$profile" ]; then
  note "no llm-d-validate profile for this overlay; skipped"
  exit 0
fi
identity=""
for flag in $(yq "$row.identity[]" "$HERE/overlays.yaml"); do
  case "$flag" in
    *[!A-Za-z0-9=._/-]*) warn "unexpected identity flag in overlays.yaml: $flag; not a guide failure"; exit 0 ;;
  esac
  identity+=" --identity $flag"
done

# 2. The router Service and its HTTP port, chosen as llm-d-benchmark's find_kustomize_endpoint
#    chooses: port 80, else a port named http/https, else the one targeting 8081 (the EPP's
#    Envoy sidecar).
SVC="${GUIDE}-epp"
PORT=$(kubectl -n "$NS" get svc "$SVC" -o json 2>/dev/null | python3 -c '
import json, sys
ports = json.load(sys.stdin).get("spec", {}).get("ports", [])
pick = (
    next((p for p in ports if p.get("port") == 80), None)
    or next((p for p in ports if p.get("name") in ("http", "https")), None)
    or next((p for p in ports if str(p.get("targetPort")) == "8081"), None)
    or (ports[0] if ports else None)
)
print(pick["port"] if pick else "")' 2>/dev/null || true)
if [ -z "$PORT" ]; then
  warn "Service $SVC not found in the namespace; not a guide failure"
  exit 0
fi
URL="http://$SVC:$PORT"
note "router \`$URL\`"

# 3. The profile as one self-contained file, delivered as a ConfigMap.
command -v llmdv >/dev/null || python3 -m pip install -q "llm-d-validate==$V"
mkdir -p "$RUNS"
if ! llmdv profile show "$ROOT/profiles/llm-d/$profile.yaml" --yaml > "$RUNS/profile.yaml"; then
  warn "could not export profile $profile; not a guide failure"
  exit 0
fi
kubectl -n "$NS" create configmap "$POD" --from-file=profile.yaml="$RUNS/profile.yaml" >/dev/null

# shellcheck disable=SC2329 # invoked by the EXIT trap below
cleanup() {
  kubectl -n "$NS" delete pod "$POD" --wait=false --ignore-not-found >/dev/null 2>&1
  kubectl -n "$NS" delete configmap "$POD" --ignore-not-found >/dev/null 2>&1
}
trap cleanup EXIT

# 4. One pod, run as deploy/base/cronjob.yaml runs llmdv: non-root, read-only root filesystem,
#    no capabilities, no Kubernetes API token. It runs verify, records the exit code, and waits
#    to be collected.
kubectl -n "$NS" apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: $POD
  labels: {app.kubernetes.io/name: llmdv, llm-d-benchmark/ephemeral: "true"}
spec:
  restartPolicy: Never
  terminationGracePeriodSeconds: 5
  activeDeadlineSeconds: $((READY_TIMEOUT + TIMEOUT + 300))
  automountServiceAccountToken: false
  securityContext: {runAsNonRoot: true, seccompProfile: {type: RuntimeDefault}}
  containers:
    - name: llmdv
      image: $IMAGE
      command: ["sh", "-c"]
      args:
        - >-
          llmdv verify --profile /etc/llmdv/profile.yaml$identity
          --test-set smoke --pod-logs off --analyze --local-output /work/runs;
          echo \$? > /work/exit; sleep $((TIMEOUT + 300))
      env: [{name: LLMDV_ROUTER_URL, value: "$URL"}]
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities: {drop: [ALL]}
      resources: {requests: {cpu: 100m, memory: 256Mi}, limits: {memory: 1Gi}}
      volumeMounts:
        - {name: profile, mountPath: /etc/llmdv, readOnly: true}
        - {name: work, mountPath: /work}
        - {name: tmp, mountPath: /tmp}
  volumes:
    - {name: profile, configMap: {name: $POD}}
    - {name: work, emptyDir: {}}
    - {name: tmp, emptyDir: {}}
EOF

if ! kubectl -n "$NS" wait --for=condition=Ready "pod/$POD" --timeout="${READY_TIMEOUT}s" >/dev/null 2>&1; then
  kubectl -n "$NS" describe pod "$POD" | tail -15
  warn "the llmdv pod did not start within ${READY_TIMEOUT}s; not a guide failure"
  exit 0
fi

# 5. Wait for verify to finish, then collect its run directory and console.
rc=""
for _ in $(seq 1 $((TIMEOUT / 5))); do
  rc=$(kubectl -n "$NS" exec "$POD" -- cat /work/exit 2>/dev/null || true)
  [ -n "$rc" ] && break
  sleep 5
done
kubectl -n "$NS" logs "$POD" > "$RUNS/console.txt" 2>&1 || true
kubectl -n "$NS" cp "$POD:/work/runs" "$RUNS/runs" >/dev/null 2>&1 || warn "could not copy the run directory"
tail -25 "$RUNS/console.txt"

if [ -z "$rc" ]; then
  warn "llmdv did not finish within ${TIMEOUT}s; not a guide failure"
  exit 0
fi
note "llmdv exited $rc; results in the uploaded artifact"
case "$rc" in
  0) exit 0 ;;
  1) exit 1 ;; # the deployment did not behave as its profile declares
  *)
    warn "llmdv exited $rc (2: profile, 3: environment); not a guide failure"
    exit 0
    ;;
esac
