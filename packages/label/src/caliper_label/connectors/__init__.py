"""Data connectors (CONTRACT): read rows from files, folders, object stores and databases.

    from caliper_label.connectors import open_connector
    conn = open_connector(project.source, project_dir)
    conn.schema(); conn.preview(50); conn.count()
    for batch in conn.iter_rows(1000): ...

See ``docs/label-connectors.md`` (including "write your own connector").
"""

from .base import BaseConnector, Column, Connector, ConnectorError, MediaRef, jsonable
from .registry import (
    ENTRY_POINT_GROUP,
    available_connectors,
    open_connector,
    register,
    unregister,
)

__all__ = [
    "ENTRY_POINT_GROUP",
    "BaseConnector",
    "Column",
    "Connector",
    "ConnectorError",
    "MediaRef",
    "available_connectors",
    "jsonable",
    "open_connector",
    "register",
    "unregister",
]
