"""Connector registry: built-ins by ``SourceKind`` plus plugins from entry points.

A plugin package declares, in its ``pyproject.toml``::

    [project.entry-points."caliper_label.connectors"]
    warehouse = "my_pkg.connector:WarehouseConnector"

and a project selects it with ``source: {kind: plugin, plugin: warehouse, options: {...}}``.
The entry point may be a class or any callable taking ``(source, project_dir)`` and returning
an object with the Connector methods (subclass ``BaseConnector`` to get the defaults).
"""

from __future__ import annotations

from collections.abc import Callable
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from ..project import SourceConfig, SourceKind
from .base import Connector, ConnectorError

ENTRY_POINT_GROUP = "caliper_label.connectors"

Factory = Callable[[SourceConfig, Path], Any]

_registered: dict[str, Factory] = {}


def register(name: str, factory: Factory) -> None:
    """Register a connector in-process (tests, notebooks). Entry points are the packaged way."""
    _registered[name] = factory


def unregister(name: str) -> None:
    _registered.pop(name, None)


def _entry_points() -> dict[str, Any]:
    return {ep.name: ep for ep in entry_points(group=ENTRY_POINT_GROUP)}


def available_connectors() -> dict[str, str]:
    """name -> "builtin" | "registered" | "plugin:<dist>" (for the wizard's source picker)."""
    out = {k.value: "builtin" for k in SourceKind if k is not SourceKind.PLUGIN}
    out.update(dict.fromkeys(_registered, "registered"))
    for name, ep in _entry_points().items():
        dist = getattr(getattr(ep, "dist", None), "name", None) or "?"
        out.setdefault(name, f"plugin:{dist}")
    return out


def _plugin_factory(name: str) -> Factory:
    if name in _registered:
        return _registered[name]
    ep = _entry_points().get(name)
    if ep is None:
        known = sorted(set(_registered) | set(_entry_points()))
        raise ConnectorError(
            f"no connector plugin named '{name}'",
            "install the package that provides it"
            + (f" (installed plugins: {', '.join(known)})" if known else ""),
        )
    try:
        return ep.load()
    except Exception as exc:
        raise ConnectorError(
            f"connector plugin '{name}' failed to load: {type(exc).__name__}: {exc}",
            f"check that {ep.value} imports cleanly",
        ) from None


def open_connector(source: SourceConfig, project_dir: Path) -> Connector:
    """Build the connector for ``source``. Paths in ``source`` resolve from ``project_dir``."""
    project_dir = Path(project_dir)
    if source.kind is SourceKind.PLUGIN:
        if not source.plugin:
            raise ConnectorError("source.plugin is empty", "set the plugin's entry-point name")
        factory = _plugin_factory(source.plugin)
    else:
        from .duck import BUILTINS

        factory = _registered.get(source.kind.value) or BUILTINS[source.kind]
    conn = factory(source, project_dir)
    missing = [
        m
        for m in ("schema", "preview", "count", "iter_rows", "resolve_media")
        if not callable(getattr(conn, m, None))
    ]
    if missing:
        raise ConnectorError(
            f"connector for '{source.plugin or source.kind.value}' lacks {', '.join(missing)}",
            "subclass caliper_label.connectors.BaseConnector",
        )
    return conn
