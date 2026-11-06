"""Credential *references* and how they resolve. Values are never stored, logged or returned.

A ``SourceConfig.credentials`` block only names where a secret lives:

* ``env``: one env var holding a whole DSN (databases, e.g. ``DATABASE_URL``) or a bearer token
  (HTTP sources);
* ``env_map``: option -> env var, e.g. ``{"password": "APP_DB_PASSWORD"}``;
* ``aws_profile`` / ``region`` / ``endpoint_url``: AWS (and S3-compatible) settings.

Without those, the standard chains apply: the AWS chain (env vars, ``AWS_PROFILE``, SSO, instance
role), libpq's ``PG*`` vars or ``DATABASE_URL``, MySQL's ``MYSQL_*`` vars, GCP ADC.

``credential_status`` reports which variable or chain is used and whether it resolved, never a
value. Anything that turns an exception into text for a user goes through ``redact``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import quote, unquote, urlsplit

from pydantic import BaseModel

from .connectors.base import ConnectorError
from .project import SourceConfig, SourceKind


class CredentialCheck(BaseModel):
    name: str  # what is used: an env var name, "AWS profile 'dev'", "GCP ADC", ...
    kind: Literal["env", "aws", "gcp", "file", "none"]
    resolved: bool
    hint: str | None = None


# Env vars whose values are always scrubbed from error text, whatever the source.
_SECRET_ENV = re.compile(
    r"(PASSWORD|PASSWD|PWD|SECRET|TOKEN|KEY|DATABASE_URL|DSN|CREDENTIAL)", re.IGNORECASE
)
_URL_PASSWORD = re.compile(r"(?P<pre>[a-z][a-z0-9+.-]*://[^:/@\s]*:)(?P<pw>[^@\s]+)(?P<post>@)")
_KV_PASSWORD = re.compile(r"(?P<pre>\b(password|pwd|secret)\s*[=:]\s*)(?P<pw>'[^']*'|\S+)", re.I)


def _env(name: str | None) -> str | None:
    if not name:
        return None
    v = os.environ.get(name)
    return v if v else None


def _secret_values(source: SourceConfig | None) -> list[str]:
    vals: set[str] = set()
    for k, v in os.environ.items():
        if v and len(v) >= 4 and _SECRET_ENV.search(k):
            vals.add(v)
    if source is not None:
        for name in [source.credentials.env, *source.credentials.env_map.values()]:
            v = _env(name)
            if v and len(v) >= 4:
                vals.add(v)
    for v in list(vals):  # also the forms a DSN or URL would carry
        vals.add(quote(v, safe=""))
        vals.add(unquote(v))
        if "://" in v:
            pw = urlsplit(v).password
            if pw and len(pw) >= 3:
                vals.update({pw, unquote(pw), quote(unquote(pw), safe="")})
    return sorted((v for v in vals if v), key=len, reverse=True)


def redact(text: str, source: SourceConfig | None = None) -> str:
    """Remove every secret value we know of (and any URL/key=value password) from ``text``."""
    out = str(text)
    for v in _secret_values(source):
        out = out.replace(v, "***")
    out = _URL_PASSWORD.sub(lambda m: m["pre"] + "***" + m["post"], out)
    out = _KV_PASSWORD.sub(lambda m: m["pre"] + "***", out)
    return out


# --------------------------------------------------------------------------- AWS


@dataclass(frozen=True)
class AwsCreds:
    key_id: str
    secret: str
    token: str | None
    region: str | None
    via: str  # non-secret description: "env", "AWS profile 'dev'", "default chain"

    def __repr__(self) -> str:  # never print the values
        return f"AwsCreds(via={self.via!r}, region={self.region!r})"


def _aws_hint(source: SourceConfig) -> str:
    prof = source.credentials.aws_profile
    if prof:
        return f"run `aws sso login --profile {prof}` or check that profile '{prof}' exists"
    if source.credentials.endpoint_url:
        return (
            "set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY for the S3-compatible endpoint "
            "(or map other env vars in credentials.env_map)"
        )
    return "set AWS_PROFILE or run `aws sso login`, or set AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY"


def aws_session(source: SourceConfig):
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - boto3 is a dependency
        raise ConnectorError("boto3 is not installed", "pip install boto3") from exc
    import botocore.exceptions

    cm = source.credentials
    try:
        return boto3.Session(profile_name=cm.aws_profile or None, region_name=cm.region or None)
    except botocore.exceptions.ProfileNotFound as exc:
        raise ConnectorError(
            f"AWS profile '{cm.aws_profile}' not found", _aws_hint(source)
        ) from exc


def aws_credentials(source: SourceConfig) -> AwsCreds:
    """Resolve AWS credentials through env_map, the profile or the default chain."""
    cm = source.credentials
    em = cm.env_map
    if em.get("access_key_id") or em.get("secret_access_key"):
        kid, sec = _env(em.get("access_key_id")), _env(em.get("secret_access_key"))
        if not (kid and sec):
            missing = [
                em[k]
                for k in ("access_key_id", "secret_access_key")
                if em.get(k) and not _env(em[k])
            ]
            raise ConnectorError(
                f"AWS credentials not found: {', '.join(missing) or 'env_map'} not set",
                f"set {' and '.join(missing) or 'the mapped env vars'}",
            )
        return AwsCreds(kid, sec, _env(em.get("session_token")), cm.region, "env_map")
    import botocore.exceptions

    session = aws_session(source)
    try:
        creds = session.get_credentials()
        frozen = creds.get_frozen_credentials() if creds else None
    except (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError) as exc:
        raise ConnectorError(
            "AWS credentials could not be loaded: " + redact(type(exc).__name__, source),
            _aws_hint(source),
        ) from None
    if not frozen or not frozen.access_key:
        raise ConnectorError("AWS credentials not found", _aws_hint(source))
    via = f"AWS profile '{cm.aws_profile}'" if cm.aws_profile else f"AWS chain ({creds.method})"
    region = cm.region or session.region_name
    return AwsCreds(frozen.access_key, frozen.secret_key, frozen.token, region, via)


# --------------------------------------------------------------------------- databases


@dataclass(frozen=True)
class DbParams:
    host: str | None = None
    port: str | None = None
    user: str | None = None
    password: str | None = None
    database: str | None = None
    extra: tuple[tuple[str, str], ...] = ()  # e.g. sslmode
    via: str = ""

    def __repr__(self) -> str:
        return f"DbParams(host={self.host!r}, database={self.database!r}, via={self.via!r})"


_PG_ENV = {
    "host": "PGHOST",
    "port": "PGPORT",
    "user": "PGUSER",
    "password": "PGPASSWORD",
    "database": "PGDATABASE",
}
_MYSQL_ENV = {
    "host": "MYSQL_HOST",
    "port": "MYSQL_TCP_PORT",
    "user": "MYSQL_USER",
    "password": "MYSQL_PWD",
    "database": "MYSQL_DATABASE",
}


def _parse_dsn(dsn: str) -> dict[str, str]:
    dsn = dsn.strip()
    if "://" in dsn:
        u = urlsplit(dsn)
        out = {
            "host": u.hostname or "",
            "port": str(u.port) if u.port else "",
            "user": unquote(u.username or ""),
            "password": unquote(u.password or ""),
            "database": unquote(u.path.lstrip("/")),
        }
        for part in (u.query or "").split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k] = unquote(v)
        return {k: v for k, v in out.items() if v}
    out = {}
    for m in re.finditer(r"(\w+)\s*=\s*('(?:[^'\\]|\\.)*'|\S+)", dsn):  # libpq key=value
        k, v = m[1], m[2].strip("'")
        out[{"dbname": "database"}.get(k, k)] = v
    return out


def db_params(source: SourceConfig) -> DbParams:
    """Postgres/MySQL settings: env_map > env DSN > DATABASE_URL > PG*/MYSQL_* > options."""
    std = _PG_ENV if source.kind is SourceKind.POSTGRES else _MYSQL_ENV
    cm = source.credentials
    vals: dict[str, str] = {}
    via: list[str] = []
    dsn_var = cm.env or ("DATABASE_URL" if _env("DATABASE_URL") and not cm.env_map else None)
    if dsn_var:
        dsn = _env(dsn_var)
        if not dsn:
            raise ConnectorError(
                f"{dsn_var} is not set",
                f"export {dsn_var}=<connection URL> before starting caliper label",
            )
        vals.update(_parse_dsn(dsn))
        via.append(dsn_var)
    for opt, var in cm.env_map.items():
        v = _env(var)
        if v is None:
            raise ConnectorError(f"{var} is not set", f"export {var} (mapped to '{opt}')")
        vals[{"dbname": "database"}.get(opt, opt)] = v
        via.append(var)
    for opt, var in std.items():
        if opt not in vals and _env(var):
            vals[opt] = _env(var)  # type: ignore[assignment]
            via.append(var)
    # Non-secret connection options may also live in source.options (host, port, database).
    for opt in ("host", "port", "user", "database"):
        if opt not in vals and source.options.get(opt) is not None:
            vals[opt] = str(source.options[opt])
    extra = tuple((k, v) for k, v in vals.items() if k not in _PG_ENV)
    return DbParams(
        host=vals.get("host"),
        port=vals.get("port"),
        user=vals.get("user"),
        password=vals.get("password"),
        database=vals.get("database"),
        extra=extra,
        via=", ".join(via) or "defaults",
    )


# --------------------------------------------------------------------------- GCP


def _adc_file() -> Path | None:
    p = _env("GOOGLE_APPLICATION_CREDENTIALS")
    if p:
        return Path(p)
    cfg = _env("CLOUDSDK_CONFIG") or str(Path.home() / ".config" / "gcloud")
    return Path(cfg) / "application_default_credentials.json"


def gcs_hmac(source: SourceConfig) -> tuple[str, str] | None:
    em = source.credentials.env_map
    kid = _env(em.get("key_id", "GCS_HMAC_KEY_ID"))
    sec = _env(em.get("secret", "GCS_HMAC_SECRET"))
    return (kid, sec) if kid and sec else None


# --------------------------------------------------------------------------- status


def credential_status(source: SourceConfig) -> list[CredentialCheck]:
    """Which variable or chain each source uses and whether it resolved. Never a value."""
    k = source.kind
    cm = source.credentials
    if k in (SourceKind.FILE, SourceKind.FOLDER, SourceKind.SQLITE):
        return [CredentialCheck(name="local files", kind="none", resolved=True)]
    if k is SourceKind.S3:
        try:
            c = aws_credentials(source)
        except ConnectorError as exc:
            name = f"AWS profile '{cm.aws_profile}'" if cm.aws_profile else "AWS default chain"
            if cm.env_map:
                name = ", ".join(cm.env_map.values())
            return [CredentialCheck(name=name, kind="aws", resolved=False, hint=exc.hint)]
        checks = [CredentialCheck(name=c.via, kind="aws", resolved=True)]
        if cm.endpoint_url:
            checks.append(
                CredentialCheck(name=f"endpoint {cm.endpoint_url}", kind="none", resolved=True)
            )
        return checks
    if k is SourceKind.GCS:
        if gcs_hmac(source):
            em = cm.env_map
            names = f"{em.get('key_id', 'GCS_HMAC_KEY_ID')}, {em.get('secret', 'GCS_HMAC_SECRET')}"
            return [CredentialCheck(name=names, kind="env", resolved=True)]
        adc = _adc_file()
        ok = bool(adc and adc.is_file())
        return [
            CredentialCheck(
                name="GCP application default credentials",
                kind="gcp",
                resolved=ok,
                hint=None
                if ok
                else "run `gcloud auth application-default login` or set "
                "GOOGLE_APPLICATION_CREDENTIALS (or GCS_HMAC_KEY_ID/GCS_HMAC_SECRET)",
            )
        ]
    if k is SourceKind.HTTP:
        if cm.env:
            ok = _env(cm.env) is not None
            return [
                CredentialCheck(
                    name=cm.env,
                    kind="env",
                    resolved=ok,
                    hint=None if ok else f"export {cm.env}=<bearer token>",
                )
            ]
        return [CredentialCheck(name="no credentials (public URL)", kind="none", resolved=True)]
    if k in (SourceKind.POSTGRES, SourceKind.MYSQL):
        std = _PG_ENV if k is SourceKind.POSTGRES else _MYSQL_ENV
        out: list[CredentialCheck] = []
        if cm.env:
            ok = _env(cm.env) is not None
            out.append(
                CredentialCheck(
                    name=cm.env,
                    kind="env",
                    resolved=ok,
                    hint=None if ok else f"export {cm.env}=<connection URL>",
                )
            )
        for opt, var in cm.env_map.items():
            ok = _env(var) is not None
            out.append(
                CredentialCheck(
                    name=var,
                    kind="env",
                    resolved=ok,
                    hint=None if ok else f"export {var} (the {opt})",
                )
            )
        if not cm.env and not cm.env_map:
            if _env("DATABASE_URL"):
                out.append(CredentialCheck(name="DATABASE_URL", kind="env", resolved=True))
            else:
                used = [v for v in std.values() if _env(v)]
                pw = std["password"]
                ok = bool(used)
                out.append(
                    CredentialCheck(
                        name=", ".join(used) if used else f"{' / '.join(std.values())}",
                        kind="env",
                        resolved=ok,
                        hint=None
                        if ok
                        else f"set DATABASE_URL, or {std['host']}, {std['user']} and {pw}",
                    )
                )
        return out
    return [CredentialCheck(name="plugin-defined", kind="none", resolved=True)]
