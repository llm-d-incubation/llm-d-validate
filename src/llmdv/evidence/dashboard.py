"""The run dashboard: one self-contained HTML file for triaging a run.

Built from the run directory alone -- outcome.json, env.json, results.jsonl, exchanges.jsonl,
profile.used.yaml, reproduce.sh, summary.md, MANIFEST.json, alerts.json, logs/ and the per-case
bundles -- so it works on a finished run, a killed one, or one fetched back from storage.
`llmdv verify --analyze` and `llmdv publish --analyze` build it as a run finishes; `llmdv analyze`
builds it for any run directory.

It is a reading aid over evidence llmdv already wrote, and three rules follow from that:
- nothing about it can change a run's exit code or stop the run being published
  (`write_best_effort`);
- it refuses to carry a credential it can recognise. Its data is gzip-compressed inside the
  HTML, where a grep proves nothing, so `write` scans the uncompressed data and writes nothing
  on a match. Two scans work anywhere: credential-shaped strings (`sk-...`, `Bearer ...`,
  `AKIA...`), and fields named like a credential (`authorization`, `api_key`, `access_token`,
  `password`, ...) holding a value that is not the redaction sentinel. A third matches literal
  values, and finds only what it is given: the secrets llmdv registered in this process (verify,
  publish) and the credential variables set in this environment (the profile's key_env
  variables, the S3, COS and webhook credentials). A run analysed where none of those are set
  gets the first two scans only. No scan can prove the absence of an arbitrary secret;
- it shows only what llmdv recorded. It draws no conclusions: no debugging hints, no
  verdict on which layer is at fault, only signals found and not found.
"""

from __future__ import annotations

import base64
import collections
import gzip
import html
import json
import os
import re
import shlex
from datetime import datetime
from pathlib import Path

import yaml

from ..http import redact
from ..log.setup import get_logger

log = get_logger("evidence.dashboard")

FILENAME = "dashboard.html"
TEMPLATE = Path(__file__).with_name("dashboard.html")

EXIT_MEANING = {
    0: ("ok", "Every collected case passed or skipped."),
    1: ("fleet", "Test failures: the system under test did not behave as the profile declares."),
    2: ("usage", "Usage error or invalid profile; nothing was verified."),
    3: ("preflight", "Preflight unsatisfied: the operator's environment, not the fleet."),
    4: ("internal", "llmdv raised: a defect in the test library itself."),
    5: (
        "not_delivered",
        "Verification finished but results or the notification were not delivered.",
    ),
    6: ("interrupted", "The run was interrupted before it finished."),
}

SKIP_MEANING = {
    "undeclared": "The profile does not declare it; nothing was sent.",
    "not_implemented": "llmdv cannot drive it yet: a tool gap.",
    "requires_instrumentation": "Needs an instrument this run lacks.",
    "requires_load": "Needs a load experiment.",
    "excluded_by_policy": "A shared fleet declined the disruptive families.",
    "throttled": "A declared rate-limited layer answered 429.",
}


# ----------------------------------------------------------------------------------------- read


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def read_text(path: Path, cap: int | None = None) -> str | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if cap and len(text) > cap:
        return text[:cap] + f"\n… [truncated: {len(text) - cap} more characters in {path.name}]"
    return text


def read_jsonl(path: Path):
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue  # a torn last line of a killed run
    except OSError:
        return


def clip(value, cap: int):
    """A body as text, capped, with the cut marked."""
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)
    if len(text) > cap:
        return text[:cap] + f"\n… [truncated: {len(text) - cap} more characters]"
    return text


# ---------------------------------------------------------------------------------- the profile


def load_profile(run: Path):
    raw = read_text(run / "profile.used.yaml")
    data = None
    if raw:
        try:
            data = yaml.safe_load(raw)
        except Exception:  # noqa: BLE001 -- a broken profile must not break the page
            data = None
    return raw, data if isinstance(data, dict) else {}


def as_list(value) -> list:
    """`value` if it is a list, else []: a hand-written profile can put anything anywhere."""
    return value if isinstance(value, list) else []


def ingress_auth_env(profile: dict) -> dict[str, str | None]:
    out = {}
    for ing in as_list(profile.get("ingresses")):
        if not isinstance(ing, dict):
            continue
        auth = ing.get("auth") if isinstance(ing.get("auth"), dict) else {}
        out[str(ing.get("id"))] = auth.get("key_env")
    return out


