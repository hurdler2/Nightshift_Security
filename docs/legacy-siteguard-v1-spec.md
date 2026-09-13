# SiteGuard AI — V1 Teknik Spesifikasyon ve Claude Code Uygulama Planı

> **Amaç:** Dahua XVR1B08H-I tabanlı, çoklu şantiye ve çoklu müşteri destekleyen, Android/iOS mobil uygulaması üzerinden çalışan, insan/araç algılama + ikinci AI doğrulama + alarm/olay yönetimi + canlı izleme + sağlık izleme + abonelik altyapısı sunan SaaS güvenlik platformunun V1 sürümünü üretmek.
>
> **Hedef pilot:** 14 adet Dahua XVR1B08H-I, XVR başına yaklaşık 5 analog kamera, toplam yaklaşık 70 kamera.
>
> **Tarih:** 2026-09-12

---

# 0. CLAUDE CODE İÇİN ANA TALİMAT

Bu doküman ürünün **source of truth** dosyasıdır. Claude Code bu projeyi geliştirirken aşağıdaki kurallara uymalıdır:

1. Bir özelliği sessizce atlama.
2. TODO bırakılması gerekiyorsa `TODO(V1-BLOCKER)` veya `TODO(V1-NONBLOCKER)` etiketi kullan.
3. Donanım/firmware davranışı garanti edilemiyorsa sahte sonuç üretme; capability probe ve fallback uygula.
4. Dahua entegrasyonunu doğrudan mobil uygulamaya koyma. Tüm cihaz erişimi **Edge Agent** üzerinden yapılmalı.
5. XVR kullanıcı adı/parolalarını mobil uygulamaya veya cloud loglarına gönderme.
6. RTSP ve CGI portlarını internete doğrudan açmayı gerektiren çözüm üretme.
7. CGNAT altında çalışmak zorunludur.
8. Sistem internet kesildiğinde edge tarafında olay kuyruğu tutmalıdır.
9. Çoklu müşteri izolasyonu (`tenant_id`) en baştan uygulanmalıdır.
10. Ana servisler Docker ile ayağa kalkmalıdır.
11. Her modül için unit test, integration test ve mümkünse contract test yaz.
12. V1 tamamen çalışmadan mikroservis sayısını gereksiz büyütme.
13. API ve event payload'ları OpenAPI/JSON Schema ile belgelenmelidir.
14. Mobil uygulama Flutter ile Android + iOS tek kod tabanında geliştirilmelidir.
15. Cloud backend için Python 3.12+ ve FastAPI kullanılmalıdır.
16. Edge Agent için Python 3.12+ `asyncio` tabanlı mimari kullanılmalıdır.
17. PostgreSQL veri kaynağıdır; Redis cache/lock/queue yardımı için kullanılabilir.
18. Event/job kuyruğu için V1'de RabbitMQ tercih edilir; Kafka gereksizdir.
19. Medya dosyaları S3 uyumlu object storage'da tutulmalıdır; local disk kalıcı kaynak olmamalıdır.
20. AI inference modülü provider interface ile soyutlanmalı; model daha sonra değiştirilebilmelidir.

---

# 1. ARAŞTIRMA SONUCU — DAHUA XVR1B08H-I İLE PROGRAMATİK ENTEGRASYON

## 1.1 Donanımın doğrulanan kabiliyetleri

Dahua'nın XVR1B08H-I datasheet'i şu özellikleri doğruluyor:

- 8 analog BNC kanal.
- HDCVI/AHD/TVI/CVBS desteği.
- XVR üzerinde **8 kanal SMD Plus**.
- SMD Plus insan ve motorlu araç için ikincil filtreleme yapar.
- HTTP/HTTPS, TCP/IP, RTSP, UDP gibi ağ protokolleri.
- CGI conformant.
- ONVIF desteği.
- Motion, video loss, tampering, storage anomaly gibi alarm kategorileri.
- HDD hata/alan azlığı gibi anomaly alarm türleri.
- Dahua dokümanında IP kanal genişletmesi belirli şekilde kullanılırsa recorder-side SMD'nin devre dışı kalabileceği notu vardır.

**V1 kararı:** XVR'ın kendi SMD'si ilk filtre olarak kullanılacak, kendi AI sunucumuz ikinci doğrulama katmanı olacaktır.

---

# 2. DAHUA ENTEGRASYONUNDA KESİN TEKNİK KARAR

## 2.1 Birincil entegrasyon yöntemi

### SMD ve alarm olayları

**Birincil yöntem: Dahua CGI event stream**

Endpoint:

```text
GET http://<XVR_IP>/cgi-bin/eventManager.cgi?action=attach&codes=[All]&heartbeat=5
```

Authentication:

```text
HTTP Digest Authentication
```

Beklenen response tipi:

```text
multipart/x-mixed-replace
```

Örnek olay:

```text
Code=SmartMotionHuman;action=Start;index=0;data={...}
```

Önemli Dahua event kodları:

```text
SmartMotionHuman       -> SMD insan
SmartMotionVehicle     -> SMD araç
VideoMotion            -> klasik hareket
VideoLoss              -> görüntü kaybı
VideoBlind             -> görüntü kapatma/tamper benzeri olay
StorageNotExist        -> disk yok
StorageFailure         -> disk/storage hatası
StorageLowSpace        -> düşük disk alanı
CrossLineDetection     -> tripwire/çizgi geçişi (destek varsa)
CrossRegionDetection   -> intrusion/bölge geçişi (destek varsa)
VideoAbnormalDetection -> scene abnormal (destek varsa)
NewFile                -> yeni kayıt dosyası (firmware'e göre)
```

**Neden `codes=[All]`?**

Dahua topluluk entegrasyonlarında bazı firmware'lerde seçilmiş uzun event kodu listelerinde olayların kaçabildiği görülmüştür. İlk discovery aşamasında `All` dinleyip event'leri normalize ederek filtrelemek daha güvenlidir. Production'da cihazın davranışı doğrulandıktan sonra gerektiğinde daraltılabilir.

---

## 2.2 Capability discovery

Edge Agent cihaz eklenirken önce aşağıdakileri çalıştırmalıdır:

```text
GET /cgi-bin/eventManager.cgi?action=getExposureEvents
GET /cgi-bin/eventManager.cgi?action=getCaps
```

Not: Eski firmware'lerde `getExposureEvents` veya bazı capability çağrıları eksik olabilir. Bu durumda `attach&codes=[All]` ile 30–60 saniyelik probe çalıştır ve cihaz üzerinde manuel SMD testi yaptır.

Ayrıca cihazın firmware/model bilgisi discovery sırasında saklanmalıdır.

Minimum kayıt alanları:

```text
manufacturer
model
serial_number
firmware_version
web_version
channel_count
rtsp_port
http_port
https_port
supports_cgi_events
supports_smd_human
supports_smd_vehicle
supports_rtsp_playback
supports_snapshot
supports_video_blind
supports_storage_events
capabilities_json
last_capability_probe_at
```

---

# 3. SNAPSHOT ALMA

## 3.1 Birincil yöntem

```text
GET http://<XVR_IP>/cgi-bin/snapshot.cgi?channel=<CHANNEL>
```

Authentication:

```text
Digest Auth
```

Başarılı response:

```text
Content-Type: image/jpeg
```

### Kanal numarası uyarısı

Dahua HTTP API v2.63 dokümanında snapshot kanalının 1'den başladığı belirtilmektedir.

