# ingress

`base.py` defines the ingress adapter, `ServedBy` (which endpoint answered) and the headers that name a layer's build. `vllm.py`, `router.py` and `litellm.py` override only what differs. A registry maps each `IngressKind` to its adapter.
