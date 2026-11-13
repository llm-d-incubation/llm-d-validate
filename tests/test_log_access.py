"""Whether a run can collect pod logs, decided before the suite rather than discovered after.

These exist because of a specific wrong behaviour: a run against an endpoint with no cluster
access printed "collecting pod logs", created an empty `logs/` directory, and wrote a summary
section explaining how to correlate against it. Three signals saying logs happened, and none
had. The states below all used to be that same empty directory.
"""

from __future__ import annotations

import shutil
import textwrap
from urllib.parse import parse_qs, urlsplit

import pytest

from llmdv import preflight
from llmdv.cli.verify_cmd import _pod_log_section
from llmdv.collect import Collection, KubectlLogCollector, NoopCollector
from llmdv.kube import NO_CLI_REASON, KubeContext
from llmdv.preflight import LogAccess
from llmdv.profile import loader
from tests.fakes import FakeKubeAPI

TEMPLATE = """
schema: llmdv.profile/1
name: la
{environment}
model: {{public_id: m}}
ingresses:
  - id: one
    kind: vllm
    base_url: http://localhost:1
    surfaces: [chat_completions]
{logs}
"""

LOGS_BLOCK = """    logs:
      - {target: svc/engine, container: modelserver}
"""


def build(tmp_path, *, namespace="ns", logs=True):
    path = tmp_path / "la.yaml"
    path.write_text(
        textwrap.dedent(
            TEMPLATE.format(
                environment=f"environment: {{namespace: {namespace}}}" if namespace else "",
                logs=LOGS_BLOCK if logs else "",
            )
        )
    )
    return loader.load(path)


class FakeKube:
    """kubectl, with the two access answers dialled in independently."""

    unavailable_reason = NO_CLI_REASON

    def __init__(self, available=True, logs_readable=True, pods_listable=True):
        self.available = available
        self._logs = logs_readable
        self._pods = pods_listable
        self.probed: list[str] = []

    def can_read_pods(self, namespace):
        return self._pods

    def can_read_pod_logs(self, namespace):
        self.probed.append(namespace)
        return self._logs


def verdict(profile, kube, require_logs=False):
    report = preflight.Report()
    preflight._check_log_access(report, profile, ["one"], kube, require_logs)
    return report


# ----------------------------------------------------------- probe classification


def classify(monkeypatch, stderr):
    """Drive can_read_pod_logs with one kubectl failure, and report its verdict."""
    from llmdv.kube import KubeError

    def fake(self, verb, *rest, timeout=None):
        raise KubeError(stderr)

    monkeypatch.setattr(KubeContext, "_run", fake)
    return KubeContext().can_read_pod_logs("ns")


def test_forbidden_means_denied(monkeypatch):
    assert classify(monkeypatch, 'Error from server (Forbidden): pods "x" is forbidden') is False


def test_not_found_means_permitted(monkeypatch):
    """NotFound from the log subresource means authorization passed but the pod is absent."""
    stderr = 'Error from server (NotFound): pods "llmdv-access-probe" not found'
    assert classify(monkeypatch, stderr) is True


def test_anything_else_is_inconclusive(monkeypatch):
    """An unreachable cluster must not read as either answer -- that is how empty logs/ happen."""
    assert classify(monkeypatch, "Unable to connect to the server: i/o timeout") is None


@pytest.mark.skipif(not shutil.which("kubectl"), reason="kubectl is required for the wire probe")
@pytest.mark.parametrize("allow_logs", [False, True], ids=["denied", "permitted"])
def test_probe_checks_log_access_even_when_the_pod_is_absent(tmp_path, monkeypatch, allow_logs):
    """Real kubectl must reach pods/log; a preceding Pod 404 cannot establish log access."""
    with FakeKubeAPI(allow_logs=allow_logs) as api:
        base = [
            "kubectl",
            "--kubeconfig=/dev/null",
            f"--server={api.base_url}",
            f"--cache-dir={tmp_path / 'cache'}",
            "--request-timeout=3s",
        ]
        monkeypatch.setattr(KubeContext, "_base", lambda self: base)
        report = verdict(build(tmp_path), KubeContext(), require_logs=True)

    assert report.log_access is (LogAccess.AVAILABLE if allow_logs else LogAccess.DENIED)
    assert report.ok is allow_logs
    assert len(api.requests) == 1
    request = urlsplit(api.requests[0])
    assert request.path == "/api/v1/namespaces/ns/pods/llmdv-access-probe/log"
    assert parse_qs(request.query)["tailLines"] == ["1"]


