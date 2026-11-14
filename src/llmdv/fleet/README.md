# fleet

Fleet invariants read from configuration, metrics and logs: `config.py` reads objects with `kubectl get`, `prometheus.py` is the read-only metrics client. The rules are declared in a profile's `fleet` block ([profiles/FLEET.md](../../../profiles/FLEET.md)).
