# What the person you are working for has

The skills assume nothing about the machine they run on. Before a skill's first live step, find
out what this person has; the skill says what to skip without it. Check, then ask only about what
is missing. Never ask for a secret in the conversation: the person loads it into the shell
themselves (in Claude Code, a command prefixed with `!` runs in the session).

## Check

```sh
for t in curl jq python3 gh aws kubectl oc; do command -v $t >/dev/null && echo "have $t" || echo "no $t"; done
test -x .venv/bin/llmdv && echo "llmdv installed" || echo "run ./install.sh"
gh auth status >/dev/null 2>&1 && echo "gh logged in"
kubectl config current-context 2>/dev/null || oc whoami --show-context 2>/dev/null
for v in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_ENDPOINT_URL_S3 AWS_BUCKET; do [ -n "${!v}" ] && echo "$v set"; done
```

`${!v}` is bash; in zsh use `${(P)v}`. Never `echo` a credential's value: test it with `[ -n "$VAR" ]`.

## What each piece unlocks

| Have | Unlocks | Without it |
|---|---|---|
| `.venv` (`./install.sh`; `--dev` for tests, `--s3` for the bucket) | `llmdv` and the repo's Python with PyYAML, used by the snippets | install it first; nothing live works |
| `curl`, `jq` | hand checks and probes | install them; `python3 -m json.tool` stands in for pretty-printing only |
| A gateway URL and key, or a router route | live runs at that layer | ask the person; a router (EPP) route needs no key |
| `gh`, logged in | issue and PR search, filing | the person searches or files from the browser; you draft |
| Bucket credentials (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_ENDPOINT_URL_S3`, `AWS_DEFAULT_REGION`, `AWS_BUCKET`, exported) and the `aws` CLI | published runs, `llmdv compare --from s3://…`, publishing | compare local run directories; ask someone with access to pull a run |
| `kubectl` or `oc`, logged into the cluster that serves the model | Routes, pod logs, deployment arguments, port-forwards | ask the deployment's owner |

`kubectl` and `oc` are interchangeable for the read-only `get` and `logs` commands the skills use
(`kubectl get route` works on OpenShift). Use whichever the person has, against the context they
name; never switch their context without asking.

## Credentials and endpoints

Where each person keeps gateway URLs, keys and bucket credentials differs (an env file, a shell
profile, a secret manager). Ask where, and have them load it. Then map their names onto the ones a
profile reads (each ingress's `base_url_env` and `auth.key_env`); [testing-a-model-deployment](skills/testing-a-model-deployment/SKILL.md)
shows how to print those.

```sh
set -a; source <their file>; set +a      # set -a exports variables a file sets without `export`
export <profile's url variable>="$<their url variable>" <profile's key variable>="$<their key variable>"
```

## Long runs

A full run takes 15 to 60 minutes; the machine must stay awake (`caffeinate -i <command>` on
macOS, `systemd-inhibit <command>` on Linux). Where that is impractical, run it unattended with `llmdv job` or
as a cluster job ([running-and-comparing-live-runs](skills/running-and-comparing-live-runs/SKILL.md)).
