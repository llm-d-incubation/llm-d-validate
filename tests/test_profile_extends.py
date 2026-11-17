"""`extends`: a profile written as changes to a parent, composed before it is validated."""

from __future__ import annotations

import json
import random
from dataclasses import replace
from functools import reduce
from pathlib import Path

import pytest
import yaml

from llmdv.profile import compose, loader
from tests.fakes import FakeFleet, profile_yaml, run_llmdv

PARENT = {
    "schema": "llmdv.profile/1",
    "name": "shared",
    "model": {"public_id": "org/model", "reasoning": {"parser": "deepseek_r1"}},
    "capabilities": {"count_tokens": True, "vllm_options": ["priority"]},
    "ingresses": [
        {
            "id": "gateway",
            "kind": "litellm",
            "base_url": "https://gateway.example",
            "surfaces": ["chat_completions", "messages"],
        },
        {
            "id": "engine",
            "kind": "vllm",
            "base_url": "http://localhost:8000",
            "surfaces": ["chat_completions"],
        },
    ],
}


def write(path: Path, document: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    return path


def child_of(tmp_path, child: dict, parent: dict = PARENT) -> Path:
    write(tmp_path / "shared.yaml", parent)
    return write(tmp_path / "local.yaml", {"extends": "shared.yaml", **child})


def test_a_child_is_merged_over_its_parent(tmp_path):
    path = child_of(
        tmp_path,
        {
            "name": "local",
            "fleet_policy": "shared",
            "model": {"max_model_len": 4096},
            "capabilities": {"vllm_options": [], "count_tokens": None},
            "ingresses": [
                {"id": "engine", "setup": {"proxy": "allow"}},
                {"id": "extra", "base_url": "http://localhost:9000"},
            ],
        },
    )
    profile = loader.load(path)

    assert profile.name == "local"
    assert profile.source_path == path
    assert profile.fleet_policy.value == "shared"
    # Mappings merge, lists replace, null clears.
    assert profile.model.public_id == "org/model"
    assert profile.model.reasoning.parser == "deepseek_r1"
    assert profile.model.max_model_len == 4096
    assert profile.capabilities.vllm_options == ()
    assert profile.capabilities.count_tokens is False
    # Ingresses match by id; a new id is added at the end.
    assert profile.ingress_ids == ("gateway", "engine", "extra")
    engine = profile.ingress("engine")
    assert engine.kind.value == "vllm"
    assert engine.surfaces == ("chat_completions",)
    assert engine.setup.proxy.value == "allow"
    assert engine.model.max_model_len == 4096
    assert profile.ingress("gateway").surfaces == ("chat_completions", "messages")


def test_a_chain_of_three_resolves_root_first_and_relative_to_each_file(tmp_path):
    write(tmp_path / "base" / "root.yaml", PARENT)
    write(tmp_path / "base" / "middle.yaml", {"extends": "root.yaml", "model": {"aliases": ["a"]}})
    leaf = write(
        tmp_path / "team" / "leaf.yaml",
        {"extends": "../base/middle.yaml", "model": {"public_id": "org/other"}},
    )
    profile = loader.load(leaf)
    assert profile.model.public_id == "org/other"
    assert profile.model.aliases == ("a",)
    assert profile.model.reasoning.parser == "deepseek_r1"
    assert profile.name == "leaf"  # its own file's: a name is not inherited


def test_a_cycle_is_refused_naming_the_chain(tmp_path):
    write(tmp_path / "a.yaml", {"extends": "b.yaml", **PARENT})
    write(tmp_path / "b.yaml", {"extends": "c.yaml"})
    write(tmp_path / "c.yaml", {"extends": "a.yaml"})
    with pytest.raises(loader.ProfileError) as exc:
        loader.load(tmp_path / "a.yaml")
    message = str(exc.value)
    assert "extends: a cycle" in message
    for name in ("a.yaml -> ", "b.yaml -> ", "c.yaml -> "):
        assert name in message


def test_a_missing_parent_names_the_child_the_key_and_the_resolved_path(tmp_path):
    child = write(tmp_path / "team" / "local.yaml", {"extends": "../nowhere/shared.yaml"})
    with pytest.raises(loader.ProfileError) as exc:
        loader.load(child)
    expected = f"{child}: extends: no such profile file: {tmp_path / 'nowhere' / 'shared.yaml'}"
    assert str(exc.value) == expected


@pytest.mark.parametrize("value", [3, "", [], {"a": "shared.yaml"}, None, False])
def test_extends_must_name_a_file_or_a_list_of_them(tmp_path, value):
    child = write(tmp_path / "local.yaml", {"extends": value})
    with pytest.raises(loader.ProfileError, match="extends: expected the path of a parent"):
        loader.load(child)


def test_an_invalid_profile_chain_exits_2(tmp_path):
    write(tmp_path / "a.yaml", {"extends": "a.yaml"})
    result = run_llmdv("profile", "validate", str(tmp_path / "a.yaml"))
    assert result.returncode == 2, result.stderr
    assert "a cycle" in result.stderr


def test_a_problem_names_the_file_that_set_the_value(tmp_path):
    parent = {**PARENT, "capabilities": {"count_tokens": "yes"}}
    path = child_of(tmp_path, {"ingresses": [{"id": "engine", "model": {"bogus": 1}}]}, parent)
    with pytest.raises(loader.ProfileError) as exc:
        loader.load(path)
    assert str(exc.value).splitlines() == [
        f"{tmp_path / 'shared.yaml'}: capabilities.count_tokens: expected true or false, got 'yes'",
        f"{path}: ingresses[1](engine).model.bogus: unknown field",
    ]


def test_a_value_the_child_replaces_is_the_child_s_problem(tmp_path):
    path = child_of(tmp_path, {"ingresses": [{"id": "gateway", "surfaces": "messages"}]})
    with pytest.raises(loader.ProfileError) as exc:
        loader.load(path)
    assert str(exc.value).startswith(f"{path}: ingresses[0](gateway).surfaces: expected a list")


def test_the_composed_profile_is_written_whole_with_where_each_key_was_set(tmp_path):
    path = child_of(
        tmp_path, {"model": {"max_model_len": 4096}, "ingresses": [{"id": "engine", "tls": {}}]}
    )
    text = loader.composed_text(path)
    header = [line for line in text.splitlines() if line.startswith("#")]
    assert header == [
        "# Composed by llmdv from these profiles, each merged over the one before it:",
        "#   shared.yaml",
        "#   local.yaml",
        "# Where each key was set:",
        "#   schema: shared.yaml",
        "#   name: (file name)",
        "#   model: shared.yaml, local.yaml",
        "#   capabilities: shared.yaml",
        "#   ingresses: shared.yaml, local.yaml",
        "#     ingresses[0](gateway): shared.yaml",
        "#     ingresses[1](engine): shared.yaml, local.yaml",
    ]
    used = tmp_path / "run" / "profile.used.yaml"
    used.parent.mkdir()
    used.write_text(text)
    assert "extends" not in yaml.safe_load(text)
    assert loader.load(used) == _with_source(loader.load(path), used)
    assert loader.contract_digest(used) == loader.contract_digest(path)


def test_a_profile_without_extends_is_its_own_record(tmp_path):
    path = write(tmp_path / "plain.yaml", PARENT)
    assert loader.composed_text(path) is None


def test_a_name_left_to_the_file_name_is_kept_in_the_composed_record(tmp_path):
    parent = {key: value for key, value in PARENT.items() if key != "name"}
    path = child_of(tmp_path, {}, parent)
    text = loader.composed_text(path)
    assert yaml.safe_load(text)["name"] == "local"
    assert "#   name: (file name)" in text


def test_verify_writes_the_composed_profile_into_the_run_directory(tmp_path):
    """Written even when the profile is refused, as a single file is copied."""
    path = child_of(tmp_path, {"schema": "llmdv.profile/0"})
    run_dir = tmp_path / "run"
    result = run_llmdv("verify", "--profile", str(path), "--local-output", str(run_dir))
    assert result.returncode == 2, result.stderr
    assert (run_dir / "profile.used.yaml").read_text() == loader.composed_text(path)


def test_the_digest_covers_the_composed_declarations(tmp_path):
    path = child_of(tmp_path, {"model": {"max_model_len": 4096}})
    same = write(
        tmp_path / "flat.yaml",
        {**PARENT, "name": "local", "model": {**PARENT["model"], "max_model_len": 4096}},
    )
    assert loader.contract_digest(path) == loader.contract_digest(same)
    assert loader.contract_digest(path) != loader.contract_digest(tmp_path / "shared.yaml")
    write(tmp_path / "shared.yaml", {**PARENT, "fleet_policy": "shared"})
    assert loader.contract_digest(path) != loader.contract_digest(same)


# ------------------------------------------------------------------- a list of parents


def test_a_list_of_one_composes_as_the_single_path(tmp_path):
    write(tmp_path / "shared.yaml", PARENT)
    child = {"name": "one", "model": {"max_model_len": 4096}}
    single = write(tmp_path / "single.yaml", {"extends": "shared.yaml", **child})
    listed = write(tmp_path / "listed.yaml", {"extends": ["shared.yaml"], **child})
    assert compose.layers(listed)[0] == compose.layers(single)[0]
    assert compose.composed(compose.layers(listed)) == compose.composed(compose.layers(single))
    assert loader.composed_text(listed) == loader.composed_text(single).replace(
        "single.yaml", "listed.yaml"
    )


def test_a_list_merges_left_to_right_and_the_file_last(tmp_path):
    write(tmp_path / "base.yaml", PARENT)
    write(tmp_path / "engines" / "vllm.yaml", {"model": {"max_model_len": 1}, "name": "vllm"})
    write(tmp_path / "guides" / "small.yaml", {"model": {"max_model_len": 2}, "name": "small"})
    leaf = write(
        tmp_path / "combos" / "leaf.yaml",
        {"extends": ["../base.yaml", "../engines/vllm.yaml", "../guides/small.yaml"]},
    )
    profile = loader.load(leaf)
    assert profile.model.max_model_len == 2
    assert profile.name == "leaf"
    assert profile.model.public_id == "org/model"
    write(leaf, {"extends": ["../base.yaml", "../guides/small.yaml", "../engines/vllm.yaml"]})
    assert loader.load(leaf).model.max_model_len == 1
    write(
        leaf, {"extends": ["../base.yaml", "../engines/vllm.yaml"], "model": {"max_model_len": 3}}
    )
    assert loader.load(leaf).model.max_model_len == 3


def test_a_listed_parent_brings_its_own_parents_before_it(tmp_path):
    write(tmp_path / "root.yaml", PARENT)
    write(tmp_path / "engine.yaml", {"extends": "root.yaml", "model": {"max_model_len": 1}})
    write(tmp_path / "gateway.yaml", {"model": {"public_id": "org/gateway", "aliases": ["a"]}})
    leaf = write(tmp_path / "leaf.yaml", {"extends": ["gateway.yaml", "engine.yaml"]})
    chain = compose.layers(leaf)
    assert [layer.path.name for layer in chain] == [
        "gateway.yaml",
        "root.yaml",
        "engine.yaml",
        "leaf.yaml",
    ]
    profile = loader.load(leaf)
    assert profile.model.max_model_len == 1
    assert profile.model.aliases == ("a",)
    # The engine's root comes after the gateway, so its value wins: one sequence, later wins.
    assert profile.model.public_id == "org/model"


def test_a_file_reached_by_two_routes_is_refused_naming_both(tmp_path):
    write(tmp_path / "shared.yaml", PARENT)
    write(tmp_path / "a.yaml", {"extends": "shared.yaml"})
    write(tmp_path / "b.yaml", {"extends": ["shared.yaml"]})
    leaf = write(tmp_path / "leaf.yaml", {"extends": ["a.yaml", "b.yaml"]})
    with pytest.raises(loader.ProfileError) as exc:
        loader.load(leaf)
    leaf_, a, b, shared = (tmp_path / name for name in ("leaf", "a", "b", "shared"))
    assert str(exc.value) == (
        f"{b}.yaml: extends[0]: {shared}.yaml is reached twice, "
        f"by {leaf_}.yaml -> {a}.yaml -> {shared}.yaml "
        f"and by {leaf_}.yaml -> {b}.yaml -> {shared}.yaml"
    )


def test_a_cycle_through_a_list_is_refused_naming_the_chain(tmp_path):
    write(tmp_path / "base.yaml", PARENT)
    write(tmp_path / "a.yaml", {"extends": ["base.yaml", "b.yaml"]})
    write(tmp_path / "b.yaml", {"extends": "a.yaml"})
    with pytest.raises(loader.ProfileError) as exc:
        loader.load(tmp_path / "a.yaml")
    a, b = tmp_path / "a.yaml", tmp_path / "b.yaml"
    assert str(exc.value) == f"{b}: extends: a cycle: {a} -> {b} -> {a}"


def test_a_missing_listed_parent_names_the_file_the_index_and_the_resolved_path(tmp_path):
    write(tmp_path / "shared.yaml", PARENT)
    child = write(tmp_path / "team" / "local.yaml", {"extends": ["../shared.yaml", "gone.yaml"]})
    with pytest.raises(loader.ProfileError) as exc:
        loader.load(child)
    expected = f"{child}: extends[1]: no such profile file: {tmp_path / 'team' / 'gone.yaml'}"
    assert str(exc.value) == expected


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (["shared.yaml", 3], "extends[1]: expected the path of a parent profile, got 3"),
        (["shared.yaml", ""], "extends[1]: expected the path of a parent profile, got ''"),
        (["shared.yaml", "./shared.yaml"], "extends[1]: {shared} is already named in this list"),
    ],
)
def test_a_bad_list_entry_names_its_index_and_exits_2(tmp_path, value, message):
    write(tmp_path / "shared.yaml", PARENT)
    child = write(tmp_path / "local.yaml", {"extends": value})
    expected = f"{child}: {message.format(shared=tmp_path / 'shared.yaml')}"
    with pytest.raises(loader.ProfileError) as exc:
        loader.load(child)
    assert str(exc.value) == expected
    result = run_llmdv("profile", "validate", str(child))
    assert result.returncode == 2, result.stderr
    assert expected in result.stderr


