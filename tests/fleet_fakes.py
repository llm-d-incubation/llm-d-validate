"""Doubles for the fleet suite: an executable `kubectl` over a JSON state, and a healthy fleet.

The kubectl double is a real executable on PATH, as the Claude Code doubles are, so llmdv's own
adapter runs unchanged: argument building, the verb allowlist, exit codes and stderr parsing.
It answers `get`, `logs`, `version` and `config` from a state file, refuses every other verb,
and records each invocation so a test can prove which verbs were used.

`fleet_state(defects)` is a small, healthy fleet with generic names; each defect breaks exactly
one invariant the fleet cases exist to catch.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import textwrap
import time
from copy import deepcopy
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from llmdv.cli.verify_cmd import _plugin_is_registered
from tests.fakes import _Server

SRC = Path(__file__).resolve().parents[1] / "src"
FLEET_SUITE = SRC / "llmdv" / "suites" / "fleet"
NAMESPACE = "fleet"

_KUBECTL = r"""
import json, os, sys

argv = sys.argv[1:]
with open(os.environ["LLMDV_FAKE_KUBE_CALLS"], "a") as handle:
    handle.write(json.dumps(argv) + "\n")
state = json.load(open(os.environ["LLMDV_FAKE_KUBE_STATE"]))

flags, positional, index = {}, [], 0
while index < len(argv):
    word = argv[index]
    if word.startswith("--") and "=" in word:
        key, value = word[2:].split("=", 1)
        flags[key] = value
    elif word in ("-n", "-l", "-o", "-c", "--kubeconfig", "--context", "--field-selector", "--raw"):
        flags[word.lstrip("-")] = argv[index + 1]
        index += 1
    else:
        positional.append(word)
    index += 1

def die(message):
    sys.stderr.write(message + "\n")
    raise SystemExit(1)

def out(text):
    sys.stdout.write(text + "\n")

verb, rest = positional[0], positional[1:]
if verb == "version":
    out("Client Version: fake")
    raise SystemExit
if verb == "config":
    out("fake-context")
    raise SystemExit
if verb == "logs":
    pod = rest[0]
    container = flags.get("c")
    logs = state.get("logs", {})
    key = pod + "/" + str(container) + ("#previous" if "--previous" in argv else "")
    if key in state.get("log_errors", {}):
        die(state["log_errors"][key])
    text = logs.get(key)
    if text is None:
        die('Error from server (NotFound): pods "%s" not found' % pod)
    sys.stdout.write(text)
    raise SystemExit
if verb != "get":
    die("fake kubectl refuses verb " + verb)
if "raw" in flags:
    die('Error from server (NotFound): pods "llmdv-access-probe" not found')

kind = rest[0]
name = rest[1] if len(rest) > 1 else None
if kind in state.get("forbidden", []):
    die('Error from server (Forbidden): %s is forbidden: User "fake" cannot list resource' % kind)
if name and kind + "/" + name in state.get("forbidden_names", []):
    message = 'Error from server (Forbidden): %s/%s is forbidden: cannot get resource'
    die(message % (kind, name))
namespace = flags.get("n", "default")
items = state.get("objects", {}).get(namespace, {}).get(kind, [])
if name:
    found = [item for item in items if item["metadata"]["name"] == name]
    if not found:
        die('Error from server (NotFound): %s "%s" not found' % (kind, name))
    if flags.get("o", "").startswith("jsonpath"):
        spec = found[0].get("spec", {})
        out("\n".join(c["name"] for c in spec.get("containers", [])))
    else:
        out(json.dumps(found[0]))
    raise SystemExit
if "l" in flags:
    wanted = dict(pair.split("=", 1) for pair in flags["l"].split(","))
    items = [
        item for item in items
        if all(item["metadata"].get("labels", {}).get(k) == v for k, v in wanted.items())
    ]
if flags.get("o") == "name":
    out("\n".join(kind + "/" + item["metadata"]["name"] for item in items))
else:
    out(json.dumps({"kind": "List", "items": items}))
