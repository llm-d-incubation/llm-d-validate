"""Reading results.jsonl back, and refusing what this llmdv cannot read.

Archives keep old runs, so a reader meets records from older and newer llmdv releases side by
side. Every reader accepts the majors in `READABLE` and refuses any other, naming the file, the
line and the schema, rather than guessing at a record whose fields may have changed meaning.
A line that parses as JSON but is not a result -- a bare string, a record without an `outcome`
-- is refused the same way, since counting it would mean guessing too.

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
OUTCOMES = ("pass", "fail", "skip", "error")


class Unreadable(ValueError):
    """A line of a results file that this llmdv refuses to read as a result."""


class UnreadableSchema(Unreadable):
    """A record written under a results schema this llmdv cannot read."""


class NotAResult(Unreadable):
    """A line that is valid JSON but not a result record."""


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


def check_result(record: Any, where: str) -> None:
    """Refuse a line that is not a result record. `where` names the file and line."""
    if not isinstance(record, dict) or record.get("outcome") not in OUTCOMES:
        raise NotAResult(
            f"{where}: not a result record (expected an object with outcome "
            f"{', '.join(OUTCOMES[:-1])} or {OUTCOMES[-1]})"
        )


def records(
    path: Path, lines: Iterable[str], refused: list[str] | None = None
) -> Iterator[dict[str, Any]]:
    """Each result a results file holds. A line that is not JSON (the torn last line of a
    killed run) is passed over. A line that is not a result, or is written under a schema this
    llmdv cannot read, raises `Unreadable` -- or, when `refused` is given, is described there
    and left out, for a caller that must deliver what a crashed run left."""
    for number, line in enumerate(lines, start=1):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        where = f"{path}, line {number}"
        try:
            check_result(record, where)
            check_schema(record, where)
        except Unreadable as exc:
            if refused is None:
                raise
            refused.append(str(exc))
            continue
        yield record


def read(path: Path, refused: list[str] | None = None) -> list[dict[str, Any]]:
    """The records a local results file holds, or none if it is missing or unreadable.
    `refused` is as for `records`."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return list(records(path, text.splitlines(), refused))
