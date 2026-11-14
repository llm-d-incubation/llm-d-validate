# tests

llmdv's own offline tests. They need no cluster or gateway: every case runs against fakes on
local sockets.

| File | What it is |
| --- | --- |
| `fakes.py`, `fake_openai.py`, `fake_anthropic.py`, `fleet_fakes.py` | real HTTP servers on local sockets, with flags for the defects the suites exist to catch |
| `conftest.py` | shared fixtures |
| `test_*.py` | one file per area of the library or suite under test |
| [`fixtures/`](fixtures/) | recorded inputs |

```sh
.venv/bin/python -m pytest tests/ -q           # one test at a time
.venv/bin/python -m pytest tests/ -q -n auto   # in parallel; needs ./install.sh --dev
```

A new case needs its defect added to a fake, and a test proving the case fails against it.
