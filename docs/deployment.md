# Deployment

Kaynak: spec §10, §46, §68. PHASE 0 kapsamı: geliştirme yığını ve gateway kurulum
adımlarının sabitlenmesi.

## Geliştirme

```bash
cp .env.example .env
docker compose -f infra/docker-compose.yml up -d
docker compose -f infra/docker-compose.yml ps
curl http://localhost:8000/health
curl http://localhost:8100/health
```

Hot reload:

```bash
docker compose -f infra/docker-compose.yml -f infra/docker-compose.dev.yml up
```

Portlar (hepsi `127.0.0.1` üzerine bağlıdır, dışarı açık değildir):

| Servis | Port |
|--------|------|
| backend | 8000 |
| ai-service | 8100 |
| postgres | 5432 |
| redis | 6379 |
| rabbitmq / management | 5672 / 15672 |
| minio / konsol | 9000 / 9001 |
| mediamtx rtsp / webrtc / hls / api | 8554 / 8889 / 8888 / 9997 |

## Süreçler

V1'de sahada cihaz yok; her şey buluttadır (§3). Dört ayrı süreç çalışır ve ayrı
olmaları bilinçlidir — alarm postası seli, nöbetçinin alarm onaylamasını
yavaşlatmamalıdır.

| Süreç | Komut | Ne yapar |
|-------|-------|----------|
| API | `uvicorn app.main:app` | REST + WebSocket |
| Mail ingest | `python -m app.workers.mail_ingest` | Kaydedicilerin gönderdiği alarm postasını kabul eder |
| Watchdog | `python -m app.workers.watchdog` | Susan kaydediciyi fark eder (§13.3) |
| Escalation | `python -m app.workers.escalation` | Yanıtlanmayan alarmı yükseltir (§12) |

Hepsi aynı imajı kullanır:

```bash
docker compose -f infra/docker-compose.yml up -d \
  backend mail-ingest watchdog escalation
```

## Mail ingest — dışarıya bakan tek yüzey

Kaydediciler SMTP **istemcisidir**; biz onların sunucusuyuz. Cihaz kimliği mesajın
içinden değil `AUTH`'tan gelir, çünkü saha CGNAT arkasındadır ve gönderen IP hiçbir
şey ispat etmez (§7.5).

| Port | Kullanım |
|------|----------|
| 587 | Birincil, STARTTLS |
| 465 | Implicit TLS, 587 engelliyse |
| 2525 | Son çare, ikisi de engelliyse |
| ~~25~~ | **Asla.** Mobil operatörler engeller. |

Production'da TLS zorunludur; sertifika verilmezse süreç başlamayı reddeder:

```
SMTP_TLS_CERT_FILE=/etc/nightshift/tls/fullchain.pem
SMTP_TLS_KEY_FILE=/etc/nightshift/tls/privkey.pem
SMTP_PORT=587
```

DNS tarafı: ingest alan adının A kaydı bu sunucuya bakmalı. MX kaydına gerek yoktur —
cihazlar adrese değil, **verilen sunucu adına** bağlanır.

Kimlik bilgisi anlık görüntüsü dakikada bir tazelenir (`SMTP_ACCOUNT_REFRESH_SECONDS`).
Bunun sonucu açıkça söylenir: yeni açılan hesap bir tazeleme kadar geç çalışır,
pasifleştirilen hesap da bir tazeleme kadar geç kapanır.

## Push sağlayıcıları

Yoksa sistem çalışmaya devam eder ve bunu loglar — alarm satırları yazılır, açık
uygulamalar canlı kanaldan görür, yalnızca telefon çalmaz.

```
FCM_SERVICE_ACCOUNT_FILE=/etc/nightshift/fcm.json
APNS_KEY_FILE=/etc/nightshift/apns.p8
APNS_KEY_ID=...
APNS_TEAM_ID=...
APNS_TOPIC=com.nightshift.app
```

## Ağ kuralları

- Şantiyedeki DVR'a router üzerinden **port yönlendirmesi yok**. Tüm trafik
  cihazdan dışarı doğru kurulur.
- Bulut tarafında dışarı açık olanlar: 443 (API/WS) ve SMTP portu. Başka hiçbir şey.
- Canlı görüntü için içeri açılan port yoktur; V1'de canlı izleme DMSS'e
  devredilir (§17).

## Saha kurulumu

Cihaz tarafı adım adım: [commissioning-runbook.md](commissioning-runbook.md).

## Production kontrol listesi

- [ ] TLS terminasyonu (Caddy/Traefik/Nginx) ve HSTS
- [ ] `JWT_SECRET` en az 32 karakter, secret manager üzerinden
      (production'da kısa veya varsayılan secret ile süreç başlamaz)
- [ ] SMTP sertifikası kurulu; `SMTP_REQUIRE_TLS` açık
- [ ] FCM/APNs kimlik bilgileri yerinde — yoksa alarm telefona ulaşmaz
- [ ] PostgreSQL yedeği ve geri yükleme tatbikatı
- [ ] Object storage yaşam döngüsü kuralları (plan bazlı retention, §25)
- [ ] Prometheus + Grafana + alerting
- [ ] Rate limit ve güvenlik başlıkları
- [ ] Log retention ve PII incelemesi
- [ ] Yük testi (14 XVR / ~70 kamera profili)
- [ ] Mobil release pipeline (Android + iOS)