def test_the_composed_record_lists_every_file_in_the_order_it_merged(tmp_path):
    write(tmp_path / "base.yaml", PARENT)
    write(tmp_path / "engines" / "root.yaml", {"model": {"aliases": ["a"]}})
    write(tmp_path / "engines" / "vllm.yaml", {"extends": "root.yaml", "fleet_policy": "shared"})
    path = write(
        tmp_path / "combos" / "local.yaml",
        {"extends": ["../base.yaml", "../engines/vllm.yaml"], "model": {"max_model_len": 1}},
    )
    header = [line for line in loader.composed_text(path).splitlines() if line.startswith("#")]
    assert header[:5] == [
        "# Composed by llmdv from these profiles, each merged over the one before it:",
        "#   ../base.yaml",
        "#   ../engines/root.yaml",
        "#   ../engines/vllm.yaml",
        "#   local.yaml",
    ]
    assert "#   model: ../base.yaml, ../engines/root.yaml, local.yaml" in header
    assert "#   fleet_policy: ../engines/vllm.yaml" in header


def _with_source(profile, path):
    return replace(profile, source_path=path)


# ------------------------------------------------------------------- what one file may hold


@pytest.mark.parametrize("extends", [{"extends": "shared.yaml"}, {}])
def test_an_ingress_id_named_twice_in_one_file_is_refused_before_composing(tmp_path, extends):
    """Composing merges entries by id, so a repeat would otherwise fold into one ingress."""
    write(tmp_path / "shared.yaml", PARENT)
    twice = [
        {"id": "extra", "base_url": "http://localhost:7", "surfaces": ["chat_completions"]},
        {"id": "extra", "base_url": "http://localhost:8"},
    ]
    document = {**extends, "name": "twice", "model": {"public_id": "m"}, "ingresses": twice}
    path = write(tmp_path / "local.yaml", document)
    with pytest.raises(loader.ProfileError) as exc:
        loader.load(path)
    assert str(exc.value) == f"{path}: ingresses[1](extra): duplicate ingress id 'extra'"
    result = run_llmdv("profile", "validate", str(path))
    assert result.returncode == 2, result.stderr


def test_an_ingress_id_named_twice_in_a_parent_names_the_parent(tmp_path):
    parent = {**PARENT, "ingresses": [PARENT["ingresses"][0], PARENT["ingresses"][0]]}
    path = child_of(tmp_path, {"name": "local"}, parent)
    with pytest.raises(loader.ProfileError) as exc:
        loader.load(path)
    assert str(exc.value) == (
        f"{tmp_path / 'shared.yaml'}: ingresses[1](gateway): duplicate ingress id 'gateway'"
    )


@pytest.mark.parametrize("extends", [{"extends": "shared.yaml"}, {}])
@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"base_url": "http://localhost:7"}, "ingresses[0].id: required"),
        ({"id": "", "base_url": "http://localhost:7"}, "ingresses[0].id: expected a name"),
        ({"id": 1, "base_url": "http://localhost:7"}, "ingresses[0].id: expected a name"),
        ({"id": "x\ny: 1", "base_url": "http://localhost:7"}, "ingresses[0].id: expected a name"),
        ({"id": "a b", "base_url": "http://localhost:7"}, "ingresses[0].id: expected a name"),
        ({"id": "a)b", "base_url": "http://localhost:7"}, "ingresses[0].id: expected a name"),
    ],
)
def test_an_ingress_needs_an_id_that_is_a_plain_name(tmp_path, extends, entry, message):
    """An id keys the merge and becomes a pytest id, a column and a file name."""
    write(tmp_path / "shared.yaml", PARENT)
    document = {**extends, "name": "odd", "model": {"public_id": "m"}, "ingresses": [entry]}
    path = write(tmp_path / "local.yaml", document)
    with pytest.raises(loader.ProfileError) as exc:
        loader.load(path)
    assert str(exc.value).startswith(f"{path}: {message}")
    assert loader.composed_text(path) is None