Ancak bazı eski Dahua dokümanlarında/firmware'lerinde 0-based davranış görülebilir.

Bu yüzden **hard-code etme**.

Device onboarding sırasında channel mapping testi yap:

```text
logical_channel 1 -> snapshot channel 1 test
logical_channel 1 -> gerekirse snapshot channel 0 test
```

Sonuç `device_channel_map` tablosuna kaydedilsin.

---

# 4. CANLI RTSP VIDEO

## 4.1 Doğrulanmış Dahua RTSP formatı

Main stream:

```text
rtsp://<USER>:<PASS>@<XVR_IP>:554/cam/realmonitor?channel=<CHANNEL>&subtype=0
```

Sub stream:

```text
rtsp://<USER>:<PASS>@<XVR_IP>:554/cam/realmonitor?channel=<CHANNEL>&subtype=1
```

Dahua dokümanlarında:

```text
channel: 1-based
subtype=0 -> main stream
subtype=1 -> first sub stream
```

### V1 kullanım kararı

- AI/event preview için mümkün olduğunca **substream** kullan.
- Kullanıcı canlı ekrana geçtiğinde kalite seçimine göre main veya substream aç.
- 70 kamerayı 7/24 cloud'a taşımak YASAK.
- Cloud live stream **on-demand** olmalı.

---

# 5. XVR KAYITLARINDAN PLAYBACK / EVENT CLIP

## 5.1 RTSP playback URL

Dahua HTTP API dokümanında playback formatı:

```text
rtsp://<USER>:<PASS>@<XVR_IP>:554/cam/playback?channel=<CHANNEL>&starttime=<START>&endtime=<END>
```

Time format:

```text
YYYY_MM_DD_HH_MM_SS
```

Örnek:

```text
/cam/playback?channel=1&starttime=2026_09_12_22_10_00&endtime=2026_09_12_22_10_30
```

### Event clip standardı

Varsayılan:

```text
T-10 saniye -> event -> T+20 saniye
```

Toplam hedef klip:

```text
30 saniye
```

Configurable:

```text
pre_event_seconds: 10
post_event_seconds: 20
```

### Fallback

Bazı firmware'lerde RTSP playback farklı davranabilir.

Fallback sırası:

1. RTSP playback.
2. Dahua media file search/download adapter (firmware destekliyorsa).
3. Edge rolling buffer (sadece seçilmiş kritik kameralarda veya capability problemi olan cihazlarda).

Sistem playback başarısız olduğunda event'i kaybetmemeli. Snapshot + metadata yine cloud'a çıkmalıdır.

---

# 6. NETSDK FALLBACK

CGI API ana yöntemdir çünkü Python Edge Agent için hafif ve kolaydır.

Ancak belirli XVR firmware'lerinde SMD event'i CGI üzerinden düzgün gelmezse Dahua NetSDK fallback yazılmalıdır.

NetSDK tarafında araştırmada doğrulanan ilgili fonksiyon aileleri:

```text
CLIENT_Init
CLIENT_LoginEx2
CLIENT_SetAutoReconnect
CLIENT_SetDVRMessCallBack
CLIENT_StartListenEx
CLIENT_StopListen
CLIENT_RealPlayEx
CLIENT_SnapPictureEx
CLIENT_DownloadByTimeEx
CLIENT_QueryRecordFile
CLIENT_Logout
CLIENT_Cleanup
```

**V1 mimari kuralı:**

```text
DahuaDeviceAdapter
    ├── DahuaCgiAdapter      (primary)
    └── DahuaNetSdkAdapter   (fallback, feature flag)
```

Cloud ve mobil uygulama hangi adapter'ın kullanıldığını bilmemelidir.

---

# 7. ONVIF'İN ROLÜ

ONVIF'i şu işler için kullanabiliriz:

- cihaz keşfi,
- temel profil bilgileri,
- bazı stream URI bilgileri,
- vendor-independent cihaz ekleme geleceği.

Ancak Dahua SMD human/vehicle event'lerinde **birincil yöntem ONVIF olmayacak**.

Sebep:

- SmartMotionHuman ve SmartMotionVehicle Dahua'ya özgü event isimleri olabilir.
- ONVIF event mapping firmware'e göre farklılaşabilir.
- CGI Dahua event payload'ını daha doğrudan verir.

---

# 8. GERÇEK V1 SİSTEM MİMARİSİ

```text
Analog Cameras
     │
     ▼
Dahua XVR1B08H-I
     │
     │ LAN
     ▼
SiteGuard Edge Agent
     │
     ├── CGI Event Listener
     ├── Snapshot Fetcher
     ├── RTSP Controller
     ├── Playback Clip Fetcher
     ├── Device Health Monitor
     ├── Offline Queue
     ├── Local Secret Store
     └── Secure Outbound Tunnel / HTTPS
     │
     │ Internet / CGNAT-safe
     ▼
Cloud API
     │
     ├── Event Ingestion
     ├── Rule Engine
     ├── AI Orchestrator
     ├── Alert Engine
     ├── Media Service
     ├── Subscription/Billing
     ├── Tenant/Auth/RBAC
     └── Realtime Gateway
     │
     ├──────────────► PostgreSQL
     ├──────────────► Redis
     ├──────────────► RabbitMQ
     └──────────────► S3/MinIO
     │
     ▼
AI Inference Server(s)
     │
     ├── Person
     ├── Vehicle
     ├── PPE
     ├── Fire/Smoke
     ├── Zone/Intrusion
     ├── Loitering
     ├── Crowd
     ├── Camera Scene/Tamper
     └── Risk Engine
     │
     ▼
Notification Service
     │
     ├── FCM Android
     └── APNs iOS
     │
     ▼
Flutter Mobile App
```

---

# 9. CGNAT-SAFE TASARIM

## Kesin kural

XVR üzerinde public port forwarding açılmayacak.

Edge Agent yalnızca dışarı doğru bağlantı başlatacaktır.

V1 seçenekleri:

```text
A) Edge -> Cloud HTTPS/WSS persistent connection
B) Edge -> WireGuard site tunnel
C) Edge -> Tailscale (pilot/dev)
```

Production SaaS için öneri:

```text
Edge outbound HTTPS/WSS + gerekirse WireGuard
```

Tailscale pilot için kullanılabilir ama SaaS'ın zorunlu dependency'si yapılmamalıdır.

---

# 10. EDGE GATEWAY DONANIMI

Minimum pilot:

```text
x86-64 mini PC
4 core CPU
8 GB RAM
128 GB SSD
1 Gbit Ethernet
Ubuntu Server 24.04 LTS veya güncel stabil LTS
```

Eğer edge üzerinde continuous AI yapılacaksa:

```text
NVIDIA GPU / Jetson / Intel iGPU/NPU opsiyonu
```

Ancak normal V1 event-driven AI'da ağır AI cloud/server tarafında olacaktır.

---

# 11. EDGE AGENT DETAYI

## 11.1 Sorumluluklar

Edge Agent:

1. XVR cihazlarını yönetir.
2. Digest auth ile CGI bağlantısı kurar.
3. Event stream'i sürekli dinler.
4. SMD event'lerini normalize eder.
5. Event geldiğinde snapshot alır.
6. Kural gerektiriyorsa playback klip üretir.
7. Cloud'a event metadata gönderir.
8. Snapshot/clip upload eder.
9. İnternet yoksa SQLite local queue kullanır.
10. İnternet gelince sırayla sync eder.
11. Device heartbeat yapar.
12. Kamera/XVR offline durumunu üretir.
13. Credential'ları şifreli lokal storage'da tutar.
14. Cloud komut kanalını dinler.
15. Kullanıcı canlı yayın istediğinde temporary live session başlatır.
16. Session bitince RTSP pull işlemini kapatır.

