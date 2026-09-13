# API

Kaynak: spec §38, §39, §40. OpenAPI şeması çalışan servisten alınır:
`http://localhost:8000/openapi.json` (`/docs` üzerinden gezilebilir).

PHASE 0'da yalnızca `/health` ve `/ready` yayında. Aşağıdaki tablo her endpoint'in
hangi fazda mount edileceğini sabitler; router eklendikçe bu liste güncellenir.

## Uygulama API'si (`/v1`)

| Faz | Endpoint |
|-----|----------|
| 2 | `POST /v1/auth/register`, `/login`, `/refresh`, `/logout`, `/forgot-password`, `/reset-password`, `GET /v1/me` |
| 2 | `GET /v1/tenants/current`, `PATCH /v1/tenants/current` |
| 2 | `POST /v1/edges/enrollment-codes`, `POST /v1/edges/enroll`, `GET /v1/edges`, `GET /v1/edges/{id}`, `POST /v1/edges/{id}/commands` |
| 3 | `GET|POST /v1/sites`, `GET|PATCH|DELETE /v1/sites/{id}` |
| 3 | `GET|POST /v1/devices`, `GET|PATCH /v1/devices/{id}`, `POST /v1/devices/{id}/probe`, `POST /v1/devices/{id}/sync-channels` |
| 3 | `GET /v1/cameras`, `GET|PATCH /v1/cameras/{id}`, `POST /v1/cameras/{id}/snapshot`, `POST /v1/cameras/{id}/zones`, `GET /v1/cameras/{id}/health` |
| 4 | `GET /v1/events`, `GET /v1/events/{id}` |
| 6 | `GET|POST /v1/rules`, `GET|PATCH|DELETE /v1/rules/{id}` |
| 7 | `GET /v1/alarms`, `GET /v1/alarms/{id}`, `POST /v1/alarms/{id}/acknowledge`, `POST /v1/alarms/{id}/resolve` |
| 9 | `POST /v1/cameras/{id}/live-sessions` |
| 13 | `GET /v1/billing/subscription`, `/usage`, `POST /v1/billing/checkout`, `/portal`, `/webhook` |
| 14 | `GET /v1/analytics/overview`, `/events`, `/response-times`, `/device-health` |

## Edge API'si (`/internal/edge/v1`) — makine kimliği zorunlu

| Faz | Endpoint |
|-----|----------|
| 2 | `POST /heartbeat`, `GET /config`, `WS /commands` |
| 4 | `POST /events`, `POST /media/presign`, `POST /media/complete` |
| 10 | `POST /device-health` |

## Realtime (`/v1/realtime`, faz 4+)

Olay tipleri: `alarm.created`, `alarm.updated`, `event.created`,
`device.status_changed`, `camera.status_changed`, `edge.status_changed`,
`live_session.ready`.

İstemci exponential backoff ile yeniden bağlanır.

## Sözleşme kuralları

- Kimlik: `Authorization: Bearer <access_jwt>`; tenant kimliği token içinden okunur,
  gövdeden veya query parametresinden **asla** alınmaz.
- Her yanıtta `x-request-id` döner; korelasyon için istemci de gönderebilir.
- Medya yanıtları kısa ömürlü presigned URL taşır; bucket public değildir.
- Hiçbir yanıt XVR credential veya credential içeren RTSP URL taşımaz.
- Sayfalama: `?limit=&cursor=`; varsayılan sıralama olay zamanına göre azalan.
- Hata gövdesi: `{"detail": "...", "code": "...", "request_id": "..."}`.
