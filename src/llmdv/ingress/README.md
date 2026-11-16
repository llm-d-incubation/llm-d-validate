# ingress

`base.py` defines `ServedBy` (which endpoint answered), one `IngressAdapter` class, and the `ADAPTERS` table: one row per `IngressKind` with the headers that name that layer's backend and build. Adding a kind is one row.
