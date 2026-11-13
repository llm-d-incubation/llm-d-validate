"""Output sinks: local run directories and remote publication."""

from .base import ObjectSink, OutputSink
from .local import LocalDirSink, default_run_dir
from .offload import OFFLOAD, Offloader, reference, run_prefix, whole
from .remote import RemoteTarget, UnsupportedRemote, parse_remote_uri, sink_for

__all__ = [
    "LocalDirSink",
    "OutputSink",
    "ObjectSink",
    "OFFLOAD",
    "Offloader",
    "RemoteTarget",
    "UnsupportedRemote",
    "default_run_dir",
    "parse_remote_uri",
    "sink_for",
    "reference",
    "run_prefix",
    "whole",
]