# -------------------------------------------------------------------------------- the evidence


def bundle_dir(run: Path, rec: dict) -> Path | None:
    evidence = rec.get("evidence")
    if evidence:
        candidate = run / evidence
        if candidate.is_dir():
            return candidate
    # Older layout: failures/<ingress>__<case>[__<variant>]
    ingress, case, variant = rec.get("ingress"), rec.get("case_id"), rec.get("variant")
    names = (
        [f"{ingress}__{case}__{variant}", f"{ingress}__{case}"]
        if variant
        else [f"{ingress}__{case}"]
    )
    for root in ("failures", "cases"):
        for name in names:
            candidate = run / root / name
            if candidate.is_dir():
                return candidate
    return None


def curl_for(exchange: dict, key_env: str | None, body_cap: int) -> str:
    method = exchange.get("method") or "GET"
    url = exchange.get("url") or ""
    parts = ["curl", "-sS", "-i", "-X", method, shlex.quote(url)]
    for name, value in (exchange.get("request_headers") or {}).items():
        if name.lower() in ("authorization", "x-api-key", "api-key"):
            if key_env:
                value = (
                    f"Bearer ${{{key_env}}}"
                    if name.lower() == "authorization"
                    else f"${{{key_env}}}"
                )
                parts += ["-H", f'"{name}: {value}"']
            continue
        if name.lower() in ("content-length", "host", "accept-encoding", "connection"):
            continue
        parts += ["-H", shlex.quote(f"{name}: {value}")]
    body = exchange.get("request_body")
    script = " ".join(parts)
    if body is not None:
        text = body if isinstance(body, str) else json.dumps(body, indent=2, ensure_ascii=False)
        if len(text) > body_cap * 4:
            return (
                script
                + f" --data-binary @request.json   # body is {len(text)} characters;"
                + " use the bundle's request.json"
            )
        script += " --data-binary @- <<'BODY'\n" + text + "\nBODY"
    guard = f': "${{{key_env}:?set {key_env} first}}"\n' if key_env else ""
    return guard + script


# ------------------------------------------------------------------------------ stack signals


def stack_signals(exchanges_by_node: dict[str, list[dict]]) -> list[dict]:
    """What the recorded traffic says about the layers behind each ingress, found or not.

    Deliberately broader than the `stack` block outcome.json publishes (evidence/stack.py):
    this page stays with the run directory, so it may show the gateway's api-base and the
    whole router header family, which name internal services. dashboard.html reads these
    exact keys, including the `router header: ` prefix.
    """
    per = collections.defaultdict(lambda: collections.defaultdict(collections.Counter))
    epp = collections.Counter()
    for ex_list in exchanges_by_node.values():
        for e in ex_list:
            ing = e.get("ingress") or "?"
            h = {k.lower(): str(v) for k, v in (e.get("response_headers") or {}).items()}
            for name in (
                "server",
                "x-litellm-version",
                "x-litellm-model-name",
                "x-litellm-model-api-base",
            ):
                if name in h:
                    per[ing][name][h[name][:120]] += 1
            for name in h:
                if name.startswith(
                    ("x-llm-d", "x-went", "x-envoy", "x-gateway", "x-inference", "x-epp")
                ):
                    per[ing]["router header: " + name][h[name][:80]] += 1
            body = e.get("response_body")
            if isinstance(body, dict) and body.get("system_fingerprint"):
                per[ing]["system_fingerprint"][str(body["system_fingerprint"])[:80]] += 1
            text = (
                json.dumps(body)[:4000]
                if body is not None
                else str(e.get("response_text") or "")[:4000]
            )
            if "inference error:" in text:
                epp[ing] += 1
    rows = []
    for ing, signals in per.items():
        found = {k: dict(v.most_common(3)) for k, v in signals.items()}
        rows.append({"ingress": ing, "found": found, "epp_error_bodies": epp.get(ing, 0)})
    return rows


# --------------------------------------------------------------------------------------- build


