#!/usr/bin/env bash
# Run deploy/llm-d-nightly/smoke-step.sh against a stand-in router on a cluster, and check its
# exit status, what it collected and what it left behind for each case it must handle.
#
#   LLMDV_VERSION=<version> LLMDV_IMAGE=<image> FAKE_IMAGE=<image> run-cases.sh
#
# Needs: kubectl pointed at a disposable cluster (kind) that can run both images, yq, python3,
# and llmdv on PATH. Each case runs in its own namespace that enforces the `restricted` Pod
# Security Standard; the namespaces are deleted at the end.
set -uo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
: "${LLMDV_VERSION:?}" "${LLMDV_IMAGE:?}" "${FAKE_IMAGE:?}"
prefix="smoke-e2e-$RANDOM"
work=$(mktemp -d)
failed=0

# Every namespace a run makes carries its prefix as a label, so cleanup finds them all, even
# those made inside $(...).
cleanup() {
  kubectl delete namespace -l "llmdv-smoke-e2e=$prefix" --wait=false >/dev/null 2>&1
  rm -rf "$work"
}
trap cleanup EXIT

# A release checkout as the nightly clones it, with a one-row overlay map and the stand-in's
# profile in place of the real ones.
release="$work/release"
mkdir -p "$release/deploy/llm-d-nightly" "$release/profiles/llm-d/optimized-baseline"
cp "$root/deploy/llm-d-nightly/"*.{sh,py} "$release/deploy/llm-d-nightly/"
cat > "$release/deploy/llm-d-nightly/overlays.yaml" <<'EOF'
lanes:
  optimized-baseline:
    gpu/vllm/gke:
      lane: optimized-baseline-gke-acc-gpu-vllm-x
      profile: optimized-baseline/fake-router
      identity: [accelerator=gpu, provider=gke]
EOF
python3 "$root/tests/e2e_llm_d_nightly/fake_router.py" profile \
  > "$release/profiles/llm-d/optimized-baseline/fake-router.yaml"

namespace() {
  local ns="$prefix-$1"
  kubectl create namespace "$ns" >/dev/null
  kubectl label namespace "$ns" "llmdv-smoke-e2e=$prefix" pod-security.kubernetes.io/enforce=restricted \
    pod-security.kubernetes.io/enforce-version=latest >/dev/null
  echo "$ns"
}

# The router Service as a kustomize standup creates it: <guide>-epp, port 80 named http, in
# front of the Envoy sidecar's 8081. With no defect argument there is nothing behind it.
router() {
  local ns=$1 defect=${2-none} delay=${3-0}
  if [ "$defect" != none ]; then
    kubectl -n "$ns" apply -f - >/dev/null <<EOF
apiVersion: apps/v1
kind: Deployment
metadata: {name: fake-router}
spec:
  replicas: 1
  selector: {matchLabels: {app: fake-router}}
  template:
    metadata: {labels: {app: fake-router}}
    spec:
      securityContext: {runAsNonRoot: true, seccompProfile: {type: RuntimeDefault}}
      containers:
        - name: router
          image: $FAKE_IMAGE
          imagePullPolicy: IfNotPresent
          env: [{name: FAKE_DEFECT, value: "$defect"}, {name: FAKE_DELAY_SECONDS, value: "$delay"}]
          ports: [{containerPort: 8081}]
          readinessProbe: {tcpSocket: {port: 8081}, periodSeconds: 2}
          securityContext: {allowPrivilegeEscalation: false, capabilities: {drop: [ALL]}}
EOF
  fi
  kubectl -n "$ns" apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Service
metadata: {name: optimized-baseline-epp}
spec:
  selector: {app: fake-router}
  ports: [{name: http, port: 80, targetPort: 8081}]
EOF
  if [ "$defect" != none ]; then kubectl -n "$ns" rollout status deploy/fake-router --timeout=300s >/dev/null; fi
}

check() { # <label> <condition description> <test command...>
  local label=$1 what=$2; shift 2
  if "$@"; then echo "  ok    $label: $what"; else echo "  FAIL  $label: $what"; failed=1; fi
}

