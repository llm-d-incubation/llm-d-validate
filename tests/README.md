# tests

llmdv's own offline tests. They need no cluster or gateway: every case runs against fakes on
local sockets.

| File | What it is |
| --- | --- |
| `fakes.py`, `fake_openai.py`, `fake_anthropic.py`, `fleet_fakes.py` | real HTTP servers on local sockets, with flags for the defects the suites exist to catch |
| `conftest.py` | shared fixtures |
| `test_*.py` | one file per area of the library or suite under test |
| [`fixtures/`](fixtures/) | recorded inputs |

The commands to run them are in the
[manual](../docs/manual.md#running-llmdvs-own-tests).

A new case needs its defect added to a fake, and a test proving the case fails against it.
