# ingress

`base.py` defines `ServedBy` (which endpoint answered), one `IngressAdapter` class, and the `ADAPTERS` table: one row per position (`IngressKind`: gateway, router, engine) with the headers that name the backend and build of the product measured there so far. A new product needs no code: it reads its position's row and records the headers it sends.