## 11.2 Edge process'leri

```text
edge-agent
├── device_manager
├── dahua
│   ├── cgi_client
│   ├── event_stream
│   ├── snapshot
│   ├── rtsp
│   ├── playback
│   ├── health
│   └── netsdk_fallback
├── media
│   ├── ffmpeg_manager
│   ├── clip_builder
│   └── live_session
├── queue
│   ├── sqlite_store
│   └── sync_worker
├── cloud
│   ├── api_client
│   └── command_socket
├── security
│   └── secret_store
└── telemetry
```

---

# 12. DAHUA EVENT STREAM IMPLEMENTASYONU

Python async client kullanılmalıdır.

Öneri:

```text
httpx.AsyncClient
DigestAuth
stream=True
```

Event parser şu alanları çıkarabilmelidir:

```text
Code
Action
Index
Data
```

Normalize örneği:

```json
{
  "event_type": "person_detected",
  "vendor": "dahua",
  "vendor_event_code": "SmartMotionHuman",
  "vendor_action": "Start",
  "vendor_channel_index": 0,
  "logical_channel": 1,
  "occurred_at": "2026-09-12T22:14:32+01:00",
  "raw_data": {}
}
```

Action handling:

```text
Start  -> event opened
Pulse  -> instantaneous event
Stop   -> event closed
```

Firmware'lerde action isimleri değişebilir; unknown action loglanıp drop edilmemelidir.

---

# 13. EVENT DEDUPLICATION

Bir kişi 10 saniye boyunca kamerada kaldığında onlarca duplicate alarm üretmemeliyiz.

Dedup key:

```text
tenant_id + site_id + device_id + channel_id + normalized_event_type
```

Default dedup window:

```text
15 saniye
```

Kural bazlı değiştirilebilir.

Event lifecycle:

```text
NEW
VERIFYING
VERIFIED
ALERTED
ACKNOWLEDGED
RESOLVED
FALSE_POSITIVE
```

---

# 14. AI PIPELINE

## 14.1 Event-driven pipeline

```text
Dahua SmartMotionHuman
        ↓
Edge snapshot
        ↓
Cloud event ingest
        ↓
AI queue
        ↓
Person detector
        ↓
Zone test
        ↓
Schedule test
        ↓
Risk score
        ↓
Alert / ignore / log-only
```

## 14.2 Continuous AI gerektiren özellikler

Aşağıdaki özellikler sadece XVR SMD event'i ile tam güvenilir çalışmaz:

- fire/smoke,
- PPE,
- uzun süre bekleme (loitering),
- crowd,
- bazı operasyonel sayımlar.

Bu yüzden per-camera AI mode olmalı:

```text
EVENT_DRIVEN
CONTINUOUS_LOW_FPS
CONTINUOUS_REALTIME
DISABLED
```

Örnek:

```text
Depo kapısı       -> EVENT_DRIVEN
Yakıt alanı       -> CONTINUOUS_LOW_FPS
PPE giriş kapısı  -> CONTINUOUS_REALTIME
Ofis içi          -> DISABLED
```

---

# 15. AI MODEL KATMANI

Model ismi business logic'e gömülmeyecek.

Interface:

```python
class DetectorProvider(Protocol):
    async def detect(self, image: bytes, task: str) -> DetectionResult:
        ...
```

Provider örnekleri:

```text
OnnxDetectorProvider
TensorRTDetectorProvider
RemoteGpuDetectorProvider
```

Commercial SaaS lisans uyumluluğu kontrol edilmeden AGPL veya kısıtlı model dependency'si production'a alınmamalıdır.

---

# 16. AI MODÜLLERİ — V1

## Security AI

- Person Detection
- Vehicle Detection
- Restricted Zone Intrusion
- Line Crossing (AI side fallback)
- After-hours Detection
- Loitering
- Crowd Detection

## Safety AI

- Helmet Detection
- Reflective Vest Detection
- Fire Detection
- Smoke Detection

## Camera Integrity AI

- Camera moved
- Camera covered
- Severe blur/defocus
- Scene obstruction
- Abnormal darkness/brightness

## Operational

- Vehicle arrival/departure
- Truck count
- People count (selected zones)

Not: Her modül per-camera enable/disable edilebilir.

---

# 17. ZONE ENGINE

Her kamerada bir veya daha fazla polygon zone oluşturulabilir.

Zone types:

```text
NORMAL
RESTRICTED
IGNORE
ENTRY
EXIT
PPE_REQUIRED
FIRE_RISK
VEHICLE_ONLY
```

Koordinatlar normalized olmalı:

```text
0.0 - 1.0
```

Örnek:

```json
{
  "name": "Warehouse Restricted",
  "type": "RESTRICTED",
  "points": [
    [0.51, 0.22],
    [0.91, 0.20],
    [0.94, 0.88],
    [0.49, 0.90]
  ]
}
```

---

# 18. RISK ENGINE

Default örnek puanlar:

```text
person_detected                 +10
outside_business_hours          +20
restricted_zone                 +30
loitering_over_threshold        +20
repeated_entry                  +10
camera_tamper_near_event        +20
vehicle_in_restricted_zone      +20
```

Severity:

```text
0–29   INFO
30–49  LOW
50–69  MEDIUM
70–84  HIGH
85–100 CRITICAL
```

Hard-coded olmamalı. Rule config üzerinden düzenlenebilir.

---

# 19. RULE ENGINE

Kural örneği:

```json
{
  "name": "Gece depo insan alarmı",
  "sites": ["site_uuid"],
  "cameras": ["camera_uuid_1", "camera_uuid_2"],
  "schedule": {
    "timezone": "Africa/Algiers",
    "days": [0,1,2,3,4,5,6],
    "start": "19:00",
    "end": "06:00"
  },
  "event_types": ["person_detected"],
  "min_ai_confidence": 0.80,
  "zone_types": ["RESTRICTED"],
  "actions": [
    "push",
    "snapshot",
    "clip",
    "escalate"
  ]
}
```

Midnight-crossing schedules doğru desteklenmelidir.

---

# 20. PUSH NOTIFICATION

Android:

```text
Firebase Cloud Messaging (FCM)
```

iOS:

```text
Apple Push Notification Service (APNs)
```

Notification payload minimum:

```json
{
  "type": "security_alert",
  "event_id": "uuid",
  "severity": "CRITICAL",
  "site_name": "Şantiye A",
  "camera_name": "Depo Arka",
  "title": "İnsan algılandı",
  "body": "Yasak bölgede kişi tespit edildi",
  "occurred_at": "2026-09-12T22:14:32Z"
}
```

Push içinde XVR credential veya RTSP URL bulunmamalıdır.

---

# 21. ALARM ESCALATION

Örnek:

```text
T+0 sec   -> Guard
T+30 sec  -> Supervisor
T+60 sec  -> Security Manager
T+120 sec -> Company Owner / Emergency Contact
```

Eğer biri ACK yaparsa sonraki escalation durur.

Audit log:

```text
sent_at
received/opened_at (mümkünse)
acknowledged_at
acknowledged_by
resolved_at
resolved_by
resolution_code
```