def build(run: Path, body_cap: int, max_exchanges: int) -> dict:
    outcome = read_json(run / "outcome.json", {}) or {}
    env = read_json(run / "env.json", None)
    manifest = read_json(run / "MANIFEST.json", None)
    alerts = read_json(run / "alerts.json", None)
    comparison = read_json(run / "comparison.json", None)
    profile_raw, profile = load_profile(run)
    key_envs = ingress_auth_env(profile)
    reproduce = read_text(run / "reproduce.sh")
    summary_md = read_text(run / "summary.md", 20000)

    records = list(read_jsonl(run / "results.jsonl"))
    wanted = {r.get("nodeid") for r in records}

    exchanges_by_node: dict[str, list[dict]] = collections.defaultdict(list)
    for e in read_jsonl(run / "exchanges.jsonl"):
        node = e.get("nodeid")
        if node in wanted:
            exchanges_by_node[node].append(e)

    tests = []
    for rec in records:
        node = rec.get("nodeid") or ""
        exs = exchanges_by_node.get(node, [])
        bdir = bundle_dir(run, rec)
        files: dict[str, str] = {}
        if bdir:
            for name in ("assertion.txt", "verdict.txt", "repro.sh", "captured_output.txt"):
                text = read_text(bdir / name, body_cap)
                if text:
                    files[name] = text
            transcript = read_text(bdir / "transcript.sse", body_cap * 2)
            if transcript:
                files["transcript.sse"] = transcript
        shown = (
            exs
            if len(exs) <= max_exchanges
            else exs[: max_exchanges // 2] + exs[-(max_exchanges - max_exchanges // 2) :]
        )
        key_env = key_envs.get(str(rec.get("ingress")))
        exchanges = []
        for e in shown:
            exchanges.append(
                {
                    "seq": e.get("seq"),
                    "method": e.get("method"),
                    "url": e.get("url"),
                    "status": e.get("status"),
                    "elapsed_s": e.get("elapsed_s"),
                    "transport_error": e.get("transport_error"),
                    "request_headers": e.get("request_headers") or {},
                    "request_body": clip(e.get("request_body"), body_cap),
                    "response_headers": e.get("response_headers") or {},
                    "response_body": clip(
                        e.get("response_body")
                        if e.get("response_body") is not None
                        else e.get("response_text"),
                        body_cap,
                    ),
                    "curl": curl_for(e, key_env, body_cap),
                }
            )
        name = node.split("::", 1)[1] if "::" in node else node
        case_id = str(rec.get("case_id") or name or "unknown")
        tests.append(
            {
                "nodeid": node,
                "name": name,
                "case_id": case_id,
                "suite": case_id.split(".", 1)[0] if "." in case_id else "other",
                "variant": rec.get("variant"),
                "surface": rec.get("surface"),
                "ingress": rec.get("ingress"),
                "outcome": rec.get("outcome") or "unknown",
                "skip_class": rec.get("skip_class"),
                "reason": clip(rec.get("reason"), body_cap),
                "duration_s": rec.get("duration_s"),
                "http_status": rec.get("http_status"),
                "served_by": rec.get("served_by"),
                "checks": rec.get("checks") or [],
                "known_failure": rec.get("known_failure"),
                "ts": rec.get("ts"),
                "bundle": str(bdir.relative_to(run)) if bdir else None,
                "files": files,
                "exchanges": exchanges,
                "exchanges_total": len(exs),
                "rerun": "llmdv verify --profile "
                + shlex.quote(str((run / "profile.used.yaml").resolve()))
                + " --evidence all -k "
                + shlex.quote(name),
            }
        )

    counts = collections.Counter(t["outcome"] for t in tests)
    skips = collections.Counter(t["skip_class"] for t in tests if t["outcome"] == "skip")
    by_suite = collections.defaultdict(collections.Counter)
    for t in tests:
        by_suite[t["suite"]][t["outcome"]] += 1
    clusters = collections.Counter()
    examples: dict[str, list[str]] = collections.defaultdict(list)
    known_by_group: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for t in tests:
        if t["outcome"] in ("fail", "error"):
            first = next((c for c in t["checks"] if c.get("ok") is False), None)
            key = (first.get("name") if first else None) or re.sub(
                r"'[^']*'|\d+", "…", (t["reason"] or "").split("\n")[0]
            )[:90]
            clusters[key] += 1
            if len(examples[key]) < 4:
                examples[key].append(t["case_id"] + (f" [{t['variant']}]" if t["variant"] else ""))
            known = t.get("known_failure") or {}
            if known.get("status") == "known":
                known_by_group[key][str(known.get("issue") or "?")] += 1

    started = outcome.get("started") or (env or {}).get("started")
    finished = outcome.get("finished")
    duration = None
    try:
        if started and finished:
            duration = (
                datetime.fromisoformat(finished) - datetime.fromisoformat(started)
            ).total_seconds()
    except ValueError:
        pass
    in_progress = finished is None or env is None

    verification = outcome.get("verification") or {}
    exit_code = verification.get("exit_code")
    publication = dict(outcome.get("publication") or {})
    profile_name = (outcome.get("profile") or {}).get("name") or profile.get("name") or "unknown"
    run_id_for_location = outcome.get("run_id") or (env or {}).get("run_id") or run.name
    if publication.get("destination") and not publication.get("location"):
        # Built before publishing, so the upload has not happened yet. The location is the one
        # deliver() will use: <destination>/<profile>/<run_id>/.
        publication["location"] = (
            f"{str(publication['destination']).rstrip('/')}/{profile_name}/{run_id_for_location}/"
        )
        publication.setdefault("status", "requested; outcome.json records whether it succeeded")
    location = publication.get("location")
    run_id = outcome.get("run_id") or (env or {}).get("run_id") or run.name
    pull = None
    if location and str(location).startswith("s3://"):
        loc = str(location).rstrip("/") + "/"
        pull = (
            f'aws s3 cp --recursive "{loc}" "./runs/{run_id}/"'
            '  # add --endpoint-url "$AWS_ENDPOINT_URL_S3" for an S3-compatible store'
            " such as IBM COS"
        )
    elif location and str(location).startswith("file://"):
        pull = f'cp -R "{str(location)[7:].rstrip("/")}/" "./runs/{run_id}/"'

    logs = []
    if (run / "logs").is_dir():
        for p in sorted((run / "logs").rglob("*")):
            if p.is_file():
                logs.append({"path": str(p.relative_to(run)), "bytes": p.stat().st_size})

    return {
        "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
        "run_dir": str(run.resolve()),
        "run_id": run_id,
        "in_progress": in_progress,
        "outcome": outcome,
        "env": env,
        "exit": {
            "code": exit_code,
            "class": verification.get("class") or EXIT_MEANING.get(exit_code, (None,))[0],
            "meaning": EXIT_MEANING.get(
                exit_code,
                (None, "Not recorded: the run is in progress or was killed before it finished."),
            )[1],
        },
        "started": started,
        "finished": finished,
        "duration_s": duration,
        "counts": dict(counts),
        "skips": dict(skips),
        "skip_meaning": SKIP_MEANING,
        "by_suite": {k: dict(v) for k, v in sorted(by_suite.items())},
        "clusters": [
            {"key": k, "count": n, "examples": examples[k], "known": dict(known_by_group[k])}
            for k, n in clusters.most_common()
        ],
        "profile_raw": profile_raw,
        "profile": profile,
        "reproduce": reproduce,
        "summary_md": summary_md,
        "publication": publication,
        "pull": pull,
        "publish_cmd": None
        if location
        else f"llmdv publish {shlex.quote(str(run.resolve()))} --remote-output s3://<bucket>/<prefix>",
        "manifest": {
            "present": manifest is not None,
            "files": len((manifest or {}).get("files") or [])
            if isinstance(manifest, dict)
            else None,
        },
        "alerts": alerts,
        "comparison": comparison,
        "logs": logs,
        "stack": stack_signals(exchanges_by_node),
        "slowest": sorted(
            (
                {
                    "case": t["case_id"] + (f" [{t['variant']}]" if t["variant"] else ""),
                    "s": t["duration_s"] or 0,
                }
                for t in tests
            ),
            key=lambda x: -x["s"],
        )[:10],
        "tests": tests,
    }


# ---------------------------------------------------------------------------------------- page

# Credentials llmdv itself reads from the environment, besides the profile's key_env variables.
CREDENTIAL_ENV = (
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_ACCESS_KEY_ID",
    "IBM_COS_API_KEY",
    "LLMDV_NOTIFY_WEBHOOK",
)

# A field whose name says it holds a credential. Matched on the whole name, case-insensitive,
# so `max_tokens` or `prompt_cache_key` do not match. Bare `token` and `secret` are left out:
# logprob entries are {"token": "Hello", ...}, so they would refuse every logprobs run.
CREDENTIAL_NAMES = (
    r"authorization|proxy[-_]authorization|x[-_]api[-_]key|api[-_]?key|access[-_]?token|"
    r"refresh[-_]?token|id[-_]?token|client[-_]?secret|password|cookie|set[-_]cookie"
)
CREDENTIAL_FIELD = re.compile(rf"^(?:{CREDENTIAL_NAMES})$", re.IGNORECASE)
# The same names as a "name": "value" pair in serialised text, which is where bodies live in the
# page data: JSON text inside a string (so its quotes are escaped), possibly cut at the size cap.
CREDENTIAL_PAIR = re.compile(
    r'\\?"(' + CREDENTIAL_NAMES + r')\\?"\s*:\s*\\?"((?:[^"\\]|\\[^"])*)', re.IGNORECASE
)

SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"Bearer\s+(?!\$\{)(?!\*\*\*)[A-Za-z0-9._~+/-]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
]


