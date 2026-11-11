"""The connector interface (CONTRACT): every data source looks like this to the rest of the app.

A connector reads rows from somewhere and never writes. It must:

* stream: ``iter_rows`` yields batches and never materialises the whole source;
* keep secrets out of everything it returns or raises (see ``credentials.redact``);
* raise :class:`ConnectorError` with a plain-words ``hint`` when something is wrong.

:class:`BaseConnector` fills in everything except ``iter_rows`` so a custom
connector can be a few lines (see ``docs/label-connectors.md``).
"""

from __future__ import annotations

import base64
import datetime as _dt
import decimal
import itertools
import math
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel

from ..project import DisplayType, SourceConfig


class ConnectorError(Exception):
    """A data-source problem the user can fix. ``hint`` says how, in plain words."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def to_dict(self) -> dict[str, str | None]:
        return {"detail": self.message, "hint": self.hint}


class Column(BaseModel):
    name: str
    dtype: str  # engine type, e.g. "VARCHAR", "BIGINT", "STRUCT(...)"; "unknown" for plugins
    display: DisplayType  # auto-detected display type


class MediaRef(BaseModel):
    kind: Literal["file", "url"]
    path: str | None = None  # kind=file: absolute local path, already checked against roots
    url: str | None = None  # kind=url: http(s) or a short-lived presigned URL
    mime: str | None = None


@runtime_checkable
class Connector(Protocol):
    def schema(self) -> list[Column]: ...

    def preview(self, n: int = 50) -> list[dict[str, Any]]: ...

    def count(self) -> int: ...

    def iter_rows(self, batch_size: int = 1000) -> Iterator[list[dict[str, Any]]]: ...

    def resolve_media(self, ref: str) -> MediaRef: ...


def jsonable(v: Any) -> Any:
    """Coerce a cell to a JSON-safe value, deterministically (row hashes depend on it)."""
    if v is None or isinstance(v, bool | int | str):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, decimal.Decimal):
        return str(v) if not v.is_finite() else (int(v) if v == v.to_integral_value() else float(v))
    if isinstance(v, _dt.datetime | _dt.date | _dt.time):
        return v.isoformat()
    if isinstance(v, _dt.timedelta):
        return v.total_seconds()
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, bytes | bytearray | memoryview):
        return "base64:" + base64.b64encode(bytes(v)).decode("ascii")
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    if isinstance(v, list | tuple | set):
        return [jsonable(x) for x in v]
    return str(v)


class BaseConnector:
    """Defaults for everything but ``iter_rows``. Subclass and override what you can do faster.

    ``source`` and ``project_dir`` are set by the registry; a hand-built connector
    may leave them as ``None``.
    """

    def __init__(self, source: SourceConfig | None = None, project_dir: Path | None = None):
        self.source = source
        self.project_dir = Path(project_dir) if project_dir else None

    def iter_rows(self, batch_size: int = 1000) -> Iterator[list[dict[str, Any]]]:
        raise NotImplementedError

    def preview(self, n: int = 50) -> list[dict[str, Any]]:
        rows = (r for batch in self.iter_rows(batch_size=max(n, 1)) for r in batch)
        return [jsonable(r) for r in itertools.islice(rows, n)]

    def count(self) -> int:
        return sum(len(b) for b in self.iter_rows())

    def schema(self) -> list[Column]:
        from ..typedetect import detect_columns

        rows = self.preview(200)
        names: dict[str, None] = {}
        for r in rows:
            names.update(dict.fromkeys(r))
        return detect_columns([(c, "unknown") for c in names], rows)

    def resolve_media(self, ref: str) -> MediaRef:
        from .media import resolve_ref

        return resolve_ref(ref, self.media_roots(), source=self.source)

    def media_roots(self) -> list[Path]:
        """Directories local media may be served from (never anything outside them)."""
        from .media import default_roots

        return default_roots(self.source, self.project_dir)

    def close(self) -> None:
        pass