---

# 22. ALARM RESOLUTION CODES

```text
TRUE_SECURITY_INCIDENT
SUSPICIOUS_ACTIVITY
AUTHORIZED_PERSON
FALSE_POSITIVE
ANIMAL
WEATHER
LIGHT_CHANGE
MAINTENANCE
CAMERA_ERROR
OTHER
```

Bu feedback AI tuning dataset'inin temelidir.

---

# 23. LIVE VIEW TASARIMI

Mobil app XVR'a doğrudan bağlanmayacak.

Akış:

```text
Mobile -> Cloud API: request live session
Cloud -> Edge command socket
Edge -> XVR RTSP pull
Edge -> secure outbound media publish
Cloud media gateway -> WebRTC/HLS
Mobile -> signed temporary playback URL
```

Önerilen media gateway:

```text
MediaMTX
```

On-demand session timeout:

```text
60 saniye idle
5 dakika max default
```

Kullanıcı uzatabilir.

Kalite:

```text
LOW  -> substream
HIGH -> main stream
AUTO -> network-aware
```

---

# 24. EVENT CLIP STORAGE

Event media:

```text
snapshot_original.jpg
snapshot_annotated.jpg
clip.mp4
thumbnail.jpg
```

Object key:

```text
/{tenant_id}/{site_id}/{yyyy}/{mm}/{dd}/{event_id}/...
```

Presigned URL kullanılmalı.

Public bucket YASAK.

---

# 25. RETENTION

Plan bazlı:

```text
Starter  -> 7 gün
Business -> 30 gün
Pro      -> 90 gün
Enterprise -> configurable
```

Event metadata daha uzun tutulabilir.

Media retention ve metadata retention ayrı config olmalı.

---

# 26. DEVICE HEALTH

İzlenecek durumlar:

- XVR online/offline
- camera channel online/video loss
- camera blind/tamper
- storage absent
- storage failure
- storage low space
- RTSP availability
- snapshot availability
- event stream connected/disconnected
- cloud-edge connectivity
- recording freshness

### Recording freshness

Mümkün olduğunda recent recording/file kontrolü yap.

Firmware bunu güvenilir şekilde vermiyorsa:

```text
status = UNKNOWN
```

Yanlış `RECORDING_OK` sonucu üretme.

---

# 27. CAMERA OFFLINE AGGREGATION

Aynı site interneti kesildiğinde 70 ayrı kamera alarmı gönderme.

Hierarchy:

```text
Site gateway offline
  -> suppress device/camera offline alerts

XVR offline
  -> suppress its channel offline alerts

Only camera video loss
  -> camera-level alert
```

---

# 28. TAMPER DETECTION

İki katman:

### Katman 1

Dahua:

```text
VideoBlind
VideoLoss
```

### Katman 2

AI scene comparison:

- perceptual embedding / similarity,
- black-frame percentage,
- blur score,
- camera angle scene shift.

Critical tamper:

```text
camera view changed + after-hours + recent human event
```

risk score yükseltilir.

---

# 29. SAAS MULTI-TENANCY

Hierarchy:

```text
Tenant
├── Sites
│   ├── Edge Gateways
│   ├── XVR Devices
│   │   └── Cameras
│   └── Rules
├── Users
├── Roles
├── Subscription
└── Audit Logs
```

Her business entity `tenant_id` taşımalıdır.

PostgreSQL Row-Level Security düşünülmelidir.

Backend service katmanında tenant filtering zorunludur.

---

# 30. RBAC

Roller:

```text
OWNER
ADMIN
SECURITY_MANAGER
SITE_MANAGER
SUPERVISOR
GUARD
VIEWER
BILLING_ADMIN
```

Permission örnekleri:

```text
sites.view
sites.manage
devices.view
devices.manage
cameras.live_view
cameras.configure
alarms.view
alarms.acknowledge
alarms.resolve
rules.manage
users.manage
billing.manage
analytics.view
```

---

# 31. AUTHENTICATION

V1:

- Email/password
- Email verification
- Forgot password
- Access JWT kısa ömürlü
- Refresh token rotation
- Device push token management
- Optional TOTP 2FA scaffold

Password hashing:

```text
Argon2id
```

---

# 32. EDGE AUTHENTICATION

Edge cloud'a ayrı machine identity ile bağlanır.

Onboarding:

1. Admin app'te site oluşturur.
2. `Generate Edge Enrollment Code`.
3. Edge CLI code'u kullanır.
4. Cloud tek kullanımlık code'u doğrular.
5. Edge device certificate/token alır.
6. Enrollment code invalid olur.

Token rotation desteklenmelidir.

---

# 33. XVR CREDENTIAL GÜVENLİĞİ

- Dahua için ayrı service account oluşturulması tercih edilir.
- `admin` hesabını production integration için kullanma.
- Credential edge üzerinde encrypted-at-rest.
- Cloud'a plaintext gönderme.
- Mobil app'e hiçbir zaman gönderme.
- Log sanitization zorunludur.

---

# 34. BACKEND STACK

```text
Python 3.12+
FastAPI
SQLAlchemy 2.x
Alembic
Pydantic 2
PostgreSQL 16+
Redis
RabbitMQ
Celery / Dramatiq / custom async worker (tek seçim yap)
S3 compatible storage
Docker
Docker Compose
Nginx/Caddy/Traefik ingress
```

V1 için backend modular monolith olmalıdır.

---

# 35. REPO YAPISI

```text
siteguard/
├── README.md
├── docs/
│   ├── architecture.md
│   ├── dahua-integration.md
│   ├── api.md
│   ├── threat-model.md
│   └── deployment.md
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── core/
│   │   │   ├── config.py
│   │   │   ├── security.py
│   │   │   ├── logging.py
│   │   │   └── tenancy.py
│   │   ├── modules/
│   │   │   ├── auth/
│   │   │   ├── tenants/
│   │   │   ├── users/
│   │   │   ├── sites/
│   │   │   ├── edges/
│   │   │   ├── devices/
│   │   │   ├── cameras/
│   │   │   ├── events/
│   │   │   ├── alarms/
│   │   │   ├── rules/
│   │   │   ├── media/
│   │   │   ├── notifications/
│   │   │   ├── ai/
│   │   │   ├── analytics/
│   │   │   └── billing/
│   │   ├── integrations/
│   │   │   ├── fcm/
│   │   │   ├── apns/
│   │   │   ├── object_storage/
│   │   │   └── billing_provider/
│   │   ├── workers/
│   │   └── db/
│   ├── tests/
│   ├── alembic/
│   └── Dockerfile
├── edge-agent/
│   ├── siteguard_edge/
│   │   ├── main.py
│   │   ├── config.py
│   │   ├── devices/
│   │   │   ├── base.py
│   │   │   └── dahua/
│   │   │       ├── adapter.py
│   │   │       ├── cgi_client.py
│   │   │       ├── event_parser.py
│   │   │       ├── snapshot.py
│   │   │       ├── rtsp.py
│   │   │       ├── playback.py
│   │   │       ├── health.py
│   │   │       └── netsdk.py
│   │   ├── cloud/
│   │   ├── media/
│   │   ├── queue/
│   │   ├── secrets/
│   │   └── telemetry/
│   ├── tests/
│   └── Dockerfile
├── ai-service/
│   ├── app/
│   │   ├── main.py
│   │   ├── providers/
│   │   ├── pipelines/
│   │   ├── tasks/
│   │   ├── zones/
│   │   └── risk/
│   ├── tests/
│   └── Dockerfile
├── mobile/
│   ├── lib/
│   │   ├── core/
│   │   ├── features/
│   │   │   ├── auth/
│   │   │   ├── dashboard/
│   │   │   ├── sites/
│   │   │   ├── cameras/
│   │   │   ├── alarms/
│   │   │   ├── live/
│   │   │   ├── rules/
│   │   │   ├── analytics/
│   │   │   ├── users/
│   │   │   └── billing/
│   │   └── main.dart
│   └── test/
├── infra/
│   ├── docker-compose.yml
│   ├── docker-compose.dev.yml
│   ├── nginx/
│   ├── mediamtx/
│   ├── postgres/
│   └── monitoring/
└── scripts/
    ├── bootstrap.sh
    ├── dahua_probe.py
    └── seed_demo.py
```

