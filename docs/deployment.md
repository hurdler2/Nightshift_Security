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

## Edge gateway (saha kurulumu)

Minimum donanım (§10): x86-64 mini PC, 4 çekirdek, 8 GB RAM, 128 GB SSD, 1 Gbit
Ethernet, Ubuntu Server 24.04 LTS.

```bash
# 1. Bağımlılıklar
sudo apt update && sudo apt install -y python3.12-venv ffmpeg

# 2. Kurulum
sudo useradd --system --home /var/lib/nightshift-edge --create-home nightshift
sudo -u nightshift python3 -m venv /var/lib/nightshift-edge/.venv
sudo -u nightshift /var/lib/nightshift-edge/.venv/bin/pip install /opt/nightshift/edge-agent

# 3. Şifreleme anahtarı (çıktıyı systemd unit dosyasına koyun, diske düz yazmayın)
/var/lib/nightshift-edge/.venv/bin/nightshift-edge secrets generate-key

# 4. XVR credential bilgisini yerel şifreli depoya yaz (parola sorulur)
sudo -u nightshift EDGE_SECRET_KEY=... \
  /var/lib/nightshift-edge/.venv/bin/nightshift-edge secrets set xvr-01 nightshift
```

systemd unit (özet):

```ini
[Service]
User=nightshift
Environment=EDGE_CLOUD_URL=https://api.example.com
Environment=EDGE_DATA_DIR=/var/lib/nightshift-edge
EnvironmentFile=/etc/nightshift/edge.secret     # EDGE_SECRET_KEY, 0600, root:nightshift
ExecStart=/var/lib/nightshift-edge/.venv/bin/nightshift-edge run
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/nightshift-edge
```

Ağ kuralları:

- XVR yalnızca LAN üzerinde; router üzerinde XVR'a port yönlendirmesi **yok**.
- Gateway dışarı 443 (HTTPS/WSS) açar, içeri hiçbir port dinlemez.
- Opsiyonel WireGuard tüneli de outbound kurulur.
- Tailscale yalnızca pilot içindir; SaaS zorunlu bağımlılığı yapılmaz.

## Production kontrol listesi (PHASE 15)

- [ ] TLS terminasyonu (Caddy/Traefik/Nginx) ve HSTS
- [ ] `JWT_SECRET` en az 32 karakter, secret manager üzerinden
- [ ] PostgreSQL yedeği ve geri yükleme tatbikatı
- [ ] Object storage yaşam döngüsü kuralları (plan bazlı retention, §25)
- [ ] Prometheus + Grafana + alerting
- [ ] Rate limit ve güvenlik başlıkları
- [ ] Log retention ve PII incelemesi
- [ ] Yük testi (14 XVR / ~70 kamera profili)
- [ ] Mobil release pipeline (Android + iOS)
