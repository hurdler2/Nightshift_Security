"""Push payload construction (spec §11.1).

One hard rule: a push notification leaves our infrastructure and lands on a phone,
possibly on a lock screen, possibly through a third-party relay. It therefore carries
no recorder credentials and no RTSP URL — ever (spec §11.1, §19.2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from app.modules.rules.risk import Severity

#: Belt and braces: anything matching these must never appear in a payload.
_FORBIDDEN = re.compile(r"(?i)(rtsp://|\bpassword\b|\bpasswd\b|://[^/@\s]+:[^/@\s]+@)")

TITLES: dict[str, str] = {
    "person_detected": "İnsan algılandı",
    "vehicle_detected": "Araç algılandı",
    "line_crossing": "Çizgi geçişi",
    "zone_intrusion": "Bölge ihlali",
    "video_tampering": "Kameraya müdahale",
    "video_loss": "Kamera görüntüsü kayboldu",
    "storage_failure": "Disk hatası",
    "storage_low_space": "Disk alanı azaldı",
    "device_silent": "Cihaz sessizleşti",
    "site_offline": "Şantiye bağlantısı kesildi",
    "unknown_alarm": "Alarm",
}


class UnsafePayload(RuntimeError):
    """Raised when a payload would leak a credential or a stream URL."""


@dataclass(slots=True)
class PushPayload:
    event_id: UUID
    alarm_id: UUID
    severity: Severity
    site_name: str
    camera_name: str
    title: str
    body: str
    occurred_at: datetime
    risk_score: int
    reasons: list[str] = field(default_factory=list)
    has_snapshot: bool = False
    type: str = "security_alert"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": self.type,
            "event_id": str(self.event_id),
            "alarm_id": str(self.alarm_id),
            "severity": self.severity.value,
            "site_name": self.site_name,
            "camera_name": self.camera_name,
            "title": self.title,
            "body": self.body,
            "occurred_at": self.occurred_at.isoformat(),
            "risk_score": self.risk_score,
            "reasons": self.reasons,
            "has_snapshot": self.has_snapshot,
        }
        _assert_safe(payload)
        return payload

    @property
    def deep_link(self) -> str:
        """Spec §11.1: the push opens the alarm detail directly."""
        return f"nightshift://alarms/{self.alarm_id}"


def build_push(
    *,
    event_id: UUID,
    alarm_id: UUID,
    severity: Severity,
    site_name: str,
    camera_name: str,
    event_type: str,
    occurred_at: datetime,
    risk_score: int,
    reasons: list[str] | None = None,
    has_snapshot: bool = False,
    ai_unavailable: bool = False,
) -> PushPayload:
    title = TITLES.get(event_type, TITLES["unknown_alarm"])
    if severity is Severity.CRITICAL:
        title = f"KRİTİK: {title}"

    body_parts = [f"{site_name} · {camera_name}"]
    if reasons:
        body_parts.append(", ".join(reasons[:3]))
    if ai_unavailable:
        # Spec §11.3: say so rather than implying a verification that did not happen.
        body_parts.append("AI doğrulama geçici olarak kullanılamıyor")

    return PushPayload(
        event_id=event_id,
        alarm_id=alarm_id,
        severity=severity,
        site_name=site_name,
        camera_name=camera_name,
        title=title,
        body=" — ".join(body_parts),
        occurred_at=occurred_at,
        risk_score=risk_score,
        reasons=reasons or [],
        has_snapshot=has_snapshot,
    )


def _assert_safe(payload: dict[str, Any]) -> None:
    rendered = str(payload)
    match = _FORBIDDEN.search(rendered)
    if match:
        raise UnsafePayload(f"refusing to send a push containing {match.group(0)!r} (spec §11.1)")