"""


def fake_kubectl(directory: Path, state: dict, name: str = "kubectl") -> dict[str, str]:
    """Install the double in `directory`; return the environment that puts it on PATH alone."""
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / name
    binary.write_text(f"#!{sys.executable}\n" + textwrap.dedent(_KUBECTL))
    binary.chmod(0o700)
    state_path = directory / "state.json"
    state_path.write_text(json.dumps(state))
    calls = directory / "calls.jsonl"
    calls.write_text("")
    # /usr/local/bin is left off on purpose: a real kubectl there must never be reached.
    return {
        "PATH": f"{directory}:/usr/bin:/bin",
        "LLMDV_FAKE_KUBE_STATE": str(state_path),
        "LLMDV_FAKE_KUBE_CALLS": str(calls),
    }


def kubectl_calls(env: dict[str, str]) -> list[list[str]]:
    text = Path(env["LLMDV_FAKE_KUBE_CALLS"]).read_text()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# --------------------------------------------------------------------- a healthy fleet

ENGINE_ARGS = [
    "--model=example/model",
    "--served-model-name=example/model",
    "--block-size=64",
    "--no-log-error-stack",
    '--kv-events-config={"topic":"kv@$(POD_IP):8000@example/model"}',
]


def _meta(name: str, labels: dict | None = None, annotations: dict | None = None) -> dict:
    meta: dict = {"name": name, "namespace": NAMESPACE, "labels": labels or {}}
    if annotations:
        meta["annotations"] = annotations
    return meta


def _deployment(name: str, labels: dict, container: str, args: list, env: list) -> dict:
    return {
        "kind": "Deployment",
        "metadata": _meta(name, labels),
        "spec": {
            "replicas": 1,
            "template": {
                "metadata": {"labels": labels},
                "spec": {"containers": [{"name": container, "args": args, "env": env}]},
            },
        },
    }


def _slices(service: str, pods: list[str]) -> list[dict]:
    """One slice per address family, as a dual-stack Service has, each listing every pod."""
    return [
        {
            "kind": "EndpointSlice",
            "metadata": _meta(f"{service}-{family}", {"kubernetes.io/service-name": service}),
            "addressType": family,
            "endpoints": [
                {
                    "addresses": [
                        f"10.0.0.{index + 1}" if family == "IPv4" else f"fd00::{index + 1}"
                    ],
                    "conditions": {"ready": True},
                    "targetRef": {"kind": "Pod", "name": pod},
                }
                for index, pod in enumerate(pods)
            ]
            + [
                {
                    "addresses": ["10.0.0.99" if family == "IPv4" else "fd00::99"],
                    "conditions": {"ready": False},
                    "targetRef": {"kind": "Pod", "name": "router-standby"},
                }
            ],
        }
        for family in ("IPv4", "IPv6")
    ]


def _pod(name: str, labels: dict, container: str) -> dict:
    return {
        "kind": "Pod",
        "metadata": _meta(name, labels),
        "spec": {"containers": [{"name": container}]},
        "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]},
    }


def _configmap(name: str, text: str) -> dict:
    return {"kind": "ConfigMap", "metadata": _meta(name), "data": {"config.yaml": text}}


def _pool(name: str, selector: dict) -> dict:
    return {"kind": "InferencePool", "metadata": _meta(name), "spec": {"selector": selector}}


def _endpoints(name: str, pods: list[str]) -> dict:
    return {
        "kind": "Endpoints",
        "metadata": _meta(name),
        "subsets": [
            {
                "addresses": [
                    {"ip": f"10.0.0.{index + 1}", "targetRef": {"kind": "Pod", "name": pod}}
                    for index, pod in enumerate(pods)
                ]
            }
        ],
    }


CONFIG_DEFECTS = (
    "dev_mode",
    "error_stacks",
    "prefix_caching_off",
    "edge_open",
    "identity_mismatch",
    "block_size_mismatch",
    "objective_priority",
    "objective_missing",
    "route_timeout",
    "two_routers",
    "pool_overlap",
    "engines_relabelled",
)


def fleet_state(defects: tuple[str, ...] = ()) -> dict:
    engine_labels = {"app": "engine"}
    engine_env = [{"name": "HOME", "value": "/tmp"}]
    engine_args = list(ENGINE_ARGS)
    if "dev_mode" in defects:
        engine_env.append({"name": "VLLM_SERVER_DEV_MODE", "value": "1"})
    if "error_stacks" in defects:
        engine_args.remove("--no-log-error-stack")
        engine_args.append("--log-error-stack")
    if "prefix_caching_off" in defects:
        engine_args.append("--no-enable-prefix-caching")
    if "engines_relabelled" in defects:
        engine_labels = {"app": "engine-v2"}
    router_text = "modelName: example/model\nblockSizeTokens: 64\n"
    if "identity_mismatch" in defects:
        router_text = "modelName: example/other\nblockSizeTokens: 64\n"
    if "block_size_mismatch" in defects:
        router_text = "modelName: example/model\nblockSizeTokens: 16\n"
    edge_text = "strip_request_fields: [kv_transfer_params, vllm_xargs]\n"
    if "edge_open" in defects:
        edge_text = "strip_request_fields: []\n"
    objectives = [
        {
            "kind": "InferenceObjective",
            "metadata": _meta(name),
            "spec": {"priority": priority, "poolRef": {"name": "pool"}},
        }
        for name, priority in (("premium", 80), ("standard", 40))
    ]
    if "objective_priority" in defects:
        objectives[0]["spec"]["priority"] = 40
    if "objective_missing" in defects:
        objectives = objectives[1:]
    timeout = "30s" if "route_timeout" in defects else "45m"
    routers = ["router-a", "router-b"] if "two_routers" in defects else ["router-a"]
    pools = [_pool("pool", {"app": "engine"})]
    if "pool_overlap" in defects:
        pools.append(_pool("canary-pool", {"app": "engine"}))
    pods = [_pod(f"engine-{index}", engine_labels, "vllm") for index in range(2)]
    pods.append(_pod("router-a", {"app": "router"}, "router"))
    objects = {
        "deployments": [
            _deployment("engine", engine_labels, "vllm", engine_args, engine_env),
            _deployment("router", {"app": "router"}, "router", ["--config=/etc/router"], []),
        ],
        "configmaps": [_configmap("router-config", router_text), _configmap("edge", edge_text)],
        "inferenceobjectives": objectives,
        "inferencepools": pools,
        "routes": [
            {
                "kind": "Route",
                "metadata": _meta(
                    "public",
                    {"app": "edge"},
                    {"edge.example.com/timeout": timeout},
                ),
            }
        ],
        "endpoints": [_endpoints("router-epp", routers)],
        "endpointslices": _slices("router-epp", routers),
        "pods": pods,
    }
    return {"objects": {NAMESPACE: deepcopy(objects)}, "logs": fleet_logs(defects)}


FLEET_CONFIG_YAML = r"""
environment:
  namespace: fleet
