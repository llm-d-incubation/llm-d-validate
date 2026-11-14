# collect

Collects each layer's own logs for the run window through the read-only cluster adapter: `kubectl.py` reads them, `noop.py` is used when no cluster access is declared, `base.py` defines the interface.
