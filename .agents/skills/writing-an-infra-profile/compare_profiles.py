"""Print every effective setting that differs between two profiles at one ingress.

    python compare_profiles.py <profile A> <profile B> <ingress id>

Both profiles are loaded as llmdv loads them, with `extends` composed and per-ingress overrides
applied, so a public profile plus its local overlay can be compared with the private profile it
came from. Addresses, credentials, setup and the change sentinels (pins and baselines) are left
out: they are expected to differ. Run it from the repository root with llmdv importable
(`PYTHONPATH=src`), and with the variables the profiles name set to any value.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import sys

from llmdv.profile import loader

LEFT_OUT = {"id", "base_url", "base_url_env", "auth", "setup", "logs"}
# Values only a run of one exact stack supplies; a published profile carries placeholders.
SENTINELS = {"prompt_pins", "render_pins", "contract_baseline"}


def plain(value):
    """A dataclass tree as JSON-comparable data, with unordered collections sorted."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, dict):
        return {str(plain(k)): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return sorted((plain(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
    return value if isinstance(value, (str, int, float, bool, type(None))) else str(value)


def flat(value, prefix=""):
    if isinstance(value, dict):
        for key, item in value.items():
            if (not prefix and key in LEFT_OUT) or key in SENTINELS:
                continue
            yield from flat(item, f"{prefix}.{key}" if prefix else key)
    else:
        yield prefix, value


def settings(path: str, ingress_id: str) -> dict:
    profile = loader.load(path)
    ingress = next((i for i in profile.ingresses if i.id == ingress_id), None)
    if ingress is None:
        sys.exit(f"{path}: no ingress {ingress_id!r}")
    found = dict(flat(plain(ingress)))
    for header, by_ingress in plain(profile.headers).items():
        if isinstance(by_ingress, dict) and by_ingress.get(ingress_id) is not None:
            found[f"headers.{header}"] = by_ingress[ingress_id]
    found["fleet_policy"] = plain(profile.fleet_policy)
    return found


def main() -> None:
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    a, b = settings(sys.argv[1], sys.argv[3]), settings(sys.argv[2], sys.argv[3])
    differing = [key for key in sorted(a.keys() | b.keys()) if a.get(key) != b.get(key)]
    sys.stdout.write(f"{len(a.keys() | b.keys())} settings compared, {len(differing)} differ\n")
    for key in differing:
        left = json.dumps(a.get(key, "<absent>"), sort_keys=True)[:160]
        right = json.dumps(b.get(key, "<absent>"), sort_keys=True)[:160]
        sys.stdout.write(f"  {key}\n      A: {left}\n      B: {right}\n")


if __name__ == "__main__":
    main()
