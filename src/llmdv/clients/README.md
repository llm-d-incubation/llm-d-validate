# clients

`process.py` bounds a subprocess's output and lifetime; `claude_code.py` owns the version-specific flags. `relay.py` forwards the child's native requests through the recording client, which owns credentials and blocks hidden retries.
