# http

`client.py` is the recording client: raw httpx, plus `SdkRecorder` for SDK cases. `sse.py` reads streams, keeping partial transcripts. `redact.py` is the single source of scrubbing; `repro.py` turns an exchange into curl.