# Runs the step as the nightly would for overlay gpu/<backend>/<provider>, and records its exit.
step() { # <namespace> <infra provider>
  rm -rf "$work/runner" && mkdir -p "$work/runner"
  env LLMDV_VERSION="$LLMDV_VERSION" LLMDV_IMAGE="$LLMDV_IMAGE" RUNNER_TEMP="$work/runner" \
    LLMDV_READY_TIMEOUT=300 LLMDV_TIMEOUT="${SMOKE_CASE_TIMEOUT:-300}" \
    LLMDBENCH_CICD_NS="$1" LLMDBENCH_CICD_SCENARIO=optimized-baseline \
    LLMDBENCH_CICD_ACCELERATOR=gpu LLMDBENCH_CICD_BACKEND=vllm LLMDBENCH_CICD_CONNECTOR= \
    LLMDBENCH_CICD_OFFLOADING_TARGET= LLMDBENCH_CICD_INFRA_PROVIDER="$2" \
    bash "$release/deploy/llm-d-nightly/smoke-step.sh" > "$work/step.log" 2>&1
  echo $? > "$work/step.exit"
  sed 's/^/        | /' "$work/step.log" | grep -E "llmdv:|warning|PASSED|FAILED" || true
}
exit_is() { [ "$(cat "$work/step.exit")" = "$1" ]; }
outcome() { python3 -c 'import json,glob,sys; o=json.load(open(glob.glob(sys.argv[2]+"/*/outcome.json")[0])); i=o["identity"]; print(json.dumps({k: i[k] for k in ("accelerator", "provider")} if sys.argv[1] == "identity" else o["verification"]["counts"], sort_keys=True))' "$1" "$work/runner/llmdv-runs/runs"; }
counts() { outcome counts; }
leftovers_gone() { # pods and ConfigMaps the step made are gone within 30 s
  for _ in $(seq 1 15); do
    kubectl -n "$1" get pods,configmaps -o name 2>/dev/null | grep -q llmdv-smoke || return 0
    sleep 2
  done
  return 1
}
nothing_made() { ! grep -q "llmdv-smoke" <(kubectl -n "$1" get events -o name 2>/dev/null; kubectl -n "$1" get pods,configmaps -o name 2>/dev/null); }

echo "case 1: a router that serves the profile's contract"
ns=$(namespace good) && router "$ns" ""
step "$ns" gke
check 1 "the step exits 0" exit_is 0
check 1 "the run directory came back, with its dashboard" test -s "$work/runner/llmdv-runs/runs"/*/dashboard.html
check 1 "all nine smoke cases passed" test "$(counts)" = '{"pass": 9}'
check 1 "the run carries the row's identity flags" test "$(outcome identity)" = '{"accelerator": "gpu", "provider": "gke"}'
check 1 "the pod and ConfigMap are removed" leftovers_gone "$ns"

echo "case 2: a router with a streaming defect"
ns=$(namespace defect) && router "$ns" omit_done
step "$ns" gke
check 2 "the step exits 1" exit_is 1
check 2 "the run recorded failures" grep -q '"fail"' <(counts)
check 2 "the pod and ConfigMap are removed" leftovers_gone "$ns"

echo "case 3: an overlay with no profile"
ns=$(namespace skip)
step "$ns" ocp
check 3 "the step exits 0" exit_is 0
check 3 "it says it skipped" grep -q "skipped" "$work/step.log"
check 3 "it created nothing" nothing_made "$ns"

echo "case 4: no router Service in the namespace"
ns=$(namespace noservice)
step "$ns" gke
check 4 "the step exits 0" exit_is 0
check 4 "it warns that the Service is missing" grep -q "integration failed, not a guide failure" "$work/step.log"
check 4 "it created nothing" nothing_made "$ns"

echo "case 5: a router Service with nothing behind it"
ns=$(namespace down) && router "$ns"
step "$ns" gke
check 5 "the step exits 0" exit_is 0
check 5 "it reports llmdv's exit 3 as not a guide failure" grep -q "verification exit 3" "$work/step.log"
check 5 "the pod and ConfigMap are removed" leftovers_gone "$ns"

echo "case 6: timeout finalizes a slow verification before collecting"
ns=$(namespace timeout) && router "$ns" "" 1
SMOKE_CASE_TIMEOUT=8 step "$ns" gke
check 6 "timeout does not blame the guide" exit_is 0
check 6 "finalized interrupted evidence is collected" python3 -c '
import json,sys
from pathlib import Path
root=Path(sys.argv[1]); status=json.loads((root/"integration.json").read_text())
assert status["status"] == "timed_out", status
assert status["verification_exit"] == 6, status
assert status["collection_status"] == "complete", status
run=next((root/"runs").iterdir()); outcome=json.loads((run/"outcome.json").read_text())
assert outcome["finished"] and outcome["verification"]["exit_code"] == 6
assert (run/"dashboard.html").is_file()
' "$work/runner/llmdv-runs"
check 6 "the pod and ConfigMap are removed" leftovers_gone "$ns"

if [ "$failed" -ne 0 ]; then echo "smoke-step e2e: FAILED"; exit 1; fi
echo "smoke-step e2e: all cases passed"