def test_a_child_cannot_remove_the_inherited_ingresses_with_null(tmp_path):
    path = child_of(tmp_path, {"ingresses": None})
    with pytest.raises(loader.ProfileError) as exc:
        loader.load(path)
    assert str(exc.value).startswith(f"{path}: ingresses: null would remove every inherited")
    result = run_llmdv("profile", "validate", str(path))
    assert result.returncode == 2, result.stderr


@pytest.mark.parametrize(
    "document",
    [
        {"extend": "shared.yaml", "name": "typo", "model": {"public_id": "m"}},
        {"extends": "shared.yaml", "modle": {"public_id": "other"}},
        {"extends": "shared.yaml", "ingress": [{"id": "new"}]},
    ],
)
def test_an_unknown_top_level_key_is_refused_naming_the_file_and_the_closest_key(
    tmp_path, document
):
    """`extend:` would otherwise run the profile without its parent."""
    write(tmp_path / "shared.yaml", PARENT)
    document = {"ingresses": PARENT["ingresses"], **document}
    path = write(tmp_path / "local.yaml", document)
    key = next(key for key in document if key in ("extend", "modle", "ingress"))
    meant = {"extend": "extends", "modle": "model", "ingress": "ingresses"}[key]
    with pytest.raises(loader.ProfileError) as exc:
        loader.load(path)
    assert str(exc.value) == f"{path}: {key}: unknown top-level key; did you mean {meant}?"
    result = run_llmdv("profile", "validate", str(path))
    assert result.returncode == 2, result.stderr
    assert f"did you mean {meant}?" in result.stderr


