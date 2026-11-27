"""Bounded CI setup, verification and evidence collection for an existing llm-d deployment.

Only a recorded verification exit 1 fails the step. integration.json and GITHUB_OUTPUT
separately expose setup, interruption and delivery failures, including when nothing ran.
"""

from __future__ import annotations

import argparse
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
COMMAND_DEADLINE = None


class IntegrationError(Exception):
    """The integration failed, without establishing a deployment failure."""


class Cancelled(Exception):
    """Unwind a running kubectl/setup command so cancellation can collect evidence."""


def command(argv, *, timeout=60, input=None, env=None):
    if COMMAND_DEADLINE is not None:
        remaining = COMMAND_DEADLINE - time.monotonic()
        if remaining <= 0:
            raise IntegrationError("integration phase exhausted its time budget")
        timeout = min(timeout, remaining)
    try:
        result = subprocess.run(
            argv, input=input, text=True, capture_output=True, timeout=timeout, check=False, env=env
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


def profile_root(env):
    return Path(env.get("LLMDV_PROFILE_ROOT") or ROOT).resolve()


def contained(root, path):
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise IntegrationError("profile files must stay within the profile checkout")
    return resolved


def validate_inheritance(root, path, seen=None):
    # Check containment before opening each parent, including symlinks. The installed
    # runtime remains responsible for schema, composition and capability validation.
    seen = set() if seen is None else seen
    path = contained(root, path)
    if path in seen:
        raise IntegrationError("profile inheritance repeats a file")
    seen.add(path)
    body = json.loads(command(["yq", "-o=json", ".", str(path)]))
    if not isinstance(body, dict):
        raise IntegrationError("profile must be a mapping")
    parents = body.get("extends", [])
    if isinstance(parents, str):
        parents = [parents]
    if not isinstance(parents, list) or not all(isinstance(p, str) for p in parents):
        raise IntegrationError("invalid profile inheritance")
    for parent in parents:
        validate_inheritance(root, path.parent / parent, seen)


def lookup(env):
    # Parse the whole map before lookup: an unreadable map or broken yq is not an absent row.
    document = json.loads(
        command(
            [
                "yq",
                "-o=json",
                ".",
                str(contained(profile_root(env), "deploy/llm-d-nightly/overlays.yaml")),
            ]
        )
    )
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
        self.state = Path(env["RUNNER_TEMP"]) / "llmdv-state.json"
        self.source = {}
        self.phase = "run"
        self.cancellation_recovery = False

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
        return [python, "-I", "-m", "llmdv.cli.main"]

    def setup(self):
        for key in ("LLMDV_VERSION", "LLMDBENCH_CICD_NS", "LLMDBENCH_CICD_SCENARIO"):
            if not self.env.get(key):
                raise IntegrationError(f"missing {key}")
        if min(self.timeout, self.ready_timeout, self.grace) <= 0:
            raise IntegrationError("timeouts must be positive")
        explicit = self.env.get("LLMDV_PROFILE", "")
        row = {"profile": explicit, "identity": []} if explicit else lookup(self.env)
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
        root = profile_root(self.env)
        selected = explicit or "profiles/llm-d/" + row["profile"] + ".yaml"
        selected = contained(root, selected)
        validate_inheritance(root, selected)
        runner = self.runner()
        exported = command([*runner, "profile", "show", str(selected), "--yaml"])
        profile = self.runs / "profile.yaml"
        profile.write_text(exported)
        document = json.loads(command(["yq", "-o=json", ".", str(profile)]))
        router = next((i for i in document.get("ingresses", []) if i.get("id") == "router"), {})
        if (
            router.get("base_url_env") != "LLMDV_ROUTER_URL"
            or router.get("base_url")
            or router.get("kind") != "router"
        ):
            raise IntegrationError("profile must bind its router ingress through LLMDV_ROUTER_URL")
        # An override must be runnable by this installed runtime before creating a pod.
        # Restrict it to the CI router; never test an unrelated ingress from the fork.
        command(
            [
                *runner,
                "plan",
                "--profile",
                str(profile),
                "--ingress",
                "router",
                "--test-set",
                "smoke",
                "-q",
            ],
            env={**self.env, "LLMDV_ROUTER_URL": url},
        )
        self.source = {
            "runtime_version": self.env["LLMDV_VERSION"],
            "runtime_commit": command(["git", "-C", str(ROOT), "rev-parse", "HEAD"]).strip(),
            "repository": self.env.get("LLMDV_PROFILE_REPOSITORY")
            or "llm-d-incubation/llm-d-validate",
            "ref": self.env.get("LLMDV_PROFILE_REF") or "v" + self.env["LLMDV_VERSION"],
            "commit": command(["git", "-C", str(root), "rev-parse", "HEAD"]).strip(),
            "path": selected.relative_to(root).as_posix(),
            "selection": "explicit" if explicit else "mapped",
        }
        (self.runs / "profile-source.json").write_text(json.dumps(self.source, indent=2) + "\n")
        if self.cancelled:
            return False
        # Cleanup also runs when a create request succeeded but its response was lost.
        self.created = True
        self.checkpoint()  # Persist object identity before the first cluster write.
        self.kube(
            "create",
            "configmap",
            self.pod,
            f"--from-file=profile.yaml={profile}",
            f"--from-file=pod_runner.py={HERE / 'pod_runner.py'}",
        )
        argv = ["llmdv", "verify", "--profile", "/etc/llmdv/profile.yaml", "--ingress", "router"]
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
        if COMMAND_DEADLINE is not None:
            end = min(end, COMMAND_DEADLINE)
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
                    self.checkpoint()  # Preserve a verdict before collection or cleanup.
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
            timeout=10,
        )
        if not self.wait(self.grace, cancellable=False):
            self.note(
                "verification did not finalize after interruption; evidence may be incomplete", True
            )

    def collect(self):
        if self.collection == "complete":
            validate_collection(self.runs / "runs")
            return
        self.collection = "failed"
        try:
            (self.runs / "console.txt").write_text(self.kube("logs", self.pod, timeout=15))
        except IntegrationError as exc:
            self.note(f"could not collect console: {exc}", True)
        for attempt in range(3):
            partial = self.runs / f"runs.partial-{uuid.uuid4().hex[:8]}"
            try:
                self.kube("cp", f"{self.pod}:/work/runs", str(partial), timeout=60)
                validate_collection(partial)
                partial.replace(self.runs / "runs")
                self.collection = "complete"
                self.note("finalized run directory collected for artifact upload")
                return
            except (IntegrationError, OSError, ValueError, KeyError, TypeError) as exc:
                self.note(f"collection attempt {attempt + 1} failed: {exc}", True)
                if attempt < 2:
                    time.sleep(2)
        self.collection = "failed"
        self.note(
            "run collection failed; only available diagnostics/partial files can be uploaded", True
        )

    def checkpoint(self):
        body = {
            key: getattr(self, key)
            for key in (
                "pod",
                "created",
                "ready",
                "rc",
                "status",
                "collection",
                "warnings",
                "ns",
                "ready_timeout",
                "timeout",
                "grace",
                "source",
            )
        }
        body["protocol"] = 2
        pending = self.state.with_suffix(".tmp")
        pending.write_text(json.dumps(body) + "\n")
        pending.replace(self.state)
        self.report()

    def restore(self):
        body = json.loads(self.state.read_text())
        if body.pop("protocol", None) != 2:
            raise IntegrationError("unsupported saved integration state")
        for key in (
            "pod",
            "created",
            "ready",
            "rc",
            "status",
            "collection",
            "warnings",
            "ns",
            "ready_timeout",
            "timeout",
            "grace",
            "source",
        ):
            setattr(self, key, body[key])

    def report(self):
        body = {
            "status": self.status,
            "verification_exit": self.rc,
            "collection_status": self.collection,
            "warnings": self.warnings,
        }
        pending = self.runs / "integration.tmp"
        pending.write_text(json.dumps(body, indent=2) + "\n")
        pending.replace(self.runs / "integration.json")
        if self.env.get("GITHUB_OUTPUT"):
            with Path(self.env["GITHUB_OUTPUT"]).open("a") as stream:
                for key in ("status", "verification_exit", "collection_status"):
                    stream.write(f"{key}={body[key] if body[key] is not None else ''}\n")

    def finish(self):
        global COMMAND_DEADLINE
        if self.phase == "finalize":
            COMMAND_DEADLINE = time.monotonic() + (20 if self.cancellation_recovery else 40)
        self.finalizing = True
        self.checkpoint()
        if self.created:
            for kind in ("pod", "configmap"):
                try:
                    self.kube(
                        "delete", kind, self.pod, "--wait=false", "--ignore-not-found", timeout=20
                    )
                except IntegrationError as exc:
                    self.note(f"cleanup failed: {exc}", True)
        shutil.rmtree(self.runs / ".runner-venv", ignore_errors=True)
        self.checkpoint()
        self.note(
            f"integration {self.status}; verification exit {self.rc}; collection {self.collection}"
        )

    def staged(self, phase):
        global COMMAND_DEADLINE
        self.phase = phase
        if phase != "prepare":
            self.restore()
        self.cancellation_recovery = phase == "finalize" and (
            self.env.get("LLMDV_CANCELLED") == "true"
            or self.status == "interrupted"
            or (self.status == "prepared" and self.ready)
        )
        if self.cancellation_recovery:
            # Actions force-stops cancelled workflows after five minutes. Leave most
            # of that window for the guide's existing teardown and artifact delivery.
            COMMAND_DEADLINE = time.monotonic() + 60
            self.grace = min(self.grace, 20)
        self.checkpoint()
        try:
            if phase == "prepare":
                if self.setup():
                    self.status = "prepared"
            elif phase == "verify":
                if self.ready:
                    self.status = "completed" if self.wait(self.timeout) else "timed_out"
            elif phase == "finalize":
                self.finalizing = True
                if self.created:
                    if self.rc is None:
                        if self.status in ("prepared", "setup_error"):
                            self.status = "interrupted"
                        try:
                            self.stop()
                        except IntegrationError as exc:
                            self.note(f"could not interrupt verification: {exc}", True)
                        # A partial prepare can already have started the pod's test process.
                    self.collect()
        except Cancelled:
            # Runner cancellation has only a short grace. Persist and exit immediately;
            # a separate always() step owns stopping, collection and cleanup.
            self.status = "interrupted"
        except (IntegrationError, OSError, ValueError, KeyError, TypeError) as exc:
            self.note(f"integration failed, not a guide failure: {exc}", True)
        finally:
            self.checkpoint()
            if phase == "finalize":
                self.finish()
        return 1 if self.rc == 1 else 0

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
    global COMMAND_DEADLINE
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "phase", nargs="?", choices=("run", "prepare", "verify", "finalize"), default="run"
    )
    parser.add_argument("--protocol-version", action="store_true")
    args = parser.parse_args()
    if args.protocol_version:
        sys.stdout.write("2\n")
        return 0
    if args.phase != "run":
        COMMAND_DEADLINE = (
            time.monotonic() + {"prepare": 900, "verify": 910, "finalize": 180}[args.phase]
        )
    try:
        step = SmokeStep(os.environ)
    except (OSError, ValueError, KeyError) as exc:
        sys.stderr.write(f"::warning::llmdv: could not initialize integration: {exc}\n")
        return 0
    signal.signal(signal.SIGINT, step.interrupt)
    signal.signal(signal.SIGTERM, step.interrupt)
    return step.run() if args.phase == "run" else step.staged(args.phase)


if __name__ == "__main__":
    sys.exit(main())
