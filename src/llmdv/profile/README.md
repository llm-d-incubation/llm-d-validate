# profile

`schema.py` holds the profile dataclasses and the `Disposition` and `SkipClass` vocabularies. `loader.py` turns YAML into them and reports every problem with its path. `transport.py`, `fleet.py` and `evolution.py` parse per-ingress HTTP behaviour, fleet invariants and the evolution declarations. Fields are documented in [profiles/REFERENCE.md](../../../profiles/REFERENCE.md).