fleet:
  config:
    - id: engine_dev_mode_off
      check: settings
      objects: {kind: deployments, selector: app=engine}
      containers: [vllm]
      forbid: ['^VLLM_SERVER_DEV_MODE=(1|true|True)$']
    - id: error_stacks_off
      check: settings
      objects: {kind: deployments, selector: app=engine}
      containers: [vllm]
      require: ['--no-log-error-stack']
      forbid: ['--log-error-stack\b']
    - id: prefix_caching_on
      check: settings
      when: cache_reuse
      objects: {kind: deployments, selector: app=engine}
      forbid: ['--no-enable-prefix-caching']
    - id: edge_strips_internal_fields
      check: settings
      ingress: fake
      objects: {kind: configmaps, name: edge}
      view: data
      require: ['strip_request_fields:.*kv_transfer_params', 'strip_request_fields:.*vllm_xargs']
    - id: kv_events_identity
      check: values_agree
      sources:
        - objects: {kind: deployments, selector: app=engine}
          pattern: '"topic":"kv@[^@]*@([^"]+)"'
        - objects: {kind: deployments, selector: app=engine}
          pattern: '--served-model-name[ =](\S+)'
        - objects: {kind: configmaps, name: router-config}
          view: data
          pattern: 'modelName:\s*(\S+)'
    - id: kv_block_size
      check: values_agree
      expect_capability: cache_reuse.block_size
      sources:
        - objects: {kind: deployments, selector: app=engine}
          containers: [vllm]
          pattern: '--block-size[ =](\d+)'
          default: "16"
        - objects: {kind: configmaps, name: router-config}
          view: data
          pattern: 'blockSizeTokens:\s*(\d+)'
    - id: objective_premium
      check: field_values
      objects: {kind: inferenceobjectives, name: premium}
      path: [spec, priority]
      equals: 80
    - id: route_timeout
      check: field_values
      objects: {kind: routes, selector: app=edge}
      path: [metadata, annotations, edge.example.com/timeout]
      at_least_seconds: 2700
    - id: single_active_router
      check: ready_endpoints
      service: router-epp
      count: 1
    - id: pool_exclusive
      check: selector_exclusive
      objects: {kind: inferencepools, name: pool}
      selector_path: [spec, selector]
"""

# Which declared rule each defect breaks, as (case id, variant).
CONFIG_TARGETS = {
    "dev_mode": ("config.settings", "engine_dev_mode_off"),
    "error_stacks": ("config.settings", "error_stacks_off"),
    "prefix_caching_off": ("config.settings", "prefix_caching_on"),
    "edge_open": ("config.settings", "edge_strips_internal_fields"),
    "identity_mismatch": ("config.values_agree", "kv_events_identity"),
    "block_size_mismatch": ("config.values_agree", "kv_block_size"),
    "objective_priority": ("config.field_values", "objective_premium"),
    "objective_missing": ("config.field_values", "objective_premium"),
    "route_timeout": ("config.field_values", "route_timeout"),
    "two_routers": ("config.ready_endpoints", "single_active_router"),
    "pool_overlap": ("config.selector_exclusive", "pool_exclusive"),
}

CACHE_REUSE_YAML = """
capabilities:
  cache_reuse: {min_ratio: 0.5, single_replica: true, block_size: 64}
"""


def fleet_profile(path: Path, *blocks: str, ingress_extra: str = "") -> Path:
    """A one-ingress profile whose base URL is never contacted, plus the given YAML blocks.

    `ingress_extra` is YAML indented to sit inside the ingress, such as a `logs:` block.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        textwrap.dedent(
            """
            schema: llmdv.profile/1
            name: fleet-fake
            model: {public_id: fake/Model-1B}
            ingresses:
              - id: fake
                kind: vllm
                base_url: http://127.0.0.1:9
                surfaces: [chat_completions]
            """
        )
        + ingress_extra
        + "".join(blocks)
    )
    return path


def run_fleet_suite(
    profile: Path, run_dir: Path, env: dict[str, str], *extra: str
) -> tuple[subprocess.CompletedProcess, list[dict]]:
    """Run the fleet suite with the llmdv plugin, with only the given PATH and variables."""
    argv = [
        sys.executable,
        "-m",
        "pytest",
        str(FLEET_SUITE),
        "-q",
        "--no-header",
        "-p",
        "no:cacheprovider",
        "--llmdv-profile",
        str(profile),
        "--llmdv-run-dir",
        str(run_dir),
        *extra,
    ]
    if not _plugin_is_registered():
        argv[3:3] = ["-p", "llmdv.plugin"]
    result = subprocess.run(  # noqa: S603 - fixed interpreter and args
        argv,
        capture_output=True,
        text=True,
        cwd=SRC.parent,
        env={"PYTHONPATH": str(SRC), **env},
    )
    results = run_dir / "results.jsonl"
    records = (
        [json.loads(line) for line in results.read_text().splitlines() if line.strip()]
        if results.is_file()
        else []
    )
    return result, records


def verdicts(records: list[dict]) -> dict[tuple[str, str | None], str]:
    return {(r["case_id"], r.get("variant")): r["outcome"] for r in records if r.get("case_id")}


def no_kubectl_env(directory: Path) -> dict[str, str]:
    """An environment whose PATH holds no kubectl at all."""
    directory.mkdir(parents=True, exist_ok=True)
    return {"PATH": str(directory)}


# --------------------------------------------------------------------- a metrics source


