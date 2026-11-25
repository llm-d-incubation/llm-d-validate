"""Public S3 helper separator conventions coexist with strict archive path validation."""

from __future__ import annotations

import io

import pytest

from llmdv.output.paths import UnsafeName
from llmdv.output.remote import parse_remote_uri
from llmdv.output.s3 import S3Sink


class Store:
    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return self

    def paginate(self, **kwargs):
        assert kwargs == {"Bucket": "example", "Prefix": "archive/profile/"}
        return [{"Contents": [{"Key": "archive/profile/run/results.jsonl"}]}]

    def get_object(self, **kwargs):
        assert kwargs == {"Bucket": "example", "Key": "archive/profile/run/results.jsonl"}
        return {"Body": io.BytesIO(b"recorded evidence")}


@pytest.mark.parametrize("prefix", ["profile", "profile/", "/profile/"])
def test_public_read_helpers_accept_directory_style_prefixes(prefix):
    sink = S3Sink(parse_remote_uri("s3://example/archive"))
    sink._s3 = Store()
    assert sink.list_keys(prefix) == ["run/results.jsonl"]
    assert sink.key_for(prefix, "run/", "results.jsonl") == "archive/profile/run/results.jsonl"
    assert sink.get_bytes(prefix, "run/", "results.jsonl") == b"recorded evidence"


def test_optional_empty_key_components_keep_their_original_meaning():
    sink = S3Sink(parse_remote_uri("s3://example/archive"))
    assert (
        sink.key_for("", "profile/", "", "run", "results.jsonl")
        == "archive/profile/run/results.jsonl"
    )
    assert sink.key_for("") == "archive"
    assert sink.key_for() == "archive"


@pytest.mark.parametrize(
    "path", ["../outside/", "/profile/../outside/", "/./", "profile\\outside", "profile\x00"]
)
def test_boundary_normalization_never_hides_unsafe_components(path):
    sink = S3Sink(parse_remote_uri("s3://example/archive"))
    with pytest.raises(UnsafeName):
        sink.key_for(path, "results.jsonl")
