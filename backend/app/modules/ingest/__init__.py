"""Alarm-mail ingest - PHASE 2/3 (spec 7).

Entry point for every event in V1: the recorder mails us, we authenticate it,
parse it, and hand a vendor-neutral event to the rest of the system.
"""

from app.modules.ingest.pipeline import (
    DeviceIdentity,
    IngestedEvent,
    IngestedMedia,
    IngestResult,
    ParseStatus,
    ingest,
)
from app.modules.ingest.profiles import PROVISIONAL_DAHUA, EmailProfile, parse_body

__all__ = [
    "PROVISIONAL_DAHUA",
    "DeviceIdentity",
    "EmailProfile",
    "IngestResult",
    "IngestedEvent",
    "IngestedMedia",
    "ParseStatus",
    "ingest",
    "parse_body",
]
