# output

Where a run's artifacts go. `local.py` writes the run directory; `remote.py` publishes to `file://` or `s3://` (`s3.py`, or `ibm_cos.py` under `LLMDV_S3_AUTH=ibm-iam`); `manifest.py` writes `MANIFEST.json` last; `offload.py` stores evidence too large for the run directory, on request; `base.py` defines the sink interface.
