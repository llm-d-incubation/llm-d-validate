# Fleet rules

Some failures show in a fleet's configuration, metrics or logs rather than on the wire: a setting that turns on full error stacks, two active routers, a router and engine disagreeing on block size. A profile that can read the cluster declares these as rules, each naming what it reads. Without rules, the `fleet` suite's cases skip as `undeclared`. Other fields are in [REFERENCE.md](REFERENCE.md).

Each rule has a unique `id` and a `check`, and becomes one variant of that check's case. On any rule, `when` names a capability that some ingress must declare for the rule to run, and `ingress` names the column its result appears in.

## config

Read with `kubectl get -o json`, in `environment.namespace` unless the rule names another. Finding no object fails; a forbidden or unreachable read skips as `requires_instrumentation`. A rule that names Secrets is refused.

| Key | Applies to | Meaning |
| --- | --- | --- |
| `check` | all | `settings`, `values_agree`, `field_values`, `ready_endpoints` or `selector_exclusive`. |
| `objects` | `settings`, `field_values`, `selector_exclusive` | `{kind, name}` or `{kind, selector}`, optional `namespace`. One resource kind. |
| `view` | `settings`, sources | `containers` (default): each container's command line and `NAME=value` environment; Secret values are never read. `data`: a ConfigMap's data. |
| `containers` | `settings`, sources | Container names to read. |
| `require`, `forbid` | `settings` | Multiline regular expressions: each unit matches every `require` and no `forbid`. |
| `sources` | `values_agree` | Each: `objects`, a `pattern` with one capture group, optional `view`, `containers`, `default`. The captured values must agree. |
| `expect`, `expect_capability` | `values_agree` | A literal, or a capability path such as `cache_reuse.block_size`, they must also equal. |
| `path` | `field_values` | Key list into each object, such as `[metadata, annotations, <name>]`. |
| `equals`, `at_least_seconds` | `field_values` | Exactly one. The field may hold seconds or a Go-style duration such as `1h30m`. |
| `service`, `count`, `namespace` | `ready_endpoints` | The Service must have exactly `count` ready endpoints. |
| `selector_path` | `selector_exclusive` | Path to the named object's selector; no other object of its kind may select its pods. |

```yaml
fleet:
  config:
    - id: dev_mode_off
      check: settings
      objects: {kind: deployments, selector: app=engine}
      containers: [server]
      forbid: ['^DEV_MODE=(1|true)$']
    - id: block_sizes_agree
      check: values_agree
      expect_capability: cache_reuse.block_size
      sources:
        - {objects: {kind: deployments, selector: app=engine}, pattern: '--block-size[ =](\d+)', default: "16"}
        - {objects: {kind: configmaps, name: router-config}, view: data, pattern: 'blockSize:\s*(\d+)'}
    - id: one_active_router
      check: ready_endpoints
      service: router
      count: 1
```

The kubeconfig needs `get` and `list` on every kind read, plus `pods` for `selector_exclusive` and `endpointslices` or `endpoints` for `ready_endpoints`.

## metrics

The source is `environment.metrics`, a Prometheus-compatible API read only with `GET /api/v1/query`.

| Key | Default | Meaning |
| --- | --- | --- |
| `url` | **required** | Base URL, such as a port-forward to the metrics API. |
| `auth` | `{type: none}` | `{type: bearer, key_env: NAME}` sends the token from `$NAME`, redacted everywhere. |
| `proxy` | `bypass` | `allow` lets the HTTP proxy variables apply. |
| `timeout` | `15` | Seconds per query. |
| `tls_verify` | `true` | Verify the source's certificate. |

Preflight sends one query; an unanswered source or unset token is exit 3. During the run, an unreachable source or an answer with warnings skips as `requires_instrumentation`. A rejected query fails, and so does one returning no series unless `absent_is_zero: true`.

| `check` | Keys | Holds when |
| --- | --- | --- |
| `bounds` | `query`, `min` and/or `max`, `absent_is_zero` | every series is within the bounds |
| `agree` | `left`, `right`, `absolute_tolerance` or `relative_tolerance`, `relation` (`equal`, `at_least`) | each side returns one series, and they agree within tolerance, or left is at least right |
| `spread` | `query`, optional `label`, `min_ratio`, `min_series` | the smallest value is at least `min_ratio` of the largest |
| `header_label` | `ingress`, `headers`, `query`, `label`, `expect`, `timeout_s` (120), `interval_s` (5) | a request through `ingress` with `headers` is counted under `label` = `expect` |

## logs

A log source under `ingresses[].logs` with `forbid` entries gets one `logs.signatures` result. It runs last and scans the whole run window: every Running pod, and the previous instance of any container that restarted. `tail` does not cap the scan. Matching no pod or a failed read fails; a forbidden read skips as `requires_instrumentation`. A hit names the llmdv case whose request or response id is on the line.

```yaml
ingresses:
  - id: engine
    logs:
      - selector: app=engine
        container: server
        forbid:
          - {id: fatal, pattern: 'fatal error'}
          - {id: validation_stack, pattern: 'Traceback \(most recent call last\)'}
        allow: ['client disconnected']
```