class FakePrometheus(_Server):
    """GET /api/v1/query over a fixed expression -> series table, as a real HTTP server.

    Any other method or path is refused and recorded, so a test can prove the client is
    read-only. `token` requires `Authorization: Bearer <token>`. `answer` may compute series
    for an expression the table lacks, as a live router's counters would.
    """

    def __init__(self, series: dict, token: str | None = None, answer=None, warnings=None) -> None:
        self.requests: list[tuple[str, str, str | None]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _send(self, code: int, body: dict) -> None:
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _refuse(self) -> None:
                owner.requests.append((self.command, self.path, None))
                self._send(405, {"status": "error", "errorType": "method", "error": "refused"})

            do_POST = do_PUT = do_DELETE = do_PATCH = _refuse  # noqa: N815

            def do_GET(self):  # noqa: N802 -- BaseHTTPRequestHandler API
                parts = urlsplit(self.path)
                expr = (parse_qs(parts.query).get("query") or [None])[0]
                owner.requests.append(("GET", parts.path, expr))
                if parts.path != "/api/v1/query":
                    self._send(404, {"status": "error", "errorType": "not_found", "error": "no"})
                    return
                if token and self.headers.get("Authorization") != f"Bearer {token}":
                    self._send(401, {"status": "error", "errorType": "auth", "error": "no"})
                    return
                if expr is None or "BAD(" in expr:
                    self._send(400, {"status": "error", "errorType": "bad_data", "error": "parse"})
                    return
                if expr == "vector(1)":
                    rows = [({}, 1)]
                elif expr in series:
                    rows = series[expr]
                else:
                    rows = answer(expr) if answer else []
                result = [
                    {"metric": labels, "value": [time.time(), str(value)]} for labels, value in rows
                ]
                body = {"status": "success", "data": {"resultType": "vector", "result": result}}
                if warnings:
                    body["warnings"] = warnings
                self._send(200, body)

        self._serve(Handler)


METRIC_DEFECTS = {
    # defect: (case id, variant, expression, broken series)
    "decode_local_prefill": (
        "metrics.bounds",
        "decode_no_local_prefill",
        "decode_local_ratio",
        [({}, 0.05)],
    ),
    "pool_short": ("metrics.bounds", "pool_complete", "ready_endpoints", [({}, 3)]),
    "events_skipped": (
        "metrics.bounds",
        "kv_events_skipped",
        "increase(stores_skipped[1h])",
        [({}, 3)],
    ),
    "events_stalled": (
        "metrics.bounds",
        "kv_index_moving",
        "rate(index_admissions[5m])",
        [({}, 0)],
    ),
    "series_missing": ("metrics.bounds", "kv_index_moving", "rate(index_admissions[5m])", []),
    "render_drift": ("metrics.agree", "render_token_parity", "engine_prompt_tokens", [({}, 1100)]),
    "index_stale": (
        "metrics.agree",
        "router_engine_parity",
        "router_predicted_ratio",
        [({}, 0.95)],
    ),
    "subscriber_missing": (
        "metrics.agree",
        "kv_subscribers_cover",
        "router_subscribers",
        [({}, 3)],
    ),
    "kv_short_rank": (
        "metrics.spread",
        "kv_capacity_uniform",
        "kv_blocks_by_pod",
        [({"pod": f"p{i}", "blocks": b}, 1) for i, b in enumerate((1000, 1000, 990, 700))],
    ),
}


def metric_series(defects: tuple[str, ...] = ()) -> dict:
    series = {
        "decode_local_ratio": [({}, 0)],
        "ready_endpoints": [({}, 4)],
        "rate(index_admissions[5m])": [({}, 2.5)],
        "router_prompt_tokens": [({}, 1000)],
        "engine_prompt_tokens": [({}, 1005)],
        "router_predicted_ratio": [({}, 0.81)],
        "engine_reuse_ratio": [({}, 0.80)],
        "router_subscribers": [({}, 4)],
        "engine_publishers": [({}, 4)],
        "kv_blocks_by_pod": [
            ({"pod": f"p{i}", "blocks": b}, 1) for i, b in enumerate((1000, 1000, 990, 980))
        ],
    }
    for defect in defects:
        _, _, expr, broken = METRIC_DEFECTS[defect]
        series[expr] = broken
    return series


FLEET_METRICS_YAML = r"""
fleet:
  metrics:
    - id: decode_no_local_prefill
      check: bounds
      query: decode_local_ratio
      max: 0.000001
    - id: pool_complete
      check: bounds
      query: ready_endpoints
      min: 4
      max: 4
    - id: kv_events_skipped
      check: bounds
      query: increase(stores_skipped[1h])
      max: 0
      absent_is_zero: true
    - id: kv_index_moving
      check: bounds
      query: rate(index_admissions[5m])
      min: 0.000001
    - id: render_token_parity
      check: agree
      left: router_prompt_tokens
      right: engine_prompt_tokens
      relative_tolerance: 0.01
    - id: router_engine_parity
      check: agree
      left: router_predicted_ratio
      right: engine_reuse_ratio
      absolute_tolerance: 0.02
    - id: kv_subscribers_cover
      check: agree
      left: router_subscribers
      right: engine_publishers
      relation: at_least
      absolute_tolerance: 0
    - id: kv_capacity_uniform
      check: spread
      query: kv_blocks_by_pod
      label: blocks
      min_ratio: 0.85
      min_series: 4
"""


def metrics_environment(url: str, key_env: str | None = None) -> str:
    auth = f"\n    auth: {{type: bearer, key_env: {key_env}}}" if key_env else ""
    return f"\nenvironment:\n  metrics:\n    url: {url}{auth}\n"


# --------------------------------------------------------------------- header -> label


class FakeObjectiveRouter(_Server):
    """A chat ingress that counts each request by fairness id and priority, as a router does.

    `x-objective` (or the alternate `x-alt-objective`) names an objective; `x-fairness` names
    the flow. Unknown or absent objectives count at priority 0; a request with neither header
    counts under the default fairness id and priority, as a gateway's team traffic does.
    `answer` serves the counters to a FakePrometheus for
    `sum by (priority) (requests_total{fairness_id="..."})`.
    """

    PRIORITIES = {"premium": "80", "band-1": "60"}

    def __init__(
        self,
        defect: str | None = None,
        default_fairness: str = "default",
        default_priority: str = "0",
    ) -> None:
        self.counts: dict[tuple[str, str], int] = {}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):  # noqa: N802 -- BaseHTTPRequestHandler API
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                objective = self.headers.get("x-objective")
                if defect != "alt_spelling_ignored":
                    objective = objective or self.headers.get("x-alt-objective")
                fairness = self.headers.get("x-fairness")
                # Traffic with no flow of its own is the gateway's team, at the team's priority.
                priority = owner.PRIORITIES.get(objective or "", "0")
                if not (fairness or objective):
                    priority = default_priority
                if defect == "objective_ignored" and objective:
                    priority = "0"
                fairness = fairness or default_fairness
                key = (fairness, priority)
                owner.counts[key] = owner.counts.get(key, 0) + 1
                raw = json.dumps(
                    {
                        "id": "chatcmpl-1",
                        "object": "chat.completion",
                        "model": "fake/Model-1B",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": "ok"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self._serve(Handler)

    def answer(self, expr: str) -> list:
        found = re.search(r'fairness_id="([^"]+)"', expr)
        if not found:
            return []
        return [
            ({"priority": priority}, count)
            for (fairness, priority), count in sorted(self.counts.items())
            if fairness == found.group(1)
        ]


HEADER_LABEL_YAML = r"""
fleet:
  metrics:
    - id: objective_premium
      check: header_label
      ingress: fake
      headers: {x-objective: premium, x-fairness: "{nonce}"}
      query: 'sum by (priority) (requests_total{fairness_id="{nonce}"})'
      label: priority
      expect: "80"
      timeout_s: 2
      interval_s: 0.1
    - id: objective_unknown
      check: header_label
      ingress: fake
      headers: {x-objective: no-such-objective, x-fairness: "{nonce}"}
      query: 'sum by (priority) (requests_total{fairness_id="{nonce}"})'
      label: priority
      expect: "0"
      timeout_s: 2
      interval_s: 0.1
    - id: objective_alternate_spelling
      check: header_label
      ingress: fake
      headers: {x-alt-objective: band-1, x-fairness: "{nonce}"}
      query: 'sum by (priority) (requests_total{fairness_id="{nonce}"})'
      label: priority
      expect: "60"
      timeout_s: 2
      interval_s: 0.1
    - id: team_identity
      check: header_label
      ingress: fake
      query: 'sum by (priority) (requests_total{fairness_id="team-a"})'
      label: priority
      expect: "60"
      timeout_s: 1
      interval_s: 0.1
"""

HEADER_LABEL_DEFECTS = {
    "objective_ignored": "objective_premium",
    "alt_spelling_ignored": "objective_alternate_spelling",
    "identity_lost": "team_identity",
}


def header_label_profile(path: Path, ingress_url: str, metrics_url: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        textwrap.dedent(
            f"""
            schema: llmdv.profile/1
            name: fleet-fake
            model: {{public_id: fake/Model-1B}}
            ingresses:
              - id: fake
                kind: llmd_router
                base_url: {ingress_url}
                surfaces: [chat_completions]
            environment:
              metrics: {{url: {metrics_url}}}
            """
        )
        + HEADER_LABEL_YAML
    )
    return path


# --------------------------------------------------------------------- log signatures

# A llmdv request id that a seeded exchanges.jsonl ties to a case, for attribution.
SEEDED_REQUEST_ID = "llmdv-0123456789ab"


def crash_and_restart(state: dict, pod: str, container: str, last_lines: str) -> dict:
    """A container that restarted during the run, its crash in the previous instance's log."""
    for item in state["objects"][NAMESPACE]["pods"]:
        if item["metadata"]["name"] == pod:
            item["status"]["containerStatuses"] = [
                {
                    "name": container,
                    "restartCount": 1,
                    "lastState": {"terminated": {"finishedAt": "2999-01-01T00:00:00Z"}},
                }
            ]
    state["logs"][f"{pod}/{container}#previous"] = last_lines
    return state


LOG_DEFECTS = {
    # defect: (pod/container, the line it adds, signature id)
    "engine_fatal": ("engine-0/vllm", "ERROR EngineCore encountered a fatal error.", "fatal"),
    "scheduler_assert": (
        "engine-1/vllm",
        "ERROR scheduler.py:612 AssertionError: num_computed_tokens exceeds",
        "scheduler_assert",
    ),
    "invalid_kv_read": (
        "engine-0/vllm",
        "WARNING Potentially invalid KV blocks for unrecognized request 1_chatcmpl-abc",
        "invalid_kv_read",
    ),
    "validation_stack": (
        "engine-1/vllm",
        f"ERROR request {SEEDED_REQUEST_ID} failed validation; Traceback (most recent call last):",
        "validation_stack",
    ),
    "kv_batch_rejected": (
        "router-a/router",
        '{"level":"error","msg":"Failed to apply KV event batch"}',
        "kv_batch_rejected",
    ),
    "render_unknown": (
        "router-a/router",
        '{"level":"error","msg":"failed to prepare per request data","error":"render 503"}',
        "render_failed",
    ),
}


def fleet_logs(defects: tuple[str, ...] = ()) -> dict[str, str]:
    logs = {
        "engine-0/vllm": "INFO Started server process\nINFO Avg prompt throughput: 10 tokens/s\n",
        "engine-1/vllm": "INFO Started server process\n",
        "router-a/router": (
            '{"level":"info","msg":"picked endpoint"}\n'
            '{"level":"error","msg":"failed to prepare per request data",'
            '"error":"context canceled"}\n'
        ),
    }
    for defect in defects:
        if defect in LOG_DEFECTS:
            where, line, _ = LOG_DEFECTS[defect]
            logs[where] += line + "\n"
    return logs


LOGS_INGRESS_YAML = r"""
    logs:
      - selector: app=router
        container: router
        forbid:
          - {id: kv_batch_rejected, pattern: 'Failed to apply KV event batch'}
          - {id: render_failed, pattern: 'failed to prepare per request data'}
        allow: ['context canceled', 'render returned status 4\d\d']
      - selector: app=engine
        container: vllm
        forbid:
          - {id: fatal, pattern: 'EngineCore encountered a fatal error'}
          - {id: scheduler_assert, pattern: 'AssertionError'}
          - {id: invalid_kv_read, pattern: 'Potentially invalid KV blocks'}
          - {id: validation_stack, pattern: 'Traceback \(most recent call last\)'}
"""

LOG_ENVIRONMENT_YAML = """
environment:
  namespace: fleet
"""

# Which source each defect's line lands in, by variant.
LOG_VARIANTS = {
    "router-a/router": "app=router/router",
    "engine-0/vllm": "app=engine/vllm",
    "engine-1/vllm": "app=engine/vllm",
}


def seed_exchange(run_dir: Path, case_id: str = "chat.seeded") -> None:
    """An exchanges.jsonl line carrying SEEDED_REQUEST_ID, as an earlier case would write."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "exchanges.jsonl").write_text(
        json.dumps(
            {
                "case_id": case_id,
                "request_headers": {"X-Request-Id": SEEDED_REQUEST_ID},
                "response_body": {"id": "chatcmpl-seeded-0001"},
            }
        )
        + "\n"
    )
