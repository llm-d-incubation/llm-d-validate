"""Shared options survive every command boundary without changing pytest arguments."""

from pathlib import Path

import pytest

from llmdv.cli.main import _resolve_level, build_parser

OPTIONS = [
    (["--log-level", "DEBUG"], "log_level", "DEBUG"),
    (["-vv"], "verbose", 2),
    (["-q"], "quiet", True),
    (["--log-format", "json"], "log_format", "json"),
    (["--log-file", "diagnostics.log"], "log_file", "diagnostics.log"),
    (["--no-color"], "no_color", True),
    (["--kube-cli", "oc"], "kube_cli", "oc"),
    (["--kubeconfig", "config.yaml"], "kubeconfig", Path("config.yaml")),
    (["--context", "example-context"], "context", "example-context"),
]


@pytest.mark.parametrize("option,dest,expected", OPTIONS)
@pytest.mark.parametrize(
    "before,after",
    [
        ([], ["verify", "--profile", "example.yaml"]),
        (["verify", "--profile", "example.yaml"], []),
        ([], ["profile", "show", "example.yaml"]),
        (["profile"], ["show", "example.yaml"]),
        (["profile", "show", "example.yaml"], []),
    ],
)
def test_shared_options_survive_each_command_boundary(option, dest, expected, before, after):
    args, unknown = build_parser().parse_known_args([*before, *option, *after])
    assert getattr(args, dest) == expected
    assert unknown == []


@pytest.mark.parametrize("command", [["verify", "--profile", "p"], ["profile", "show", "p"]])
def test_defaults_remain_available_when_options_are_absent(command):
    args = build_parser().parse_args(command)
    assert args.log_level == "INFO"
    assert args.verbose == 0
    assert args.quiet is False
    assert args.log_format == "text"
    assert args.log_file is None
    assert args.no_color is False
    assert args.kubeconfig is None
    assert args.context is None
    assert not hasattr(args, "kube_cli")


def test_last_explicit_value_wins_across_nested_commands():
    args = build_parser().parse_args(
        [
            "--log-level",
            "WARNING",
            "--context",
            "first",
            "profile",
            "--log-level",
            "ERROR",
            "--context",
            "second",
            "show",
            "p",
            "--log-level",
            "DEBUG",
            "--context",
            "last",
        ]
    )
    assert args.log_level == "DEBUG"
    assert args.context == "last"


def test_reusing_a_parser_does_not_retain_previous_options():
    parser = build_parser()
    parser.parse_args(["-vv", "--log-format", "json", "profile", "show", "p"])
    args = parser.parse_args(["profile", "show", "p"])
    assert args.verbose == 0
    assert args.log_format == "text"


@pytest.mark.parametrize("option,level", [(["-vv"], "DEBUG"), (["-q"], "WARNING")])
def test_verbosity_changes_console_level_before_subcommand(option, level):
    args = build_parser().parse_args([*option, "verify", "--profile", "p"])
    assert _resolve_level(args) == level


def test_pytest_arguments_and_separator_remain_untouched():
    args, unknown = build_parser().parse_known_args(
        [
            "--context",
            "example-context",
            "verify",
            "--profile",
            "p",
            "-x",
            "--tb=short",
            "--",
            "--log-level",
            "ERROR",
        ]
    )
    assert args.context == "example-context"
    assert args.log_level == "INFO"
    assert unknown == ["-x", "--tb=short", "--", "--log-level", "ERROR"]