NOTE_KEYS = {"author", "summary", "groups", "cases", "intents"}


def validate_notes(notes: object) -> dict:
    """Agent-written notes, checked for shape. Raises ValueError naming what is wrong.

    {"author": str, "summary": str, "groups": {first failing check: str},
     "cases": {nodeid | case_id | "case_id [variant]": str},
     "intents": {case_id: what the case sets out to prove}} -- every key optional.
    """
    if not isinstance(notes, dict):
        raise ValueError("notes must be a JSON object")
    unknown = set(notes) - NOTE_KEYS
    if unknown:
        raise ValueError(f"unknown notes keys: {sorted(unknown)}; allowed: {sorted(NOTE_KEYS)}")
    for key in ("author", "summary"):
        if key in notes and not isinstance(notes[key], str):
            raise ValueError(f"notes.{key} must be a string")
    for key in ("groups", "cases", "intents"):
        value = notes.get(key, {})
        if not isinstance(value, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in value.items()
        ):
            raise ValueError(f"notes.{key} must map strings to strings")
    return notes


def _case_keys(test: dict) -> list[str]:
    keys = [test["nodeid"], test["name"], test["case_id"]]
    if test.get("variant"):
        keys.append(f"{test['case_id']} [{test['variant']}]")
    return keys


def apply_notes(data: dict, notes: dict | None) -> list[str]:
    """Attach notes to the page data; return the case keys that matched no test."""
    data["notes"] = None
    if not notes:
        return []
    notes = validate_notes(notes)
    cases = notes.get("cases") or {}
    used = set()
    for test in data["tests"]:
        for key in _case_keys(test):
            if key in cases:
                test["note"] = cases[key]
                used.add(key)
                break
    intents = notes.get("intents") or {}
    for test in data["tests"]:
        if test["case_id"] in intents:
            test["intent"] = intents[test["case_id"]]
    for group in data["clusters"]:
        if group["key"] in (notes.get("groups") or {}):
            group["note"] = notes["groups"][group["key"]]
    data["notes"] = {
        "author": notes.get("author") or "an agent",
        "summary": notes.get("summary"),
    }
    case_ids = {test["case_id"] for test in data["tests"]}
    group_keys = {group["key"] for group in data["clusters"]}
    missed = (
        sorted(set(cases) - used)
        + [f"intent: {k}" for k in sorted(set(intents) - case_ids)]
        + [f"group: {k}" for k in sorted(set(notes.get("groups") or {}) - group_keys)]
    )
    return missed


