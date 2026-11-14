# cli

One module per subcommand: `verify_cmd.py` (embeds pytest and owns the run directory), `job_cmd.py`, `publish_cmd.py`, `notify_cmd.py`, `compare_cmd.py`, `analyze_cmd.py` and `profile_cmd.py`. `main.py` holds the shared flags; `banner.py`, `contract_display.py` and `style.py` render output; `exit_codes.py` holds the [exit codes](../../../docs/manual.md#exit-codes).
