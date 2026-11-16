# results

`record.py` writes the versioned `results.jsonl` records (schema `llmdv.results/1`), one per case with the checks behind its verdict.

`read.py` is how everything else reads them back. It accepts `llmdv.results/1` and refuses any other schema, naming the file and the line. `/1` grows only by optional fields: a check's `observed` is any JSON value, a check may carry `rule` and `samples`, and a record may carry `claims`. No reader requires them.