def unmatched_notes(run_dir: Path, notes: dict) -> list[str]:
    return apply_notes(build(Path(run_dir), 200, 1), notes)


class CredentialFound(Exception):
    """The run data holds a credential or a credential-shaped string; no page was written."""


def credential_env_names(profile: dict) -> set[str]:
    """Every environment variable that holds a credential this run could have used."""
    names = set(filter(None, ingress_auth_env(profile).values())) | set(CREDENTIAL_ENV)
    environment = profile.get("environment") if isinstance(profile.get("environment"), dict) else {}
    metrics = environment.get("metrics") if isinstance(environment.get("metrics"), dict) else {}
    auth = metrics.get("auth") if isinstance(metrics.get("auth"), dict) else {}
    if isinstance(auth.get("key_env"), str):
        names.add(auth["key_env"])
    return names


def named_credentials(data, path: str = "") -> list[str]:
    """Fields named like a credential whose value is not redacted, as paths, values unquoted."""
    found = []
    if isinstance(data, dict):
        for key, value in data.items():
            where = f"{path}.{key}" if path else str(key)
            if (
                CREDENTIAL_FIELD.match(str(key))
                and isinstance(value, str)
                and len(value) >= 8
                and redact.SENTINEL not in value
                and not value.startswith("${")
            ):
                found.append(f"a value in a field named {key!r} at {where}")
            else:
                found.extend(named_credentials(value, where))
    elif isinstance(data, list):
        for index, value in enumerate(data):
            found.extend(named_credentials(value, f"{path}[{index}]"))
    return found


