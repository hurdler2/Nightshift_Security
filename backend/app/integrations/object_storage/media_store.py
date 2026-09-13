"""Media storage on S3-compatible object storage (spec §19.2, §19.4).

Three rules, all enforced here rather than by convention:

* **The bucket is private.** Nothing is ever served by public URL; every read is a
  short-lived presigned URL. A snapshot of a construction site at 02:00 is evidence,
  not a public asset.
* **Keys are tenant-scoped and time-ordered**, so a lifecycle rule can expire a
  tenant's media by plan and an operator can find a day's evidence by prefix.
* **Content is validated before it is stored.** An "image/jpeg" that is not a JPEG is
  refused: the attachment came from a device on a customer's network, not from us.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

log = logging.getLogger(__name__)

JPEG_MAGIC = b"\xff\xd8\xff"
MP4_BRANDS = (b"ftyp",)
MIN_IMAGE_BYTES = 1024
#: One alarm snapshot is a few hundred KB; a clip is a few MB.
MAX_OBJECT_BYTES = 64 * 1024 * 1024

#: Spec §19.4 — retention by plan, in days.
RETENTION_DAYS: dict[str, int] = {
    "starter": 7,
    "business": 30,
    "pro": 90,
    "enterprise": 365,
}


class MediaRejected(ValueError):
    """Raised when content does not match what it claims to be."""


class MediaKind:
    SNAPSHOT = "snapshot"
    ANNOTATED = "annotated"
    CLIP = "clip"
    THUMBNAIL = "thumbnail"


_EXTENSIONS = {
    MediaKind.SNAPSHOT: "jpg",
    MediaKind.ANNOTATED: "jpg",
    MediaKind.THUMBNAIL: "jpg",
    MediaKind.CLIP: "mp4",
}


def build_key(
    *,
    tenant_id: UUID | str,
    site_id: UUID | str,
    event_id: UUID | str,
    kind: str,
    occurred_at: datetime,
) -> str:
    """`{tenant}/{site}/{yyyy}/{mm}/{dd}/{event}/{kind}.{ext}` (spec §19.4).

    Tenant first so a lifecycle policy or a deletion request can work on one prefix;
    date next so a day's evidence is one listing.
    """
    moment = occurred_at.astimezone(UTC) if occurred_at.tzinfo else occurred_at
    extension = _EXTENSIONS.get(kind, "bin")
    return (
        f"{tenant_id}/{site_id}/{moment:%Y}/{moment:%m}/{moment:%d}/{event_id}/{kind}.{extension}"
    )


def expiry_for(plan: str, *, from_time: datetime | None = None) -> datetime:
    days = RETENTION_DAYS.get(plan.lower(), RETENTION_DAYS["business"])
    return (from_time or datetime.now(UTC)) + timedelta(days=days)


def validate(content: bytes, kind: str) -> str:
    """Check the bytes really are what `kind` implies. Returns the content type."""
    if not content:
        raise MediaRejected("empty object")
    if len(content) > MAX_OBJECT_BYTES:
        raise MediaRejected(f"object exceeds {MAX_OBJECT_BYTES} bytes")

    if kind in (MediaKind.SNAPSHOT, MediaKind.ANNOTATED, MediaKind.THUMBNAIL):
        if len(content) < MIN_IMAGE_BYTES or not content.startswith(JPEG_MAGIC):
            raise MediaRejected("content is not a JPEG")
        return "image/jpeg"

    if kind == MediaKind.CLIP:
        # ISO-BMFF: the 'ftyp' box sits at offset 4 in a well-formed MP4.
        if not any(brand in content[:32] for brand in MP4_BRANDS):
            raise MediaRejected("content is not an MP4")
        return "video/mp4"

    raise MediaRejected(f"unknown media kind: {kind}")


@dataclass(slots=True)
class StoredMedia:
    key: str
    content_type: str
    size_bytes: int
    sha256: str
    expires_at: datetime


class MediaStore:
    """Thin wrapper over an S3-compatible client (boto3 or a test double)."""

    def __init__(
        self,
        client: Any,
        bucket: str,
        *,
        presign_ttl_seconds: int = 300,
    ) -> None:
        self._client = client
        self._bucket = bucket
        self._ttl = presign_ttl_seconds

    @property
    def bucket(self) -> str:
        return self._bucket

    def put(
        self,
        content: bytes,
        *,
        tenant_id: UUID | str,
        site_id: UUID | str,
        event_id: UUID | str,
        kind: str,
        occurred_at: datetime,
        plan: str = "business",
    ) -> StoredMedia:
        content_type = validate(content, kind)
        key = build_key(
            tenant_id=tenant_id,
            site_id=site_id,
            event_id=event_id,
            kind=kind,
            occurred_at=occurred_at,
        )
        expires_at = expiry_for(plan, from_time=occurred_at)

        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=content,
            ContentType=content_type,
            Metadata={
                "tenant-id": str(tenant_id),
                "kind": kind,
                "expires-at": expires_at.isoformat(),
            },
        )
        log.info("stored %s for tenant %s (%d bytes)", kind, tenant_id, len(content))
        return StoredMedia(
            key=key,
            content_type=content_type,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            expires_at=expires_at,
        )

    def get(self, key: str) -> bytes:
        response = self._client.get_object(Bucket=self._bucket, Key=key)
        return response["Body"].read()

    def presigned_url(self, key: str, *, ttl_seconds: int | None = None) -> str:
        """Short-lived read URL. The only way media ever leaves storage."""
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=ttl_seconds or self._ttl,
        )

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=key)

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
        except Exception:
            return False
        return True


def build_client(
    *,
    endpoint: str,
    access_key: str,
    secret_key: str,
    region: str = "us-east-1",
    path_style: bool = True,
):
    """boto3 client configured for MinIO in dev and any S3 service in production."""
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path" if path_style else "auto"},
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )
