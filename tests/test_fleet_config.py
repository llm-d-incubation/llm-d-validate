"""Configuration cases against an executable kubectl double: each defect fails its own rule.

The double answers from a JSON state, so every run reads a real `kubectl` process through llmdv's
own adapter. No cluster is involved, and a real kubectl is never on PATH.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from llmdv.fleet import config as cfg
from llmdv.kube import READ_VERBS
from llmdv.profile import loader
from llmdv.profile.fleet import Extraction, ObjectRef
from tests.fleet_fakes import (
    CACHE_REUSE_YAML,
    CONFIG_TARGETS,
    FLEET_CONFIG_YAML,
    fake_kubectl,
    fleet_profile,
    fleet_state,
    kubectl_calls,
    no_kubectl_env,
    run_fleet_suite,
    verdicts,
)

CONFIG_CASES = (
    "config.settings",
    "config.values_agree",
    "config.field_values",
    "config.ready_endpoints",
    "config.selector_exclusive",
)


def run(tmp_path, defects=(), blocks=(FLEET_CONFIG_YAML, CACHE_REUSE_YAML), env=None):
    profile = fleet_profile(tmp_path / "profile.yaml", *blocks)
    env = env if env is not None else fake_kubectl(tmp_path / "bin", fleet_state(defects))
    result, records = run_fleet_suite(profile, tmp_path / "run", env, "-k", "config")
    return result, records, env


def config_rows(records):
    return {key: value for key, value in verdicts(records).items() if key[0] in CONFIG_CASES}


def test_a_healthy_fleet_passes_every_rule(tmp_path):
    result, records, env = run(tmp_path)
    rows = config_rows(records)
    assert set(rows.values()) == {"pass"}, (rows, result.stdout[-3000:])
    assert {variant for _, variant in rows} == {variant for _, variant in CONFIG_TARGETS.values()}
    verbs = {call[call.index("get") if "get" in call else 0] for call in kubectl_calls(env)}
    assert verbs <= READ_VERBS and verbs == {"get"}


@pytest.mark.parametrize("defect", sorted(CONFIG_TARGETS))
def test_each_defect_fails_its_own_rule(tmp_path, defect):
    result, records, _ = run(tmp_path, (defect,))
    rows = config_rows(records)
    target = CONFIG_TARGETS[defect]
    assert rows[target] == "fail", (defect, rows, result.stdout[-3000:])
    others = {key: value for key, value in rows.items() if key != target}
    assert set(others.values()) == {"pass"}, (defect, others)


def test_an_empty_selection_fails(tmp_path):
    """Relabelled engines leave the selector matching nothing; that never reads as healthy."""
    _, records, _ = run(tmp_path, ("engines_relabelled",))
    rows = config_rows(records)
    for variant in ("engine_dev_mode_off", "error_stacks_off", "prefix_caching_on"):
        assert rows[("config.settings", variant)] == "fail"
    checks = next(
        r["checks"]
        for r in records
        if r.get("case_id") == "config.settings" and r.get("variant") == "engine_dev_mode_off"
    )
    assert any(c["name"].startswith("read deployments") and not c["ok"] for c in checks)


def test_a_rule_gated_on_an_undeclared_capability_skips(tmp_path):
    _, records, _ = run(tmp_path, blocks=(FLEET_CONFIG_YAML,))
    row = next(r for r in records if r.get("variant") == "prefix_caching_on")
    assert row["outcome"] == "skip" and row["skip_class"] == "undeclared"


def test_a_profile_without_rules_keeps_one_undeclared_row_per_case(tmp_path):
    _, records, _ = run(tmp_path, blocks=())
    rows = [r for r in records if r.get("case_id") in CONFIG_CASES]
    assert sorted(r["case_id"] for r in rows) == sorted(CONFIG_CASES)
    assert {(r["outcome"], r["skip_class"]) for r in rows} == {("skip", "undeclared")}


def test_no_kubectl_is_missing_instrumentation(tmp_path):
    _, records, _ = run(tmp_path, env=no_kubectl_env(tmp_path / "empty"))
    rows = [r for r in records if r.get("case_id") in CONFIG_CASES]
    assert rows and {r["skip_class"] for r in rows} <= {"requires_instrumentation", "undeclared"}
    assert any(r["skip_class"] == "requires_instrumentation" for r in rows)


def test_direct_pytest_uses_the_requested_cluster_cli(tmp_path):
    profile = fleet_profile(tmp_path / "profile.yaml", FLEET_CONFIG_YAML, CACHE_REUSE_YAML)
    env = fake_kubectl(tmp_path / "bin", fleet_state(), name="oc")
    result, records = run_fleet_suite(
        profile, tmp_path / "run", env, "--llmdv-kube-cli", "oc", "-k", "config"
    )
    assert set(config_rows(records).values()) == {"pass"}, result.stdout[-3000:]
    assert kubectl_calls(env)


def test_direct_pytest_skips_when_the_requested_cluster_cli_is_missing(tmp_path):
    profile = fleet_profile(tmp_path / "profile.yaml", FLEET_CONFIG_YAML)
    result, records = run_fleet_suite(
        profile,
        tmp_path / "run",
        no_kubectl_env(tmp_path / "empty"),
        "--llmdv-kube-cli",
        "missing-cli",
        "-k",
        "config",
    )
    rows = [record for record in records if record.get("case_id") in CONFIG_CASES]
    assert result.returncode == 0
    assert any(row.get("skip_class") == "requires_instrumentation" for row in rows)


def test_verify_forwards_the_cluster_cli_to_pytest(tmp_path, monkeypatch):
    from llmdv.cli import verify_cmd

    args = SimpleNamespace(
        profile="profile.yaml",
        evidence="failures",
        transcript_cap=None,
        verbose=0,
        ingress=[],
        client=[],
        surface=[],
        kubeconfig=None,
        context=None,
        kube_cli="oc",
        keyword=None,
        marker=None,
        pytest_args=[],
    )
    monkeypatch.setattr(verify_cmd, "_plugin_is_registered", lambda: True)
    argv = verify_cmd._build_pytest_argv(args, tmp_path, "run")
    assert argv[argv.index("--llmdv-kube-cli") + 1] == "oc"


def test_a_forbidden_read_is_missing_instrumentation(tmp_path):
    state = fleet_state()
    state["forbidden"] = ["routes"]
    env = fake_kubectl(tmp_path / "bin", state)
    _, records, _ = run(tmp_path, env=env)
    row = next(r for r in records if r.get("variant") == "route_timeout")
    assert row["outcome"] == "skip" and row["skip_class"] == "requires_instrumentation"


def test_every_row_is_reported_in_an_ingress_column(tmp_path):
    """A declared ingress, else the innermost selected one; never a row in no column."""
    _, records, _ = run(tmp_path)
    assert {r["ingress"] for r in records} == {"fake"}
    _, records, _ = run(tmp_path / "bare", blocks=())
    assert {r["ingress"] for r in records} == {"fake"}


# ----------------------------------------------------------------- declarations


def write(tmp_path, block):
    return fleet_profile(tmp_path / "p.yaml", block)


@pytest.mark.parametrize(
    "block, message",
    [
        ("fleet:\n  config:\n    - {id: a, check: nope}\n", "check"),
        ("fleet:\n  config:\n    - {id: a, check: settings, objects: {kind: pods}}\n", "require"),
        (
            "fleet:\n  config:\n    - {id: a, check: settings, objects: {kind: pods},"
            " forbid: ['(']}\n",
            "invalid pattern",
        ),
        (
            "fleet:\n  config:\n    - {id: a, check: values_agree, expect: '1', sources:"
            " [{objects: {kind: pods}, pattern: 'x'}]}\n",
            "capture group",
        ),
        (
            "fleet:\n  config:\n    - {id: a, check: field_values, objects: {kind: pods},"
            " path: [a]}\n",
            "exactly one",
        ),
        ("fleet:\n  config:\n    - {id: a, check: ready_endpoints, service: s}\n", "count"),
        (
            "fleet:\n  config:\n    - {id: a, check: settings, objects: {kind: pods},"
            " forbid: [x], colour: red}\n",
            "unknown field",
        ),
        (
            "fleet:\n  config:\n    - {id: a, check: settings, objects: {kind: secrets},"
            " forbid: [x]}\n",
            "never read Secrets",
        ),
        (
            "fleet:\n  config:\n    - {id: a, check: settings,"
            " objects: {kind: 'configmaps,secrets'}, forbid: [x]}\n",
            "one resource kind",
        ),
        (
            "fleet:\n  config:\n    - {id: a, check: settings,"
            " objects: {kind: '--all-namespaces'}, forbid: [x]}\n",
            "one resource kind",
        ),
        (
            "fleet:\n  config:\n    - {id: a, check: ready_endpoints, service: s, count: 1}\n"
            "    - {id: a, check: ready_endpoints, service: s, count: 1}\n",
            "duplicate",
        ),
    ],
)
def test_malformed_rules_are_profile_errors(tmp_path, block, message):
    with pytest.raises(loader.ProfileError, match=message):
        loader.load(write(tmp_path, block))


def test_cross_references_are_validated_offline(tmp_path):
    block = (
        "fleet:\n  config:\n"
        "    - {id: a, check: ready_endpoints, service: s, count: 1, ingress: nope,"
        " when: no_such_capability}\n"
    )
    found = [str(d) for d in loader.validate(loader.load(write(tmp_path, block)))]
    assert any("names undeclared ingress 'nope'" in d for d in found)
    assert any("unknown capability 'no_such_capability'" in d for d in found)
    assert any("no namespace" in d for d in found)


@pytest.mark.parametrize(
    "capability, message",
    [
        ("cache_reuse.blok_size", "unknown capability"),
        ("cache_reuse.block_size", "not declared by any ingress"),
    ],
)
def test_expected_capability_must_resolve(tmp_path, capability, message):
    block = (
        "fleet:\n  config:\n    - id: a\n      check: values_agree\n"
        f"      expect_capability: {capability}\n"
        "      sources:\n        - objects: {kind: deployments}\n"
        "          pattern: 'x=(.*)'\n"
    )
    found = [str(item) for item in loader.validate(loader.load(write(tmp_path, block)))]
    assert any(message in item for item in found)


def test_an_undeclared_when_gate_suppresses_its_capability_expectation(tmp_path):
    block = (
        "fleet:\n  config:\n    - id: a\n      check: values_agree\n"
        "      when: cache_reuse\n      expect_capability: cache_reuse.block_size\n"
        "      sources:\n        - objects: {kind: deployments}\n"
        "          pattern: 'x=(.*)'\n"
    )
    found = [str(item) for item in loader.validate(loader.load(write(tmp_path, block)))]
    assert not any("expect_capability" in item for item in found)


def test_a_profile_without_a_fleet_block_has_no_rules(tmp_path):
    profile = loader.load(fleet_profile(tmp_path / "p.yaml"))
    assert profile.fleet.config == ()


# ----------------------------------------------------------------- evaluation


def test_containers_are_found_in_any_pod_template():
    nested = {
        "metadata": {"name": "set"},
        "spec": {
            "roles": [
                {"template": {"spec": {"containers": [{"name": "a", "args": ["--x=1"]}]}}},
                {"template": {"spec": {"containers": [{"name": "b", "command": ["run"]}]}}},
            ]
        },
    }
    assert [c["name"] for c in cfg.containers_in(nested)] == ["a", "b"]
    assert cfg.units(nested, "containers", ("b",)) == [("set/b", "run")]


def test_same_named_containers_in_different_templates_have_distinct_units():
    nested = {
        "metadata": {"name": "set"},
        "spec": {
            "roles": [
                {"template": {"spec": {"containers": [{"name": "v", "args": ["x=1"]}]}}},
                {"template": {"spec": {"containers": [{"name": "v", "args": ["x=2"]}]}}},
            ]
        },
    }
    extraction = Extraction(ObjectRef("custom"), r"x=(\d)")
    values, missing = cfg.extract([nested], extraction)
    assert missing == []
    assert set(values.values()) == {"1", "2"}
    assert len(values) == 2


def test_container_text_shows_referenced_values_without_reading_them():
    text = cfg.container_text(
        {"name": "c", "env": [{"name": "TOKEN", "valueFrom": {"secretKeyRef": {}}}]}
    )
    assert text.splitlines()[1] == "TOKEN=<from>"


def test_explicit_environment_value_overrides_env_from():
    container = {
        "name": "c",
        "envFrom": [{"configMapRef": {"name": "defaults"}}],
        "env": [{"name": "DEBUG", "value": "false"}],
    }
    assert cfg.container_text(container, {"defaults": {"DEBUG": "true"}}).splitlines() == [
        "",
        "DEBUG=false",
    ]


@pytest.mark.parametrize(
    "value, seconds",
    [
        ("2700s", 2700),
        ("45m", 2700),
        ("1h", 3600),
        ("1h30m", 5400),
        ("45m0s", 2700),
        (30, 30),
        ("500ms", 0.5),
        ("soon", None),
    ],
)
def test_durations(value, seconds):
    assert cfg.duration_seconds(value) == seconds


def test_an_extraction_default_fills_a_unit_without_the_pattern():
    obj = {"metadata": {"name": "e"}, "spec": {"containers": [{"name": "v", "args": []}]}}
    extraction = Extraction(ObjectRef("deployments"), r"--block-size=(\d+)", default="16")
    assert cfg.extract([obj], extraction) == ({"e/v": "16"}, [])
    bare = Extraction(ObjectRef("deployments"), r"--block-size=(\d+)")
    assert cfg.extract([obj], bare) == ({}, ["e/v"])


@pytest.mark.parametrize("defects, code", [((), 0), (("two_routers",), 1)])
def test_verify_exits_on_the_fleet_for_a_config_defect(tmp_path, defects, code):
    """A configuration defect is a fleet result: exit 1, with the rule named in the summary."""
    from tests.fakes import run_llmdv

    profile = fleet_profile(tmp_path / "p.yaml", FLEET_CONFIG_YAML, CACHE_REUSE_YAML)
    env = fake_kubectl(tmp_path / "bin", fleet_state(defects))
    result = run_llmdv(
        "verify",
        "--profile",
        str(profile),
        "--skip-preflight",
        "-m",
        "fleet",
        "--local-output",
        str(tmp_path / "out"),
        env={**env, "HOME": str(tmp_path)},
    )
    assert result.returncode == code, result.stdout[-2000:]
    if code:
        assert "config.ready_endpoints[single_active_router]" in result.stdout


@pytest.mark.parametrize("before", [True, False])
def test_verify_accepts_the_cluster_cli_before_or_after_the_subcommand(tmp_path, before):
    from tests.fakes import run_llmdv

    profile = fleet_profile(tmp_path / "p.yaml", FLEET_CONFIG_YAML, CACHE_REUSE_YAML)
    env = fake_kubectl(tmp_path / "bin", fleet_state(), name="oc")
    command = ["--kube-cli", "oc", "verify"] if before else ["verify", "--kube-cli", "oc"]
    result = run_llmdv(
        *command,
        "--profile",
        str(profile),
        "--skip-preflight",
        "-m",
        "fleet",
        "--local-output",
        str(tmp_path / "out"),
        env={**env, "HOME": str(tmp_path)},
    )
    assert result.returncode == 0, result.stdout[-3000:]
    assert kubectl_calls(env)


@pytest.mark.parametrize("api", ["endpointslices", "endpoints"])
def test_ready_endpoints_read_either_api(tmp_path, api):
    """Slices where the cluster has them, else the Endpoints object; both catch two routers."""
    for defects, outcome in (((), "pass"), (("two_routers",), "fail")):
        state = fleet_state(defects)
        drop = "endpoints" if api == "endpointslices" else "endpointslices"
        del state["objects"]["fleet"][drop]
        where = tmp_path / f"{api}-{outcome}"
        _, records, env = run(where, env=fake_kubectl(where / "bin", state))
        row = next(r for r in records if r.get("variant") == "single_active_router")
        assert row["outcome"] == outcome, (api, defects, row["reason"])
        reads = [call for call in kubectl_calls(env) if "endpoints" in call]
        assert bool(reads) is (api == "endpoints")


def test_slices_count_a_dual_stack_pod_once_and_skip_unready():
    slices = [
        {"endpoints": [{"targetRef": {"name": "a"}, "conditions": {"ready": True}}]},
        {
            "endpoints": [
                {"targetRef": {"name": "a"}},
                {"targetRef": {"name": "b"}, "conditions": {"ready": False}},
            ]
        },
    ]
    assert cfg.ready_in_slices(slices) == ["a"]


@pytest.mark.parametrize("how", ["envFrom", "configMapKeyRef"])
def test_a_setting_moved_into_a_configmap_is_still_seen(tmp_path, how):
    """Dev mode set through a ConfigMap, whole or by key, fails like an inline variable."""
    state = fleet_state()
    objects = state["objects"]["fleet"]
    objects["configmaps"].append(
        {
            "kind": "ConfigMap",
            "metadata": {"name": "engine-env"},
            "data": {"VLLM_SERVER_DEV_MODE": "1"},
        }
    )
    container = objects["deployments"][0]["spec"]["template"]["spec"]["containers"][0]
    if how == "envFrom":
        container["envFrom"] = [{"configMapRef": {"name": "engine-env"}}]
    else:
        container["env"].append(
            {
                "name": "VLLM_SERVER_DEV_MODE",
                "valueFrom": {
                    "configMapKeyRef": {"name": "engine-env", "key": "VLLM_SERVER_DEV_MODE"}
                },
            }
        )
    _, records, _ = run(tmp_path, env=fake_kubectl(tmp_path / "bin", state))
    row = next(r for r in records if r.get("variant") == "engine_dev_mode_off")
    assert row["outcome"] == "fail", row["reason"]


_ENV_BLOCK_SIZE_YAML = r"""
environment:
  namespace: fleet
