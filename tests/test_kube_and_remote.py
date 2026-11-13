"""The read-only cluster guard, and remote-output validation."""

from __future__ import annotations

import pytest

from llmdv.kube import READ_VERBS, KubeContext, KubeError
from llmdv.output import UnsupportedRemote, parse_remote_uri


@pytest.mark.parametrize("verb", ["apply", "delete", "patch", "scale", "exec", "create"])
def test_mutating_verbs_are_structurally_refused(verb):
    """llmdv tests deployed infrastructure; it must have no path to changing it."""
    with pytest.raises(KubeError, match="read-only"):
        KubeContext()._run(verb, "thing")


def test_the_allowlist_contains_no_mutating_verb():
    """Asserted as a property, not as a frozen list.

    The set grows -- `logs` was added for pod-log collection -- and pinning it exactly makes
    every legitimate addition look like a regression. What must never change is that nothing
    in it can alter a cluster.
    """
    mutating = {
        "apply",
        "create",
        "delete",
        "patch",
        "replace",
        "scale",
        "exec",
        "edit",
        "annotate",
        "label",
        "cordon",
        "drain",
        "taint",
        "rollout",
        "set",
        "attach",
    }
    assert not (READ_VERBS & mutating)
    assert "get" in READ_VERBS and "logs" in READ_VERBS


def test_file_and_s3_schemes_are_supported():
    assert parse_remote_uri("file:///tmp/results").location == "/tmp/results"
    target = parse_remote_uri("s3://bucket/some/prefix")
    assert (target.scheme, target.location) == ("s3", "bucket/some/prefix")


def test_pvc_is_no_longer_a_scheme():
    """A mounted volume is a path, which file:// already covers."""
    with pytest.raises(UnsupportedRemote, match="known schemes"):
        parse_remote_uri("pvc://claim/path")


def test_s3_without_a_bucket_fails_loudly():
    """Failing at preflight beats discovering it after a forty-minute suite."""
    with pytest.raises(UnsupportedRemote, match="names no bucket"):
        parse_remote_uri("s3:///prefix-only")


def test_unknown_scheme_lists_the_known_ones():
    with pytest.raises(UnsupportedRemote, match="known schemes"):
        parse_remote_uri("redis://x/y")


def test_missing_scheme_is_rejected():
    with pytest.raises(UnsupportedRemote, match="no scheme"):
        parse_remote_uri("just-a-path")
