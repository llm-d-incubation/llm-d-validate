"""Reading results.jsonl back, and refusing what this llmdv cannot read.

Archives keep old runs, so a reader meets records from older and newer llmdv releases side by
side. Every reader accepts the majors in `READABLE` and refuses any other, naming the file, the
line and the schema, rather than guessing at a record whose fields may have changed meaning.

`llmdv.results/1` grows only by optional fields; a reader never requires one:

- a check's `observed` is any JSON value: text, a number, a list or a mapping, as several
  checks already record;
- a check may carry `rule` (the declared rule it was judged by) and `samples` (every sample
  behind it);
- a record may carry `claims`, the ids of the claims it serves.

A new major is for a field that changes meaning or becomes required, and none has.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

NAME = "llmdv.results"
READABLE = (1,)


class UnreadableSchema(ValueError):
    """A record written under a results schema this llmdv cannot read."""


def major(schema: object) -> int | None:
    """The major version of a results schema name, or None if it is not one."""
    if not isinstance(schema, str):
        return None
    name, _, version = schema.partition("/")
    number = version.split(".", 1)[0]
    if name != NAME or not number.isdigit():
        return None
    return int(number)


def check_schema(record: Any, where: str) -> None:
    """Refuse a record this llmdv cannot read. `where` names the file and line.

    A record without `schema` reads as `/1`: no released llmdv wrote one, and hand-made records
    (tests, notes) would otherwise all be refused.
    """
    if not isinstance(record, dict) or "schema" not in record:
        return
    schema = record["schema"]
    if major(schema) not in READABLE:
        readable = " or ".join(f"{NAME}/{m}" for m in READABLE)
        raise UnreadableSchema(
            f"{where}: schema {schema!r} is not one this llmdv reads ({readable}); "
            "read it with the llmdv release that wrote it, or a newer one"
        )


def records(path: Path, lines: Iterable[str]) -> Iterator[Any]:
    """Each parsed line of a results file, schema-checked; a line that is not JSON (the torn
    last line of a killed run) is passed over."""
    for number, line in enumerate(lines, start=1):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        check_schema(record, f"{path}, line {number}")
        yield record


def read(path: Path) -> list[Any]:
    """The records a local results file holds, or none if it is missing or unreadable."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return list(records(path, text.splitlines()))