fleet:
  config:
    - id: env_block_size
      check: values_agree
      sources:
        - objects: {kind: deployments, selector: app=engine}
          containers: [vllm]
          pattern: '^KV_BLOCK_SIZE=(\d+)$'
        - objects: {kind: configmaps, name: router-config}
          view: data
          pattern: 'blockSizeTokens:\s*(\d+)'
"""


@pytest.mark.parametrize("engine_value, outcome", [("64", "pass"), ("16", "fail")])
def test_a_value_taken_from_a_configmap_is_compared(tmp_path, engine_value, outcome):
    """An extraction reads a variable's ConfigMap value, so agreement is judged on it."""
    state = fleet_state()
    objects = state["objects"]["fleet"]
    objects["configmaps"].append(
        {"kind": "ConfigMap", "metadata": {"name": "engine-env"}, "data": {"BLOCK": engine_value}}
    )
    container = objects["deployments"][0]["spec"]["template"]["spec"]["containers"][0]
    container["env"].append(
        {
            "name": "KV_BLOCK_SIZE",
            "valueFrom": {"configMapKeyRef": {"name": "engine-env", "key": "BLOCK"}},
        }
    )
    env = fake_kubectl(tmp_path / "bin", state)
    _, records, _ = run(tmp_path, blocks=(_ENV_BLOCK_SIZE_YAML,), env=env)
    row = next(r for r in records if r.get("variant") == "env_block_size")
    assert row["outcome"] == outcome, row.get("reason")