---

# 36. DATABASE TABLOLARI

Minimum tablolar:

```text
tenants
subscriptions
subscription_plans
users
user_tenants
roles
permissions
role_permissions
sites
edge_gateways
devices
device_capabilities
device_credentials_refs
cameras
camera_zones
camera_ai_profiles
rules
rule_targets
events
event_detections
alarms
alarm_deliveries
alarm_acknowledgements
alarm_resolutions
media_assets
push_devices
ai_jobs
ai_results
health_checks
device_health_state
camera_health_state
edge_commands
edge_command_results
audit_logs
usage_counters
```

---

# 37. ÖNEMLİ DB ALANLARI

## events

```text
id UUID PK
tenant_id UUID
site_id UUID
device_id UUID
camera_id UUID
event_type varchar
vendor varchar
vendor_event_code varchar
vendor_event_id varchar nullable
action varchar
occurred_at timestamptz
received_at timestamptz
raw_payload jsonb
dedup_key varchar
status varchar
risk_score integer
created_at timestamptz
```

## alarms

```text
id UUID PK
tenant_id
site_id
event_id
severity
status
rule_id
opened_at
acknowledged_at
acknowledged_by
resolved_at
resolved_by
resolution_code
resolution_note
```

## cameras

```text
id
tenant_id
site_id
device_id
name
logical_channel
vendor_channel_snapshot
vendor_channel_rtsp
vendor_channel_event_index
enabled
ai_mode
status
created_at
```

---

# 38. API ENDPOINTLERİ

## Auth

```text
POST /v1/auth/register
POST /v1/auth/login
POST /v1/auth/refresh
POST /v1/auth/logout
POST /v1/auth/forgot-password
POST /v1/auth/reset-password
GET  /v1/me
```

## Tenants

```text
GET  /v1/tenants/current
PATCH /v1/tenants/current
```

## Sites

```text
GET    /v1/sites
POST   /v1/sites
GET    /v1/sites/{id}
PATCH  /v1/sites/{id}
DELETE /v1/sites/{id}
```

## Edge

```text
POST /v1/edges/enrollment-codes
POST /v1/edges/enroll
GET  /v1/edges
GET  /v1/edges/{id}
POST /v1/edges/{id}/commands
```

## Devices

```text
GET  /v1/devices
POST /v1/devices
GET  /v1/devices/{id}
PATCH /v1/devices/{id}
POST /v1/devices/{id}/probe
POST /v1/devices/{id}/sync-channels
```

## Cameras

```text
GET   /v1/cameras
GET   /v1/cameras/{id}
PATCH /v1/cameras/{id}
POST  /v1/cameras/{id}/snapshot
POST  /v1/cameras/{id}/live-sessions
POST  /v1/cameras/{id}/zones
GET   /v1/cameras/{id}/health
```

## Events

```text
GET /v1/events
GET /v1/events/{id}
```

## Alarms

```text
GET  /v1/alarms
GET  /v1/alarms/{id}
POST /v1/alarms/{id}/acknowledge
POST /v1/alarms/{id}/resolve
```

## Rules

```text
GET    /v1/rules
POST   /v1/rules
GET    /v1/rules/{id}
PATCH  /v1/rules/{id}
DELETE /v1/rules/{id}
```

## Analytics

```text
GET /v1/analytics/overview
GET /v1/analytics/events
GET /v1/analytics/response-times
GET /v1/analytics/device-health
```

## Billing

```text
GET  /v1/billing/subscription
GET  /v1/billing/usage
POST /v1/billing/checkout
POST /v1/billing/portal
POST /v1/billing/webhook
```

---

# 39. EDGE -> CLOUD API

```text
POST /internal/edge/v1/heartbeat
POST /internal/edge/v1/events
POST /internal/edge/v1/media/presign
POST /internal/edge/v1/media/complete
POST /internal/edge/v1/device-health
GET  /internal/edge/v1/config
WS   /internal/edge/v1/commands
```

Machine auth zorunlu.

---

# 40. REALTIME APP

WebSocket/SSE channel:

```text
/v1/realtime
```

Event types:

```text
alarm.created
alarm.updated
event.created
device.status_changed
camera.status_changed
edge.status_changed
live_session.ready
```

Mobil app reconnect + exponential backoff desteklemeli.

---

# 41. MOBILE APP EKRANLARI

## Authentication

- Splash
- Login
- Register
- Forgot password
- Verify email

## Dashboard

Göster:

```text
Sites count
XVR online/total
Cameras online/total
Critical alarms
Today person detections
Today vehicle detections
Offline devices
Storage warnings
```

## Sites

- Site list
- Site detail
- Site status
- Site alarm summary

## Cameras

- Camera list
- Search/filter
- Online/offline
- Thumbnail
- AI mode
- Camera detail

## Camera detail

- latest snapshot
- live view
- event timeline
- health
- zones
- AI settings

## Alarm Center

Filters:

```text
severity
site
camera
event type
status
date
```

Alarm detail:

- snapshot
- annotated snapshot
- clip
- live
- AI confidence
- zone
- risk reasons
- ACK
- resolve
- call emergency/security contact

## Rules

- list
- create/edit
- schedule
- cameras/sites
- event types
- zones
- min confidence
- actions
- escalation

## Analytics

- alarms/day
- true vs false positives
- average response time
- busiest cameras
- offline duration
- camera reliability

## Team

- users
- roles
- invites

## Billing

- current plan
- camera usage
- AI usage
- storage usage
- invoices/checkout link

---

# 42. MOBİL TASARIM KURALI

- Dark mode security-console feel.
- Critical alarm visually dominant.
- 1-tap ACK.
- Live view max 2 taps away from push.
- Push deep-link directly to alarm.
- Slow mobile internet için thumbnail-first UX.
- Clip full download yerine adaptive/streaming playback.

---

# 43. ABONELİK / USAGE MODELİ

Plan sadece kullanıcı sayısına bağlı olmamalıdır.

Usage counters:

```text
active_cameras
active_sites
monthly_ai_events
continuous_ai_camera_hours
stored_media_gb
monthly_live_stream_minutes
team_members
```

Örnek planlar ürün konfigürasyonu olarak tutulmalı, kod içine gömülmemeli.

---

# 44. BILLING PROVIDER ABSTRACTION

```python
class BillingProvider(Protocol):
    create_checkout(...)
    create_portal(...)
    cancel_subscription(...)
    verify_webhook(...)
```

Provider ülkeye göre değişebilir.

V1 Stripe ile başlayabilir, fakat domain kodu Stripe'a bağımlı olmamalıdır.