# ----------------------------------------------------------- the verdict matrix


def test_no_declared_source_is_not_declared(tmp_path):
    report = verdict(build(tmp_path, logs=False), FakeKube())
    assert report.log_access is LogAccess.NOT_DECLARED


def test_declared_without_a_cluster_cli(tmp_path):
    report = verdict(build(tmp_path), FakeKube(available=False))
    assert report.log_access is LogAccess.NO_CLUSTER_CLI
    assert report.log_access.value == "no_kubectl"
    assert report.log_access_reason == NO_CLI_REASON


def test_declared_without_a_namespace(tmp_path):
    report = verdict(build(tmp_path, namespace=""), FakeKube())
    assert report.log_access is LogAccess.NO_NAMESPACE


def test_declared_but_forbidden(tmp_path):
    report = verdict(build(tmp_path), FakeKube(logs_readable=False))
    assert report.log_access is LogAccess.DENIED


def test_declared_and_inconclusive_is_unknown(tmp_path):
    report = verdict(build(tmp_path), FakeKube(logs_readable=None))
    assert report.log_access is LogAccess.UNKNOWN


def test_declared_and_permitted(tmp_path):
    report = verdict(build(tmp_path), FakeKube())
    assert report.log_access is LogAccess.AVAILABLE
    assert not report.warnings


def test_no_namespace_never_probes_the_cluster(tmp_path):
    """The endpoint-only case must cost zero kubectl calls."""
    kube = FakeKube()
    verdict(build(tmp_path, namespace="", logs=False), kube)
    assert kube.probed == []


# ----------------------------------------------------------- what it reports


def test_access_you_are_not_using_is_reported(tmp_path):
    """The declared-absent-but-present direction: evidence left on the table."""
    report = verdict(build(tmp_path, logs=False), FakeKube())
    assert report.ok
    assert any("no selected ingress declares a log source" in w.message for w in report.warnings)


def test_declared_but_unreachable_warns_without_failing(tmp_path):
    """Default runs must not fail for missing logs -- the tests themselves never needed them."""
    report = verdict(build(tmp_path), FakeKube(logs_readable=False))
    assert report.ok
    assert any("forbidden" in w.message for w in report.warnings)


def test_asking_for_all_logs_without_access_is_an_environment_problem(tmp_path):
    """Exit 3 before the first request, not after a suite that cannot produce the evidence."""
    report = verdict(build(tmp_path), FakeKube(logs_readable=False), require_logs=True)
    assert not report.ok
    assert "--pod-logs all" in report.problems[0].message


def test_asking_for_all_logs_with_nothing_declared_is_not_a_problem(tmp_path):
    """Nothing was promised, so nothing is broken; the unused-access note still fires."""
    report = verdict(build(tmp_path, logs=False), FakeKube(), require_logs=True)
    assert report.ok


# ----------------------------------------------------------- no empty directory


def test_noop_collector_creates_no_directory_and_carries_its_reason(tmp_path, monkeypatch):
    out = tmp_path / "logs"
    collection = NoopCollector("kubectl is not on PATH").collect(None, [], None, out)
    assert not out.exists()
    assert collection.reason == "kubectl is not on PATH"


def test_collector_with_no_declared_source_creates_no_directory(tmp_path):
    profile = build(tmp_path, logs=False)
    out = tmp_path / "logs"
    collection = KubectlLogCollector(FakeKube(), "ns").collect(profile, ["one"], None, out)
    assert not out.exists()
    assert collection.reason == "no selected ingress declares a log source"


# ----------------------------------------------------------- the summary section


def test_summary_explains_why_there_are_no_logs():
    lines = _pod_log_section(Collection(reason="no cluster access"))
    text = "\n".join(lines)
    assert "no cluster access" in text
    # The failure this guards: telling the reader to correlate against files that do not exist.
    assert "Correlating the pod logs" not in text
    assert "`logs/` holds" not in text


def test_summary_explains_how_to_correlate_when_logs_are_there():
    from llmdv.collect.base import CollectedLog

    collected = Collection(logs=[CollectedLog("one", "pod-a", "c", path="logs/a.log", lines=5)])
    text = "\n".join(_pod_log_section(collected))
    assert "Correlating the pod logs" in text


def test_summary_never_leaves_the_reader_with_neither():
    """Every state says something: how to read them, or why there are none."""
    for collection in (None, Collection(), Collection(reason="not collected (--pod-logs off)")):
        text = "\n".join(_pod_log_section(collection))
        assert "None were collected" in text
        assert "Correlating the pod logs" not in text