def test_an_unknown_top_level_key_in_a_parent_names_the_parent(tmp_path):
    path = child_of(tmp_path, {"name": "local"}, {**PARENT, "zzz": 1})
    with pytest.raises(loader.ProfileError) as exc:
        loader.load(path)
    message = str(exc.value)
    assert message.startswith(f"{tmp_path / 'shared.yaml'}: zzz: unknown top-level key; known: ")
    for key in ("schema", "name", "model", "ingresses", "known_failures", "extends"):
        assert key in message


def test_the_known_top_level_keys_are_the_profile_s_fields_and_extends():
    """One list: what REFERENCE.md documents (test_profile_reference.py) is what loads."""
    assert set(loader.TOP_LEVEL_KEYS) == {
        "schema", "name", "vantage", "environment", "model", "ingresses", "capabilities",
        "headers", "clients", "fleet_policy", "fleet", "known_failures", "extends",
    }  # fmt: skip


@pytest.mark.parametrize("where", ["local.yaml", "shared.yaml"])
def test_a_file_that_is_not_utf8_is_an_invalid_profile_naming_the_file(tmp_path, where):
    path = child_of(tmp_path, {"name": "local"})
    (tmp_path / where).write_bytes((tmp_path / where).read_bytes() + b"# \xff\xfe\n")
    with pytest.raises(loader.ProfileError) as exc:
        loader.load(path)
    assert str(exc.value).startswith(f"{tmp_path / where}: not UTF-8 text: ")
    assert loader.contract_digest(path) is None
    result = run_llmdv("profile", "validate", str(path))
    assert result.returncode == 2, result.stderr
    assert str(tmp_path / where) in result.stderr


