"""Bounded CI setup, verification and evidence collection for an existing llm-d deployment.

Only a recorded verification exit 1 fails the step. integration.json and GITHUB_OUTPUT
separately expose setup, interruption and delivery failures, including when nothing ran.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


class IntegrationError(Exception):
    """The integration failed, without establishing a deployment failure."""


class Cancelled(Exception):
    """Unwind a running kubectl/setup command so cancellation can collect evidence."""


def command(argv, *, timeout=60, input=None):
    try:
        result = subprocess.run(
            argv, input=input, text=True, capture_output=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise IntegrationError(f"{argv[0]} could not complete: {type(exc).__name__}") from exc
    if result.returncode:
        raise IntegrationError(f"{argv[0]} exited {result.returncode}: {result.stderr[-2000:]}")
    return result.stdout


def overlay_key(env):
    return "/".join(
        part
        for key in ("ACCELERATOR", "BACKEND", "CONNECTOR", "OFFLOADING_TARGET", "INFRA_PROVIDER")
        for part in env.get(f"LLMDBENCH_CICD_{key}", "").split("/")
        if part
    )


def lookup(env):
    # Parse the whole map before lookup: an unreadable map or broken yq is not an absent row.
    document = json.loads(command(["yq", "-o=json", ".", str(HERE / "overlays.yaml")]))
    if not isinstance(document, dict) or not isinstance(document.get("lanes"), dict):
        raise IntegrationError("overlays.yaml must contain a lanes mapping")
    scenario = env["LLMDBENCH_CICD_SCENARIO"]
    rows = document["lanes"].get(scenario, {})
    if not isinstance(rows, dict):
        raise IntegrationError("scenario rows must be a mapping")
    row = rows.get(overlay_key(env))
    if row is not None:
        if not isinstance(row, dict) or not isinstance(row.get("profile"), str):
            raise IntegrationError("invalid profile row")
        flags = row.get("identity")
        if not isinstance(flags, list) or not all(isinstance(f, str) for f in flags):
            raise IntegrationError("invalid identity flags")
    return row


def validate_collection(directory):
    runs = list(directory.glob("*/outcome.json"))
    if len(runs) != 1:
        raise IntegrationError("collection does not contain exactly one run outcome")
    run = runs[0].parent
    outcome = json.loads(runs[0].read_text())
    if not outcome.get("finished"):
        raise IntegrationError("verification has not finalized its outcome")
    records = []
    for name in ("results.jsonl", "exchanges.jsonl"):
        raw = (run / name).read_text()
        if raw and not raw.endswith("\n"):
            raise IntegrationError(f"incomplete {name}")
        parsed = [json.loads(line) for line in raw.splitlines()]
        if name == "results.jsonl":
            records = parsed
    counts = {}
    for record in records:
        counts[record["outcome"]] = counts.get(record["outcome"], 0) + 1
        if record.get("evidence") and not (run / record["evidence"] / "meta.json").is_file():
            raise IntegrationError("missing case evidence")
    if counts != outcome["verification"]["counts"]:
        raise IntegrationError("results do not match outcome counts")
    for name in ("dashboard.html", "llmdv.log", "profile.used.yaml"):
        if not (run / name).is_file():
            raise IntegrationError(f"missing {name}")


class SmokeStep:
    def __init__(self, env):
        self.env = env
        self.runs = Path(env["RUNNER_TEMP"]) / "llmdv-runs"
        self.runs.mkdir(parents=True, exist_ok=True)
        self.pod = f"llmdv-smoke-{uuid.uuid4().hex[:12]}"
        self.created = False
        self.ready = False
        self.cancelled = False
        self.finalizing = False
        self.rc = None
        self.status = "setup_error"
        self.collection = "not_attempted"
        self.warnings = []
        self.ns = env.get("LLMDBENCH_CICD_NS", "")
        self.ready_timeout = int(env.get("LLMDV_READY_TIMEOUT", "600"))
        self.timeout = int(env.get("LLMDV_TIMEOUT", "900"))
        self.grace = int(env.get("LLMDV_FINALIZE_TIMEOUT", "60"))

    def note(self, message, warning=False):
        sys.stdout.write(("::warning::llmdv: " if warning else "llmdv: ") + message + "\n")
        if warning:
            self.warnings.append(message)
        if self.env.get("GITHUB_STEP_SUMMARY"):
            with Path(self.env["GITHUB_STEP_SUMMARY"]).open("a") as stream:
                stream.write(f"- {'**warning:** ' if warning else ''}{message}\n")

    def kube(self, *args, timeout=60, input=None):
        return command(
            ["kubectl", "--request-timeout=30s", "-n", self.ns, *args],
            timeout=timeout,
            input=input,
        )

    def interrupt(self, _signum, _frame):
        first = not self.cancelled
        self.cancelled = True
        if first and not self.finalizing:
            raise Cancelled

    def runner(self):
        # Always install the requested version in a private venv, never reuse PATH's llmdv.
        venv = self.runs / ".runner-venv"
        command([sys.executable, "-m", "venv", str(venv)], timeout=120)
        python = str(venv / "bin/python")
        command(
            [
                python,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--quiet",
                self.env.get("LLMDV_PACKAGE") or f"llm-d-validate=={self.env['LLMDV_VERSION']}",
            ],
            timeout=300,
        )
        version = command(
            [
                python,
                "-c",
                "from importlib.metadata import version; import sys; "
                "sys.stdout.write(version('llm-d-validate'))",
            ]
        ).strip()
        if version != self.env["LLMDV_VERSION"]:
            raise IntegrationError("installed runner version differs from LLMDV_VERSION")
        return [python, "-m", "llmdv.cli.main"]

    def setup(self):
        for key in ("LLMDV_VERSION", "LLMDBENCH_CICD_NS", "LLMDBENCH_CICD_SCENARIO"):
            if not self.env.get(key):
                raise IntegrationError(f"missing {key}")
        if min(self.timeout, self.ready_timeout, self.grace) <= 0:
            raise IntegrationError("timeouts must be positive")
        row = lookup(self.env)
        if row is None:
            self.status = "skipped"
            self.note("no llm-d-validate profile for this overlay; skipped")
            return False
        self.note(
            f"scenario {self.env['LLMDBENCH_CICD_SCENARIO']}, "
            f"overlay {overlay_key(self.env)}, profile {row['profile']}"
        )
        service = self.env["LLMDBENCH_CICD_SCENARIO"].rstrip("/").split("/")[-1] + "-epp"
        ports = json.loads(self.kube("get", "svc", service, "-o", "json"))["spec"]["ports"]
        port = (
            next((p for p in ports if p.get("port") == 80), None)
            or next((p for p in ports if p.get("name") in ("http", "https")), None)
            or next((p for p in ports if str(p.get("targetPort")) == "8081"), None)
            or (ports[0] if ports else None)
        )
        if port is None:
            raise IntegrationError("router Service has no usable port")
        url = f"http://{service}:{port['port']}"
        exported = command(
            [
                *self.runner(),
                "profile",
                "show",
                str(ROOT / "profiles/llm-d" / (row["profile"] + ".yaml")),
                "--yaml",
            ]
        )
        profile = self.runs / "profile.yaml"
        profile.write_text(exported)
        if self.cancelled:
            return False
        # Cleanup also runs when a create request succeeded but its response was lost.
        self.created = True
        self.kube(
            "create",
            "configmap",
            self.pod,
            f"--from-file=profile.yaml={profile}",
            f"--from-file=pod_runner.py={HERE / 'pod_runner.py'}",
        )
        argv = ["llmdv", "verify", "--profile", "/etc/llmdv/profile.yaml"]
        for flag in row["identity"]:
            argv.extend(["--identity", flag])
        argv.extend(
            [
                "--test-set",
                "smoke",
                "--pod-logs",
                "off",
                "--analyze",
                "--local-output",
                "/work/runs",
            ]
        )
        pod = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": self.pod,
                "labels": {"app.kubernetes.io/name": "llmdv", "llm-d-benchmark/ephemeral": "true"},
            },
            "spec": {
                "restartPolicy": "Never",
                "terminationGracePeriodSeconds": self.grace,
                "activeDeadlineSeconds": self.ready_timeout + self.timeout + self.grace + 300,
                "automountServiceAccountToken": False,
                "securityContext": {
                    "runAsNonRoot": True,
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
                "containers": [
                    {
                        "name": "llmdv",
                        "image": self.env.get("LLMDV_IMAGE")
                        or f"ghcr.io/llm-d-incubation/llm-d-validate:v{self.env['LLMDV_VERSION']}",
                        "command": ["python", "/etc/llmdv/pod_runner.py"],
                        "env": [
                            {"name": "LLMDV_ROUTER_URL", "value": url},
                            {"name": "LLMDV_COMMAND", "value": json.dumps(argv)},
                        ],
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]},
                        },
                        "resources": {
                            "requests": {"cpu": "100m", "memory": "256Mi"},
                            "limits": {"memory": "1Gi"},
                        },
                        "volumeMounts": [
                            {"name": "profile", "mountPath": "/etc/llmdv", "readOnly": True},
                            {"name": "work", "mountPath": "/work"},
                            {"name": "tmp", "mountPath": "/tmp"},
                        ],
                    }
                ],
                "volumes": [
                    {"name": "profile", "configMap": {"name": self.pod}},
                    {"name": "work", "emptyDir": {}},
                    {"name": "tmp", "emptyDir": {}},
                ],
            },
        }
        self.kube("apply", "-f", "-", input=json.dumps(pod))
        self.kube(
            "wait",
            "--for=condition=Ready",
            f"pod/{self.pod}",
            f"--timeout={self.ready_timeout}s",
            timeout=self.ready_timeout + 10,
        )
        self.ready = True
        return True

    def wait(self, seconds, *, cancellable=True):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if cancellable and self.cancelled:
                return False
            try:
                value = self.kube(
                    "exec",
                    self.pod,
                    "--",
                    "cat",
                    "/work/exit",
                    timeout=min(10, max(1, end - time.monotonic())),
                ).strip()
                if value in {str(n) for n in range(7)}:
                    self.rc = int(value)
                    return True
            except IntegrationError:
                pass
            time.sleep(min(5, max(0, end - time.monotonic())))
        return False

    def stop(self):
        # A completion can race the timeout/cancellation. Do not signal a reaped PID.
        if self.rc is not None or self.wait(1, cancellable=False):
            return
        self.kube(
            "exec",
            self.pod,
            "--",
            "python",
            "-c",
            "import os,signal; from pathlib import Path; "
            "os.kill(int(Path('/work/verify.pid').read_text()),signal.SIGTERM)",
        )
        if not self.wait(self.grace, cancellable=False):
            self.note(
                "verification did not finalize after interruption; evidence may be incomplete", True
            )

    def collect(self):
        try:
            (self.runs / "console.txt").write_text(self.kube("logs", self.pod))
        except IntegrationError as exc:
            self.note(f"could not collect console: {exc}", True)
        partial = self.runs / "runs.partial"
        for attempt in range(3):
            shutil.rmtree(partial, ignore_errors=True)
            try:
                self.kube("cp", f"{self.pod}:/work/runs", str(partial))
                validate_collection(partial)
                partial.replace(self.runs / "runs")
                self.collection = "complete"
                self.note("finalized run directory collected for artifact upload")
                return
            except (IntegrationError, OSError, ValueError, KeyError) as exc:
                self.note(f"collection attempt {attempt + 1} failed: {exc}", True)
                if attempt < 2:
                    time.sleep(2)
        self.collection = "failed"
        self.note(
            "run collection failed; only available diagnostics/partial files can be uploaded", True
        )

    def finish(self):
        self.finalizing = True
        if self.created:
            for kind in ("pod", "configmap"):
                try:
                    self.kube("delete", kind, self.pod, "--wait=false", "--ignore-not-found")
                except IntegrationError as exc:
                    self.note(f"cleanup failed: {exc}", True)
        shutil.rmtree(self.runs / ".runner-venv", ignore_errors=True)
        body = {
            "status": self.status,
            "verification_exit": self.rc,
            "collection_status": self.collection,
            "warnings": self.warnings,
        }
        (self.runs / "integration.json").write_text(json.dumps(body, indent=2) + "\n")
        if self.env.get("GITHUB_OUTPUT"):
            with Path(self.env["GITHUB_OUTPUT"]).open("a") as stream:
                for key in ("status", "verification_exit", "collection_status"):
                    stream.write(f"{key}={body[key] if body[key] is not None else ''}\n")
        self.note(
            f"integration {self.status}; verification exit {self.rc}; collection {self.collection}"
        )

    def run(self):
        try:
            if self.setup():
                if self.wait(self.timeout):
                    self.status = "completed"
                    if self.rc not in (0, 1):
                        self.note(f"verification exited {self.rc}; not a guide failure", True)
                else:
                    self.status = "interrupted" if self.cancelled else "timed_out"
                    self.note(f"verification {self.status}; requesting finalization", True)
                    try:
                        self.stop()
                    except IntegrationError as exc:
                        self.note(f"could not interrupt verification: {exc}", True)
                self.collect()
            elif self.cancelled:
                self.status = "interrupted"
        except Cancelled:
            self.status = "interrupted"
            self.note("integration interrupted; collecting available evidence", True)
            if self.created:
                try:
                    self.stop()
                except IntegrationError as exc:
                    self.note(f"could not interrupt verification: {exc}", True)
                self.collect()
        except (IntegrationError, OSError, ValueError, KeyError, TypeError) as exc:
            self.note(f"integration failed, not a guide failure: {exc}", True)
        finally:
            self.finish()
        return 1 if self.rc == 1 else 0


def main():
    try:
        step = SmokeStep(os.environ)
    except (OSError, ValueError, KeyError) as exc:
        sys.stderr.write(f"::warning::llmdv: could not initialize integration: {exc}\n")
        return 0
    signal.signal(signal.SIGINT, step.interrupt)
    signal.signal(signal.SIGTERM, step.interrupt)
    return step.run()


if __name__ == "__main__":
    sys.exit(main())
