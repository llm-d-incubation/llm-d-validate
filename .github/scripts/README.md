# Scripts

Helpers for workflows that run `llmdv` against a live gateway.

| Script | What it does |
| --- | --- |
| [live_summary.py](live_summary.py) | writes the GitHub job summary for a live run as Markdown: the verdict, the totals, a table per suite, the largest failure groups and an optional comparison. It reads only the redacted run directory, runs with no credentials in its environment, and refuses to print anything shaped like a credential |

```sh
python .github/scripts/live_summary.py runs/<run> --profile <name> --gateway <name> \
  --channel <folder> --event "$GITHUB_EVENT_NAME" --code "$EXIT_CODE" >> "$GITHUB_STEP_SUMMARY"
```