# ------------------------------------------------------------------- the name


def test_a_name_is_never_inherited(tmp_path):
    """Two profiles sharing a name would be one profile in results."""
    assert loader.load(child_of(tmp_path, {})).name == "local"
    assert loader.load(child_of(tmp_path, {"name": "mine"})).name == "mine"
    assert loader.load(tmp_path / "shared.yaml").name == "shared"


def test_a_rerun_from_the_composed_record_keeps_the_name_and_the_digest(tmp_path):
    for parent in (PARENT, {key: value for key, value in PARENT.items() if key != "name"}):
        for child in ({}, {"name": "mine"}):
            path = child_of(tmp_path, child, parent)
            used = tmp_path / "run" / "profile.used.yaml"
            used.parent.mkdir(exist_ok=True)
            used.write_text(loader.composed_text(path))
            assert loader.load(used).name == loader.load(path).name == child.get("name", "local")
            assert loader.contract_digest(used) == loader.contract_digest(path)


def test_a_run_from_the_composed_record_reports_the_same_profile(tmp_path):
    """The record of a run must repeat it: same name and same digest in results.jsonl."""

    def records(*args):
        run_dir = tmp_path / f"run-{len(list(tmp_path.glob('run-*')))}"
        result = run_llmdv("verify", *args, "--local-output", str(run_dir), "-m", "discovery")
        assert result.returncode == 0, result.stderr
        lines = next(run_dir.rglob("results.jsonl")).read_text().splitlines()
        used = next(run_dir.rglob("profile.used.yaml"))
        return used, {(row["profile"], row["profile_digest"]) for row in map(json.loads, lines)}

    with FakeFleet() as fleet:
        parent = profile_yaml(fleet.base_url).replace("name: fake\n", "")
        assert "name:" not in parent
        (tmp_path / "base.yaml").write_text(parent)
        path = write(tmp_path / "kid.yaml", {"extends": "base.yaml", "fleet_policy": "shared"})
        used, first = records("--profile", str(path))
        _, second = records("--profile", str(used))
    assert first == second
    assert {name for name, _ in first} == {"kid"}


def test_the_composed_record_says_which_file_named_the_profile(tmp_path):
    assert "#   name: (file name)" in loader.composed_text(child_of(tmp_path, {}))
    assert "#   name: local.yaml" in loader.composed_text(child_of(tmp_path, {"name": "mine"}))


