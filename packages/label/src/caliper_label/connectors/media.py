"""Resolve a media cell (image URL, s3:// key, local path) to something the browser can load.

* ``http(s)://`` URLs pass through.
* ``s3://bucket/key`` (or a key relative to an S3 source's folder) becomes a short-lived
  presigned GET URL, generated on the server with the source's credentials. Buckets are never
  made public.
* Local paths are served only from allowed roots: the project dir, the source's folder, and
  ``source.options.media_roots``. ``..``, absolute paths elsewhere and symlinks that leave a root
  are refused, as is anything that is not an image file type.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from ..project import SourceConfig, SourceKind
from .base import ConnectorError, MediaRef

MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".avif": "image/avif",
    ".bmp": "image/bmp",
}
PRESIGN_SECONDS = 300
_REMOTE = ("s3://", "gs://", "gcs://", "http://", "https://", "r2://")


def _static_prefix(path: str) -> str:
    """The part of a path/URL before the first glob character."""
    cut = min((i for i, ch in enumerate(path) if ch in "*?["), default=len(path))
    return path[:cut]


def default_roots(source: SourceConfig | None, project_dir: Path | None) -> list[Path]:
    roots: list[Path] = []
    base = Path(project_dir) if project_dir else Path.cwd()
    if project_dir:
        roots.append(base)
    if source is not None:
        if source.path and not source.path.startswith(_REMOTE):
            p = base / _static_prefix(source.path)
            if source.kind is SourceKind.FOLDER:
                roots.append(p)
            else:
                roots.append(p if p.is_dir() else p.parent)
        for r in source.options.get("media_roots") or []:
            roots.append(base / str(r))
    out: list[Path] = []
    for r in roots:
        rr = r.resolve()
        if rr not in out:
            out.append(rr)
    return out


def mime_for(path: str) -> str | None:
    suffix = Path(path.split("?", 1)[0]).suffix.lower()
    return MEDIA_TYPES.get(suffix) or mimetypes.guess_type(path.split("?", 1)[0])[0]


def resolve_local(ref: str, roots: list[Path]) -> MediaRef:
    if "\x00" in ref or not ref.strip():
        raise ConnectorError("bad media path", None)
    if not roots:
        raise ConnectorError("no media roots configured", "open the project from its directory")
    cand = Path(ref)
    targets = [cand] if cand.is_absolute() else [r / cand for r in roots]
    outside = False
    for t in targets:
        rt = t.resolve()
        if not any(rt.is_relative_to(r) for r in roots):
            outside = True
            continue
        if rt.suffix.lower() not in MEDIA_TYPES:
            raise ConnectorError(
                "not an image file type", "only png, jpg, gif, webp, svg, avif, bmp are served"
            )
        if rt.is_file():
            return MediaRef(kind="file", path=str(rt), mime=MEDIA_TYPES[rt.suffix.lower()])
    if outside:
        raise ConnectorError(
            "media path is outside the allowed folders",
            "keep images under the project or data folder, or add the folder to "
            "source.options.media_roots",
        )
    raise ConnectorError("image not found", "check the path in the data")


def presign_s3(url: str, source: SourceConfig | None, expires: int = PRESIGN_SECONDS) -> MediaRef:
    from ..credentials import aws_credentials, redact

    rest = url.split("://", 1)[1]
    bucket, _, key = rest.partition("/")
    if not bucket or not key:
        raise ConnectorError(f"not an object URL: {url}", "use s3://bucket/key")
    src = source or SourceConfig(kind=SourceKind.S3)
    import boto3
    from botocore.config import Config

    creds = aws_credentials(src)
    endpoint = src.credentials.endpoint_url
    client = boto3.client(
        "s3",
        aws_access_key_id=creds.key_id,
        aws_secret_access_key=creds.secret,
        aws_session_token=creds.token,
        region_name=creds.region or "us-east-1",
        endpoint_url=endpoint or None,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path" if endpoint else "auto"},
        ),
    )
    try:
        signed = client.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=int(expires)
        )
    except Exception as exc:
        raise ConnectorError("could not sign the S3 URL: " + redact(str(exc), src), None) from None
    return MediaRef(kind="url", url=signed, mime=mime_for(key))


def resolve_ref(ref: str, roots: list[Path], source: SourceConfig | None = None) -> MediaRef:
    if not isinstance(ref, str) or not ref:
        raise ConnectorError("empty media reference", None)
    low = ref.lower()
    if low.startswith(("http://", "https://")):
        return MediaRef(kind="url", url=ref, mime=mime_for(ref))
    expires = int((source.options.get("presign_seconds") if source else None) or PRESIGN_SECONDS)
    if low.startswith(("s3://", "r2://")):
        return presign_s3("s3://" + ref.split("://", 1)[1], source, expires)
    if low.startswith(("gs://", "gcs://")):
        raise ConnectorError(
            "GCS images can't be served yet",
            "store images on S3/HTTP, or use public https://storage.googleapis.com URLs",
        )
    if source is not None and source.kind is SourceKind.S3 and source.path:
        # a key relative to the source's folder: s3://bucket/exports/ + images/a.png
        prefix = _static_prefix(source.path)
        prefix = prefix if prefix.endswith("/") else prefix.rsplit("/", 1)[0] + "/"
        return presign_s3(prefix + ref.lstrip("/"), source, expires)
    return resolve_local(ref, roots)