---

# 45. OBSERVABILITY

Gerekli:

```text
structured JSON logs
request_id
correlation_id
tenant_id (güvenliyse)
edge_id
device_id
metrics
health endpoints
error tracking
```

Metrics:

```text
edge_connected_total
dahua_event_stream_connected
smd_events_total
snapshots_success_total
snapshots_failed_total
rtsp_session_errors_total
ai_job_latency_seconds
push_delivery_attempts_total
alarm_ack_latency_seconds
queue_depth
```

Prometheus/Grafana uyumlu olmalı.

---

# 46. SECURITY

## Network

- XVR LAN'da kalır.
- Public 80/443/554 port açılmaz.
- Edge outbound bağlantı kullanır.
- Cloud TLS only.

## Secrets

- `.env` git'e girmez.
- XVR secrets loglanmaz.
- Object storage private.
- Presigned URL kısa ömürlü.
- Refresh token hash DB'de tutulur.

## API

- Rate limit.
- Tenant isolation testleri.
- IDOR testleri.
- RBAC testleri.
- File upload content validation.
- Webhook signature verification.

## Audit

Audit edilecek:

```text
login
failed_login
user_invited
role_changed
device_added
device_credentials_changed
live_view_started
alarm_acknowledged
alarm_resolved
rule_changed
subscription_changed
```

---

# 47. PRIVACY / LEGAL DESIGN

V1 facial recognition YAPMAYACAK.

Person detection anonim object detection seviyesindedir.

Gelecekte yüz tanıma eklenirse ayrı legal/privacy review gerekir.

Retention müşteriye gösterilmeli.

Tenant kendi medyasının retention politikasını plan sınırları içinde yönetebilmelidir.

---

# 48. FAILURE SCENARIOS

## Edge internet yok

- Event local queue.
- Snapshot local encrypted spool.
- Sync on reconnect.
- Cloud site connectivity alarmı üretir.

## XVR offline

- XVR alarmı.
- Kanal alarmları suppress.

## CGI stream koptu

Reconnect:

```text
1s -> 2s -> 5s -> 10s -> 30s max
```

Jitter ekle.

## Snapshot başarısız

- event metadata yine gönder.
- 2 retry.
- media_missing=true.

## AI server down

- event `AI_PENDING`.
- alarm policy'ye göre Dahua SMD güveniyle fallback alarm gönderilebilir.
- AI gelince alarm güncellenebilir.

## Push provider down

- retry queue.
- app realtime varsa websocket event yine iletilir.

---

# 49. AI FALLBACK POLICY

Critical site'ta AI servisi down diye alarm kaybetmek kabul edilemez.

Rule option:

```text
if_ai_unavailable:
  ALERT_ON_VENDOR_EVENT
  QUEUE_ONLY
  DROP
```

Default security rule:

```text
ALERT_ON_VENDOR_EVENT
```

Push metni:

```text
"İnsan alarmı — AI doğrulama geçici olarak kullanılamıyor"
```

---

# 50. PROBE SCRIPT — İLK GERÇEK XVR TESTİ

`scripts/dahua_probe.py` aşağıdaki adımları yapmalı:

1. IP/port erişim testi.
2. Digest auth login testi.
3. model/firmware bilgisi.
4. event caps.
5. exposure events.
6. snapshot channel 1..8 testi.
7. RTSP substream 1..8 ffprobe testi.
8. RTSP main stream opsiyonel testi.
9. event stream 60 saniye dinleme.
10. kullanıcıya ekranda SMD trigger testi talimatı.
11. gelen event kodlarını listeleme.
12. `SmartMotionHuman` geldi mi raporu.
13. `SmartMotionVehicle` geldi mi raporu.
14. channel index mapping raporu.
15. playback test.
16. sonuçları JSON dosyasına yazma.

Örnek sonuç:

```json
{
  "model": "XVR1B08H-I",
  "firmware": "...",
  "cgi": true,
  "snapshot": true,
  "rtsp": true,
  "rtsp_playback": true,
  "event_stream": true,
  "smart_motion_human": true,
  "smart_motion_vehicle": true,
  "event_index_base": 0,
  "snapshot_channel_base": 1,
  "rtsp_channel_base": 1
}
```

---

# 51. MANUEL CURL TESTLERİ

> Aşağıdaki komutlarda gerçek credential'ı shell history'ye bırakmamaya dikkat et. Pilot/test içindir.

## Event stream

```bash
curl --digest -u 'USER:PASS' \
  'http://XVR_IP/cgi-bin/eventManager.cgi?action=attach&codes=[All]&heartbeat=5'
```

## Sadece SMD

```bash
curl --digest -u 'USER:PASS' \
  'http://XVR_IP/cgi-bin/eventManager.cgi?action=attach&codes=[SmartMotionHuman%2CSmartMotionVehicle]&heartbeat=5'
```

## Snapshot

```bash
curl --digest -u 'USER:PASS' \
  'http://XVR_IP/cgi-bin/snapshot.cgi?channel=1' \
  --output snapshot.jpg
```

## Exposure events

```bash
curl --digest -u 'USER:PASS' \
  'http://XVR_IP/cgi-bin/eventManager.cgi?action=getExposureEvents'
```

## Caps

```bash
curl --digest -u 'USER:PASS' \
  'http://XVR_IP/cgi-bin/eventManager.cgi?action=getCaps'
```

## RTSP test

```bash
ffprobe -rtsp_transport tcp \
  'rtsp://USER:PASS@XVR_IP:554/cam/realmonitor?channel=1&subtype=1'
```

## Playback test

```bash
ffprobe -rtsp_transport tcp \
  'rtsp://USER:PASS@XVR_IP:554/cam/playback?channel=1&starttime=2026_09_12_22_00_00&endtime=2026_09_12_22_00_30'
```

---

# 52. DEVELOPMENT PHASES — CLAUDE CODE SIRASI

## PHASE 0 — Repository bootstrap

- monorepo oluştur.
- Docker Compose.
- PostgreSQL.
- Redis.
- RabbitMQ.
- MinIO.
- Backend skeleton.
- Edge skeleton.
- AI service skeleton.
- Flutter skeleton.
- lint/test/pre-commit.

**Exit criteria:** tüm servisler `docker compose up` ile ayağa kalkıyor.

---

## PHASE 1 — Dahua Hardware Probe

İlk kodlanacak gerçek fonksiyon budur.

- Digest CGI client.
- event listener.
- parser.
- snapshot.
- RTSP probe.
- playback probe.
- capability report.

**Exit criteria:** gerçek XVR1B08H-I'da human event terminalde görülüyor ve doğru kanaldan snapshot alınıyor.

---

## PHASE 2 — Edge enrollment + cloud heartbeat

- enrollment code.
- edge identity.
- heartbeat.
- config pull.
- command websocket.

**Exit criteria:** cloud dashboard edge online gösteriyor.

---

## PHASE 3 — Device onboarding

- site create.
- XVR add.
- edge receives encrypted config.
- capabilities.
- channel sync.
- camera naming.

**Exit criteria:** 1 XVR + 5 kamera cloud DB'de görünür.

---

## PHASE 4 — Event ingestion

- SMD event Edge -> Cloud.
- dedup.
- event persistence.
- snapshot upload.
- event realtime notification.

**Exit criteria:** kameranın önünden kişi geçince app event listesinde snapshot ile görünür.

---

## PHASE 5 — AI verification

