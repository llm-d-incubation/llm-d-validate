# assertions

Shared assertions, so a lesson is encoded once and every surface inherits it. `common.py` holds finish-reason, output and usage checks; `errors.py` status and envelope checks; surface-specific modules check native lifecycles. Each records what it observed before it asserts. `samples.py` is the one statistical check: a declared rule (`count`, `ratio` or `order`) over exactly n samples, all recorded.
