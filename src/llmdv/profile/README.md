# profile

`schema.py` holds the profile dataclasses and the `Disposition` and `SkipClass` vocabularies. `loader.py` turns YAML into them: loading stops at the first type or shape error, and `validate` then collects every other problem, each with its path. `errors.py` holds `ProfileError` and `Diagnostic`, the only two ways the package reports a profile wrong. `transport.py`, `fleet.py` and `evolution.py` parse per-ingress HTTP behaviour, fleet invariants and the evolution declarations. Fields are documented in [profiles/REFERENCE.md](../../../profiles/REFERENCE.md).
