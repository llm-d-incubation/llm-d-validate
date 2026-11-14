# log

The logger tree. `setup.py` configures two streams: diagnostics to stderr under `--log-level`, and primary output to stdout, never suppressed. `filters.py` redacts credentials from every record; `writer.py` routes pytest's own output into the tree.