def test_an_unreadable_configmap_skips_instead_of_passing(tmp_path):
    state = fleet_state()
    objects = state["objects"]["fleet"]
    objects["configmaps"].append(
        {"kind": "ConfigMap", "metadata": {"name": "engine-env"}, "data": {"DEBUG": "true"}}
    )
    container = objects["deployments"][0]["spec"]["template"]["spec"]["containers"][0]
    container["envFrom"] = [{"configMapRef": {"name": "engine-env"}}]
    state["forbidden"] = ["configmaps"]
    block = (
        "environment:\n  namespace: fleet\nfleet:\n  config:\n"
        "    - id: no_debug\n      check: settings\n"
        "      objects: {kind: deployments, selector: app=engine}\n"
        "      forbid: ['^DEBUG=true$']\n"
    )
    _, records, _ = run(tmp_path, blocks=(block,), env=fake_kubectl(tmp_path / "bin", state))
    row = next(item for item in records if item.get("variant") == "no_debug")
    assert (row["outcome"], row["skip_class"]) == ("skip", "requires_instrumentation")


@pytest.mark.parametrize("optional,outcome", [(False, "fail"), (True, "pass")])
def test_a_missing_configmap_only_passes_when_the_reference_is_optional(
    tmp_path, optional, outcome
):
    state = fleet_state()
    objects = state["objects"]["fleet"]
    container = objects["deployments"][0]["spec"]["template"]["spec"]["containers"][0]
    container["envFrom"] = [{"configMapRef": {"name": "missing-env", "optional": optional}}]
    block = (
        "environment:\n  namespace: fleet\nfleet:\n  config:\n"
        "    - id: no_debug\n      check: settings\n"
        "      objects: {kind: deployments, selector: app=engine}\n"
        "      forbid: ['^DEBUG=true$']\n"
    )
    _, records, _ = run(tmp_path, blocks=(block,), env=fake_kubectl(tmp_path / "bin", state))
    row = next(item for item in records if item.get("variant") == "no_debug")
    assert row["outcome"] == outcome, row.get("reason")


def test_a_selector_path_short_of_the_label_map_fails(tmp_path):
    """A path that stops at a selector holding a nested map is a declaration error, never a skip."""
    state = fleet_state()
    pool = state["objects"]["fleet"]["inferencepools"][0]
    pool["spec"]["selector"] = {"matchLabels": pool["spec"]["selector"]}
    blocks = (FLEET_CONFIG_YAML, CACHE_REUSE_YAML)
    _, records, _ = run(tmp_path, blocks=blocks, env=fake_kubectl(tmp_path / "bin", state))
    row = next(r for r in records if r.get("variant") == "pool_exclusive")
    assert row["outcome"] == "fail" and "owner selector" in row["reason"], row.get("reason")


def test_a_secret_reference_is_never_read():
    container = {
        "name": "c",
        "env": [{"name": "TOKEN", "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}}],
        "envFrom": [{"secretRef": {"name": "s"}}],
    }
    assert cfg.configmap_refs({"spec": {"containers": [container]}}) == set()
    assert cfg.container_text(container, {"s": {"k": "secret-value"}}).splitlines()[1:] == [
        "TOKEN=<from>"
    ]