def secret_findings(text: str, profile: dict) -> list[str]:
    """What in the page's uncompressed data looks like a credential, without quoting it."""
    found = []
    values = set(redact.known_secrets())
    for name in credential_env_names(profile):
        if os.environ.get(name):
            values.add(os.environ[name])
    for value in values:
        if value and len(value) >= 8 and value in text:
            found.append(f"a registered credential appears {text.count(value)} time(s)")
    for name, value in CREDENTIAL_PAIR.findall(text):
        if len(value) >= 8 and redact.SENTINEL not in value and not value.startswith("${"):
            found.append(f"a value in a field named {name!r} inside a recorded body")
    for pat in SECRET_PATTERNS:
        for match in set(pat.findall(text)):
            found.append(f"a credential-shaped string starting {match[:6]!r}")
    return found


def render(data: dict) -> str:
    raw = json.dumps(data, ensure_ascii=False, default=str)
    packed = base64.b64encode(gzip.compress(raw.encode("utf-8"), 9)).decode("ascii")
    title = f"llmdv run {data['run_id']}"
    template = TEMPLATE.read_text(encoding="utf-8")
    return template.replace("__TITLE__", html.escape(title)).replace("__DATA__", packed)


def is_run_dir(run_dir: Path) -> bool:
    return (run_dir / "results.jsonl").exists() or (run_dir / "outcome.json").exists()


def write(
    run_dir: Path,
    output: Path | None = None,
    *,
    notes: dict | None = None,
    body_cap: int = 6000,
    max_exchanges: int = 6,
) -> Path:
    """Build and write the dashboard; return its path. Raises CredentialFound, writing nothing.

    `notes` are agent-written (see validate_notes) and are shown labelled as such, apart from
    what llmdv recorded. Without them the page is fully static.
    """
    data = build(Path(run_dir), body_cap, max_exchanges)
    apply_notes(data, notes)
    raw = json.dumps(data, ensure_ascii=False, default=str)
    findings = secret_findings(raw, data["profile"]) + named_credentials(data)
    if findings:
        raise CredentialFound("; ".join(findings[:10]))
    out = Path(output) if output else Path(run_dir) / FILENAME
    out.write_text(render(data), encoding="utf-8")
    log.debug("dashboard: %d tests -> %s", len(data["tests"]), out)
    return out


def summary(run_dir: Path) -> dict:
    """Counts and the top failure groups, for a command to report without a browser."""
    data = build(Path(run_dir), 200, 1)
    return {
        "counts": data["counts"],
        "clusters": data["clusters"],
        "in_progress": data["in_progress"],
        "case_ids": sorted({test["case_id"] for test in data["tests"]}),
    }


def write_best_effort(run_dir: Path) -> Path | None:
    """Write the dashboard if possible. Never raises: it must not change how a run ends."""
    try:
        return write(run_dir)
    except CredentialFound as exc:
        log.warning("dashboard NOT written: possible credential in the run data (%s)", exc)
    except Exception as exc:  # noqa: BLE001 -- a reading aid must never fail a run
        log.warning("dashboard NOT written: %s", redact.scrub(f"{type(exc).__name__}: {exc}"))
    return None
