"""Keep verification evidence available for the CI runner to collect."""

import json
import os
import signal
import subprocess
import time
from pathlib import Path


def main(work=Path("/work")):
    child = subprocess.Popen(json.loads(os.environ["LLMDV_COMMAND"]))
    (work / "verify.pid").write_text(str(child.pid))

    def interrupt(_signum, _frame):
        if child.poll() is None:
            child.send_signal(signal.SIGTERM)
        else:
            raise SystemExit(0)

    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    code = child.wait()
    pending = work / "exit.tmp"
    pending.write_text(str(code))
    pending.replace(work / "exit")
    # The orchestration deletes the pod after collection; the pod deadline is the backstop.
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