# ------------------------------------------------------------------- the record reloads


def test_a_relative_executable_in_a_parent_reloads_from_the_composed_record(tmp_path):
    binary = {"version": "2.1.0", "executable": "./claude"}
    client = {"id": "cc", "surfaces": ["chat_completions"], "binaries": [binary]}
    write(tmp_path / "base.yaml", PARENT)
    write(tmp_path / "parts" / "clients.yaml", {"clients": [client]})
    path = write(
        tmp_path / "local.yaml", {"extends": ["base.yaml", "parts/clients.yaml"], "name": "bins"}
    )
    profile = loader.load(path)
    assert profile.clients[0].binaries[0].executable == tmp_path / "parts" / "claude"

    used = tmp_path / "run" / "profile.used.yaml"
    used.parent.mkdir()
    used.write_text(loader.composed_text(path))
    assert loader.load(used) == _with_source(profile, used)
    assert loader.contract_digest(used) == loader.contract_digest(path)


def test_the_digest_is_the_same_wherever_the_profile_is_checked_out(tmp_path):
    """A binary's path is where it sits on one machine, not a declaration."""
    binary = {"version": "2.1.0", "executable": "./claude"}
    client = {"id": "cc", "surfaces": ["chat_completions"], "binaries": [binary]}
    digests = set()
    for checkout in ("one", "two"):
        root = tmp_path / checkout
        write(root / "base.yaml", PARENT)
        write(root / "parts" / "clients.yaml", {"clients": [client]})
        path = write(root / "local.yaml", {"extends": ["base.yaml", "parts/clients.yaml"]})
        digests.add(loader.contract_digest(path))
    assert len(digests) == 1 and None not in digests

    newer = {**client, "binaries": [{**binary, "version": "2.2.0"}]}
    write(root / "parts" / "clients.yaml", {"clients": [newer]})
    assert loader.contract_digest(path) not in digests  # the version is a declaration


@pytest.mark.parametrize("odd", ["x\ny: 1", "a\rb", "a: b #", " x"])
def test_the_composed_record_parses_whatever_an_id_or_a_key_holds(tmp_path, odd):
    """The header is comments, so nothing a file names may end a comment line."""
    entry = {"id": odd, "base_url": "http://localhost:7"}
    path = child_of(tmp_path, {"name": "odd", odd: 1, "ingresses": [entry]})
    chain = compose.layers(path)
    text = compose.render(chain)
    assert yaml.safe_load(text) == compose.resolved(chain)
    assert loader.contract_digest(path).startswith("sha256:")
    assert loader.composed_text(path) is None  # refused, so verify copies the file as written


# ------------------------------------------------------------------- validation names the file


def test_a_validation_problem_names_the_file_that_set_the_value(tmp_path):
    parent = {
        **PARENT,
        "headers": {"x-request-id": {"nowhere": "ignored"}},
        "ingresses": [{**PARENT["ingresses"][0], "surfaces": ["bogus"]}, PARENT["ingresses"][1]],
    }
    path = child_of(
        tmp_path,
        {"name": "kid", "clients": [{"id": "agent", "surfaces": ["responses"]}]},
        parent,
    )
    found = {item.path: item for item in loader.validate(loader.load(path))}
    shared = tmp_path / "shared.yaml"
    assert found["ingresses[0](gateway).surfaces"].file == shared
    assert found["headers.x-request-id"].file == shared
    assert found["clients[0](agent).surfaces"].file == path
    assert str(found["headers.x-request-id"]) == (
        f"ERROR   {shared}: headers.x-request-id: names ingress 'nowhere', which is not declared"
    )
    result = run_llmdv("profile", "validate", str(path))
    assert result.returncode == 2, result.stderr
    assert f"{shared}: ingresses[0](gateway).surfaces: unknown surface 'bogus'" in result.stdout
    assert f"{path}: clients[0](agent).surfaces: needs 'responses'" in result.stdout


