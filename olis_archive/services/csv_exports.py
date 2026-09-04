"""Streaming CSV helpers for large LegiView audit exports."""

from __future__ import annotations

import csv
from io import StringIO
from typing import Any, Iterator, Sequence

from ..database import Database


DEFAULT_BATCH_SIZE = 500


def stream_query_csv(
    database: Database,
    sql: str,
    params: Sequence[Any] = (),
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> Iterator[str]:
    """Yield a SQL result as CSV while retaining at most one DB page in memory.

    The UTF-8 BOM helps spreadsheet applications recognize Unicode.  Values
    that could be interpreted as formulas are prefixed with an apostrophe;
    source metadata is untrusted even though the export itself is local.
    """

    size = max(1, min(int(batch_size), 10_000))
    with database.connection() as connection:
        cursor = connection.execute(sql, tuple(params))
        columns = tuple(description[0] for description in cursor.description or ())
        yield "\ufeff" + _csv_row(columns)
        while True:
            rows = cursor.fetchmany(size)
            if not rows:
                break
            for row in rows:
                yield _csv_row(tuple(_safe_csv_value(row[column]) for column in columns))


def _csv_row(values: Sequence[Any]) -> str:
    buffer = StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(values)
    return buffer.getvalue()


def _safe_csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


__all__ = ["DEFAULT_BATCH_SIZE", "stream_query_csv"]