- AI queue.
- person detector provider.
- vehicle detector provider.
- bounding boxes.
- confidence.
- annotated snapshot.

**Exit criteria:** SMD event'i ikinci AI tarafından doğrulanır.

---

## PHASE 6 — Rules + zones + risk

- polygon editor mobile.
- zone engine.
- schedule.
- confidence.
- risk scoring.

**Exit criteria:** yasak alan geceleri insan alarmı üretiyor; normal alan üretmiyor.

---

## PHASE 7 — Push + alarm workflows

- FCM.
- APNs.
- deep link.
- ACK.
- resolution.
- escalation.

**Exit criteria:** event -> push -> alarm detail -> ACK zinciri çalışıyor.

---

## PHASE 8 — Event clips

- playback URL.
- T-10/T+20.
- MP4 normalize.
- upload.
- app playback.

**Exit criteria:** alarm ekranında 30 sn event klibi oynuyor.

---

## PHASE 9 — Live streaming

- live session API.
- edge RTSP pull.
- MediaMTX.
- secure publish.
- mobile WebRTC/HLS.

**Exit criteria:** internet üzerinden CGNAT arkasındaki XVR kanalına app'ten canlı bağlanılıyor, public XVR portu yok.

---

## PHASE 10 — Health monitoring

- edge offline.
- XVR offline.
- VideoLoss.
- VideoBlind.
- storage events.
- recording freshness.
- alert suppression hierarchy.

**Exit criteria:** gerçek kesinti testleri doğru tek alarm üretiyor.

---

## PHASE 11 — Advanced AI

- PPE.
- fire/smoke.
- loitering.
- crowd.
- scene tamper.
- continuous AI modes.

**Exit criteria:** özellikler per-camera profile üzerinden açılıp kapanıyor.

---

## PHASE 12 — Multi-tenant + RBAC hardening

- tenant RLS.
- role permissions.
- cross-tenant security tests.

**Exit criteria:** tenant A'nın tenant B verisine erişimi tüm endpointlerde 100% engelli.

---

## PHASE 13 — Billing

- plans.
- entitlements.
- usage counters.
- provider integration.
- webhook.

**Exit criteria:** plan limitleri gerçek fonksiyonları etkiliyor.

---

## PHASE 14 — Analytics

- alarms/day.
- response time.
- true/false positives.
- camera downtime.
- AI accuracy feedback.

---

## PHASE 15 — Production hardening

- rate limit.
- security headers.
- backup.
- disaster recovery.
- log retention.
- metrics.
- alerting.
- load test.
- mobile release pipeline.

---

# 53. TEST STRATEJİSİ

## Unit

- Dahua event parser.
- channel mapping.
- dedup.
- schedule evaluator.
- zone geometry.
- risk score.
- RBAC.

## Integration

- Edge -> Backend.
- Backend -> RabbitMQ.
- AI -> Backend.
- media upload.
- push abstraction.

## Hardware-in-loop

Gerçek XVR:

- SMD human.
- SMD vehicle.
- VideoLoss.
- VideoBlind.
- XVR power off.
- LAN unplug.
- internet off.
- storage warning mümkünse.

## Mobile E2E

- login.
- alarm push.
- deep link.
- ACK.
- video playback.
- live stream.

---

# 54. PILOT BAŞARI METRİKLERİ

1 haftalık gerçek şantiye testinde ölç:

```text
human events received
human events confirmed
false positive count
missed incidents
average SMD -> cloud latency
average SMD -> push latency
snapshot success %
clip success %
edge uptime
XVR event stream uptime
AI inference p50/p95 latency
bandwidth per event
monthly projected bandwidth
GPU cost per 1000 events
```

Hedef ilk değerler:

```text
SMD -> event ingest p95     < 3 sec
SMD -> push p95             < 8 sec
snapshot success            > 98%
edge/cloud recovery         automatic
cross-tenant leak           0
critical event loss         0 (when local XVR/edge operating)
```

---

# 55. BANDWIDTH STRATEJİSİ

Yapılmayacak:

```text
70 x main stream -> cloud 24/7
```

Yapılacak:

```text
Event metadata -> çok küçük
Snapshot -> event anında
Clip -> yalnızca rule/plan gerektirirse
Live -> kullanıcı istediğinde
Continuous AI -> yalnızca seçilmiş kameralarda
```

---

# 56. MEDYA TRANSCODING

Mümkünse passthrough/remux yap.

H.264/H.265 uyumluluk durumuna göre:

- mobile decode destekliyorsa transcode etme.
- browser/WebRTC codec ihtiyacı varsa yalnızca live session sırasında transcode et.

CPU/GPU tasarrufu için `-c copy` tercih edilebildiği yerde kullan.

---

# 57. TIMEZONE

Tüm DB timestamp:

```text
UTC timestamptz
```

Her site:

```text
timezone = IANA string
```

Örnek:

```text
Africa/Algiers
Europe/Istanbul
```

Rule schedule site timezone'a göre değerlendirilir.

---

# 58. EVENT ID EMPOTENCY

Edge retry yaptığında duplicate row oluşmamalı.

Edge event UUID üretir.

Cloud:

```text
UNIQUE(edge_event_id)
```

Upload retry idempotent olmalıdır.

---

# 59. OFFLINE QUEUE

SQLite tabloları:

```text
outbox_events
outbox_media
pending_commands
local_health_history
```

State:

```text
PENDING
UPLOADING
DONE
FAILED_RETRYABLE
FAILED_PERMANENT
```

Exponential retry.

Disk quota koy:

```text
max_spool_gb
```

Quota yaklaşınca önce low-priority media temizlenebilir; event metadata asla ilk silinen veri olmamalıdır.

---

# 60. CONFIG SYNC

Cloud config versioned olmalı.

```text
config_revision
```

Edge son revision'ı bildirir.

Cloud değişiklik olduğunda command socket ile:

```text
CONFIG_UPDATED
```

gönderir.

Edge atomically yeni config'e geçer.

---

# 61. FIRMWARE COMPATIBILITY MATRIX

DB'de firmware-level compatibility tut:

```text
model
firmware_version
cgi_events
smd_human
smd_vehicle
snapshot
rtsp_live
rtsp_playback
known_issues
verified_at
```

Yeni müşteri cihazı eklenince bilinen profile eşleşirse hızlı onboarding yapılır.

---

# 62. DAHUA SPECIFIC KNOWN RISKS

1. Channel numbering endpointler arasında farklı olabilir.
2. Firmware'e göre CGI endpoint davranışları değişebilir.
3. SMD enable değilse `SmartMotionHuman` gelmez.
4. IP channel extension mode recorder-side SMD'yi etkileyebilir.
5. RTSP stream codec H.265 ise bazı mobile/browser yollarında transcode gerekebilir.
6. Çok fazla eşzamanlı RTSP connection XVR limitlerine yaklaşabilir.
7. Digest auth davranışı firmware'e göre değişebilir.
8. Event stream kopabilir; reconnect şart.
9. Clock yanlışsa event time yanlış olur; NTP kontrol edilmeli.
10. XVR kayıt planı event klibi için kritik; recording schedule kontrol listesine eklenmeli.

---

# 63. ONBOARDING CHECKLIST

Her XVR için:

```text
[ ] Firmware kaydedildi
[ ] Tarih/saat doğru
[ ] NTP ayarlı
[ ] Service user oluşturuldu
[ ] SMD enabled
[ ] Human enabled
[ ] Vehicle policy belirlendi
[ ] Kamera isimleri verildi
[ ] Snapshot channel map doğrulandı
[ ] RTSP channel map doğrulandı
[ ] Human event testi yapıldı
[ ] Playback testi yapıldı
[ ] VideoLoss testi yapıldı
[ ] Edge erişimi doğrulandı
[ ] Public port forward kapalı
```

---

# 64. APP ALARM ÖRNEĞİ

```text
🚨 KRİTİK GÜVENLİK ALARMI

Şantiye: Beton Tesisi
Kamera: Depo Arka Giriş
Saat: 02:14:32
Olay: İnsan
AI Güveni: %96
Bölge: Yasak Alan
Risk: 91/100

[CANLI İZLE]
[30 SN KLİP]
[ALAMI ONAYLA]
[GÜVENLİĞİ ARA]
```

---

# 65. DASHBOARD ÖRNEĞİ

```text
14 / 14 XVR online
68 / 70 camera online
4 critical alarms today
38 person detections
21 vehicle detections
2 camera offline
1 storage warning
```

---

# 66. V1 OUT OF SCOPE

Aşağıdakiler çekirdek V1 bitmeden yapılmayacak:

- facial recognition,
- license plate recognition (ANPR) unless separate validated module,
- fully autonomous police/emergency service dispatch,
- direct XVR internet exposure,
- 24/7 all-camera cloud recording replacement,
- replacing XVR local recording infrastructure.

---

# 67. PRODUCTION DEFINITION OF DONE

V1 tamamlandı sayılabilmesi için:

- 14 XVR'ın tamamı eklenebilmeli.
- ~70 kamera görülebilmeli.
- Human SMD event cloud'a ulaşmalı.
- AI ikinci doğrulama çalışmalı.
- Mobile push gelmeli.
- Push alarm detayına deep-link etmeli.
- Snapshot görünmeli.
- Event clip oynatılmalı.
- Live view CGNAT altında çalışmalı.
- Zones çalışmalı.
- Schedules çalışmalı.
- Risk score çalışmalı.
- ACK/escalation çalışmalı.
- Camera/XVR offline health çalışmalı.
- Storage anomaly event'leri çalışmalı.
- Tamper detection çalışmalı.
- Offline edge queue çalışmalı.
- Multi-tenant isolation testleri geçmeli.
- RBAC çalışmalı.
- Subscription entitlements çalışmalı.
- Android build alınmalı.
- iOS build alınmalı.
- Backup/restore test edilmiş olmalı.
- Monitoring dashboard hazır olmalı.

---

# 68. ENVIRONMENT VARIABLES — ÖRNEK

```text
APP_ENV=
DATABASE_URL=
REDIS_URL=
RABBITMQ_URL=
JWT_SECRET=
JWT_ACCESS_TTL_MINUTES=
JWT_REFRESH_TTL_DAYS=
S3_ENDPOINT=
S3_BUCKET=
S3_ACCESS_KEY=
S3_SECRET_KEY=
FCM_PROJECT_ID=
APNS_TEAM_ID=
APNS_KEY_ID=
APNS_BUNDLE_ID=
MEDIA_GATEWAY_URL=
AI_SERVICE_URL=
BILLING_PROVIDER=
BILLING_SECRET=
```

Edge:

```text
EDGE_ID=
EDGE_DATA_DIR=
EDGE_CLOUD_URL=
EDGE_ENROLLMENT_TOKEN=
EDGE_MAX_SPOOL_GB=
EDGE_LOG_LEVEL=
```

XVR credential env'de plain-text tutulmamalı; secret store kullan.

---

# 69. CLAUDE CODE İLK PROMPT

Claude Code'a bu markdown verildikten sonra ilk görev şu olmalı:

```text
Read this entire specification before writing code.
Do not implement the mobile UI first.
Start with PHASE 0 and PHASE 1 only.
Create the monorepo and a working Dahua XVR probe utility.
The probe utility must support HTTP Digest Auth, eventManager.cgi streaming,
snapshot.cgi, RTSP live probe, RTSP playback probe, channel-base detection,
and a JSON capability report.
Add tests using recorded/synthetic Dahua multipart event streams.
Do not fake successful device capabilities.
Stop after PHASE 1 and show me the exact real-device test commands and results expected.
```

---

# 70. ARAŞTIRMA KAYNAKLARI

## Resmi / birincil kaynaklar

1. Dahua XVR1B08H-I datasheet — CGI conformant, RTSP, 8-channel SMD Plus, storage/anomaly info:
   - https://material.dahuasecurity.com/uploads/cpq/prm-os-srv-res/smart/datasheetzipfiles/XVR1B08H-I_datasheet_20220620.pdf

2. Dahua XVR1B08H-I product information / regional page:
   - https://www.dahuasecurity.com/mx/products/All-Products/Discontinued-Products/HDCVI-Recorders/XVR1B08H-I

3. Dahua RTSP URL guidance:
   - https://dahuatech.zendesk.com/hc/en-gb/articles/16320900884754-How-to-add-IP-cameras-to-NVR-via-RTSP

4. Dahua Wiki RTSP example:
   - https://dahuawiki.com/images/cache/0/0d/Remote_Access%252FEmbed_Video_Feed_On_Website.html

## HTTP API dokümanları / teknik referanslar

5. Dahua HTTP API v2.63 mirror — snapshot, RTSP, playback, eventManager attach, Digest Auth:
   - https://pdfcoffee.com/dahua-http-api-v263-2-pdf-free.html

6. Dahua HTTP API v2.76 mirror:
   - https://pdfcoffee.com/dahua-http-api-v276-for-ipcamtalk-com-pdf-free.html

## Gerçek dünya entegrasyon referansları

7. rroller/dahua Home Assistant integration — CGI eventManager, SmartMotionHuman/Vehicle ve RTSP patterns:
   - https://github.com/rroller/dahua
   - https://github.com/rroller/dahua/blob/main/custom_components/dahua/client.py

8. OpenHAB issue — raw Dahua SMD event örnekleri (`SmartMotionHuman`) ve data payload:
   - https://github.com/openhab/openhab-addons/issues/11391

9. Dahua SDK örnek reposu — login, listen, snapshot, real play, download API fonksiyonları:
   - https://github.com/452/dahua-sdk

---

# 71. SON TEKNİK KARAR ÖZETİ

**SMD event:**

```text
CGI eventManager.cgi attach + Digest Auth
```

**Human code:**

```text
SmartMotionHuman
```

**Vehicle code:**

```text
SmartMotionVehicle
```

**Snapshot:**

```text
/cgi-bin/snapshot.cgi?channel=N
```

**Live video:**

```text
/cam/realmonitor?channel=N&subtype=0/1
```

**Event clip primary:**

```text
/cam/playback?channel=N&starttime=...&endtime=...
```

**Fallback:**

```text
Dahua NetSDK
```

**Architecture:**

```text
XVR -> Edge Agent -> Cloud -> AI -> Rule/Risk -> Push -> Flutter App
```

**CGNAT çözümü:**

```text
Outbound-only Edge connection; no public XVR port forwarding.
```

**70 kamera stratejisi:**

```text
Event-driven cloud AI + on-demand media; 24/7 70-stream cloud upload yok.
```

Bu kararlar V1'in temel mimarisidir. Claude Code bunları değiştirmeden önce açıkça teknik gerekçe göstermeli ve kullanıcı onayı istemelidir.