def test_a_model_problem_names_the_file_that_set_the_value(tmp_path):
    path = child_of(
        tmp_path, {"name": "kid"}, {**PARENT, "model": {"public_id": "m", "max_model_len": -1}}
    )
    assert [str(item) for item in loader.validate_models(loader.load(path))] == [
        f"ERROR   {tmp_path / 'shared.yaml'}: model.max_model_len: must not be negative"
    ]


def test_a_single_file_s_validation_problems_read_as_before(tmp_path):
    path = write(tmp_path / "plain.yaml", {**PARENT, "schema": "llmdv.profile/0"})
    (found,) = [item for item in loader.validate(loader.load(path)) if item.level == "error"]
    assert found.file is None
    assert str(found).startswith("ERROR   schema: unsupported schema")


# ------------------------------------------------------------------- a linked file


def test_a_linked_profile_resolves_extends_from_where_the_link_is(tmp_path):
    """Relative to the file as it was named, which is the path every message shows."""
    write(tmp_path / "real" / "shared.yaml", {**PARENT, "fleet_policy": "shared"})
    real = write(tmp_path / "real" / "local.yaml", {"extends": "shared.yaml", "name": "local"})
    (tmp_path / "links").mkdir()
    link = tmp_path / "links" / "local.yaml"
    link.symlink_to(real)
    with pytest.raises(loader.ProfileError) as exc:
        loader.load(link)
    assert str(exc.value) == (
        f"{link}: extends: no such profile file: {tmp_path / 'links' / 'shared.yaml'}"
    )
    write(tmp_path / "links" / "shared.yaml", PARENT)
    assert loader.load(link).fleet_policy.value == "dedicated"


# ------------------------------------------------------------------- composition is a fold


def _tree(rng: random.Random, depth: int) -> object:
    roll = rng.random()
    if depth == 0 or roll < 0.3:
        return rng.choice([None, 0, 7, "x", "y", True])
    if roll < 0.5:
        return [rng.choice(["x", 1, None]) for _ in range(rng.randint(0, 3))]
    return {rng.choice("abcd"): _tree(rng, depth - 1) for _ in range(rng.randint(0, 4))}


@pytest.mark.parametrize("seed", range(200))
def test_a_chain_composes_as_a_fold_of_the_override_merge(tmp_path, seed):
    rng = random.Random(seed)
    documents = []
    for _ in range(rng.randint(1, 4)):
        tree = _tree(rng, 4)
        documents.append(tree if isinstance(tree, dict) else {"a": tree})
    for index, document in enumerate(documents):
        parent = {"extends": f"{index - 1}.yaml"} if index else {}
        write(tmp_path / f"{index}.yaml", {**parent, **document})

    chain = compose.layers(tmp_path / f"{len(documents) - 1}.yaml")
    assert compose.composed(chain) == reduce(compose.merge, documents)


def _merge_order(path: Path) -> list[Path]:
    """The files `path` extends, depth first, written out independently of `layers`."""
    raw = yaml.safe_load(path.read_text())
    parents = raw.get("extends") or []
    order: list[Path] = []
    for parent in [parents] if isinstance(parents, str) else parents:
        order += _merge_order(path.parent / parent)
    return [*order, path]


@pytest.mark.parametrize("seed", range(200))
def test_a_list_composes_as_a_fold_of_compose_in_merge_order(tmp_path, seed):
    rng = random.Random(seed)
    documents: dict[Path, dict] = {}

    def make(depth: int) -> Path:
        path = tmp_path / f"{len(documents)}.yaml"
        tree = _tree(rng, 3)
        document = tree if isinstance(tree, dict) else {"a": tree}
        documents[path] = document
        parents = [make(depth - 1).name for _ in range(rng.randint(0, 3) if depth else 0)]
        if parents:
            extends = parents[0] if len(parents) == 1 and rng.random() < 0.5 else parents
            document = {"extends": extends, **document}
        write(path, document)
        return path

    leaf = make(3)
    expected = reduce(compose.compose, (documents[path] for path in _merge_order(leaf)))
    assert compose.composed(compose.layers(leaf)) == expected
