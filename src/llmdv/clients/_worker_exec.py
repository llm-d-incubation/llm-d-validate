"""Register a trusted client's process group before it can start any work."""

import os
import sys
from pathlib import Path

if __name__ == "__main__":
    directory, worker, *argv = sys.argv[1:]
    marker = Path(directory) / f"client-{worker}-{os.getpid()}"
    marker.touch()
    # The worker can die inside Popen, before it even receives our pid. If
    # cleanup already ran, no client may start after the coordinator's barrier.
    if os.getppid() != int(worker) or (marker.parent / f"stopping-{worker}").exists():
        marker.unlink(missing_ok=True)
        sys.exit(1)
    os.execvp(argv[0], argv)  # noqa: S606 -- trusted, operator-pinned client
