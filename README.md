# Nightshift Security

Dahua XVR tabanlı, çoklu şantiye / çoklu müşteri (multi-tenant) güvenlik SaaS platformu.
Source of truth: [`NIGHTSHIFT_SPEC.md`](NIGHTSHIFT_SPEC.md).

```
Analog Cameras -> Dahua XVR5108HS-I3 -> Edge Agent -> Cloud API -> AI -> Rules/Risk -> Push -> Flutter App
```

## Durum

| Phase | Kapsam | Durum |
|-------|--------|-------|
| PHASE 0 | Monorepo, Docker Compose, servis iskeletleri, lint/test/pre-commit | ✅ Tamam |
| PHASE 1 | Dahua hardware probe (CGI digest, event stream, snapshot, RTSP, playback, capability report) | ✅ Tamam (gerçek cihaz doğrulaması bekliyor) |
| PHASE 2+ | Edge enrollment, event ingestion, AI, rules, push, live, health, billing | ⏳ Başlanmadı |

PHASE 1 çıktısı gerçek bir XVR5108HS-I3 üzerinde doğrulanmadan PHASE 2'ye geçilmez
(spec §52, §69).

## Repo yapısı

```
backend/      FastAPI modular monolith (cloud API)      — PHASE 0 iskelet
edge-agent/   asyncio Edge Agent + Dahua adapter        — PHASE 1 gerçek kod
ai-service/   AI inference servisi                      — PHASE 0 iskelet
mobile/       Flutter uygulaması                        — PHASE 0 iskelet
infra/        docker-compose, nginx, mediamtx, postgres, monitoring
scripts/      bootstrap.sh, dahua_probe.py, seed_demo.py
docs/         mimari, Dahua entegrasyonu, API, tehdit modeli, deployment
```

## Hızlı başlangıç (PHASE 0)

```bash
cp .env.example .env
docker compose -f infra/docker-compose.yml up -d
curl http://localhost:8000/health     # {"status":"ok",...}
curl http://localhost:8100/health     # {"status":"ok",...,"provider":"stub"}
docker compose -f infra/docker-compose.yml down   # durdur
```

Servisler: PostgreSQL 16, Redis 7, RabbitMQ 3 (management: :15672),
MinIO (:9000 / konsol :9001), MediaMTX (:8554/:8889/:8888, control API :9997),
backend (:8000), ai-service (:8100), edge-agent (`--profile edge`).
Tüm portlar `127.0.0.1` üzerine bağlıdır. MinIO imajları quay.io'dan çekilir
(Docker Hub kopyaları erişime kapalı).

## Gerçek XVR probe'u (PHASE 1)

Docker gerektirmez; tek başına çalışır.

```bash
cd edge-agent
python -m venv .venv && . .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python ../scripts/dahua_probe.py --host 192.168.1.108 --username nightshift --ask-password
```

Ayrıntılı kullanım, çıktı formatı ve manuel `curl` testleri:
[`docs/dahua-integration.md`](docs/dahua-integration.md).

## Geliştirme

```bash
make install        # backend + edge + ai geliştirme bağımlılıkları
make lint           # ruff + format kontrolü
make test           # pytest (tüm Python paketleri)
make up / make down # docker compose
pre-commit install  # commit öncesi lint/test kancaları
```

## Değişmez kurallar (spec §0, §46)

- XVR'a doğrudan mobil/cloud erişimi yok; her şey Edge Agent üzerinden.
- XVR credential'ları mobil uygulamaya veya cloud loglarına **asla** gitmez.
- XVR için public port forwarding yok; edge yalnızca outbound bağlanır (CGNAT-safe).
- Donanım kabiliyeti doğrulanmadan `true` raporlanmaz — bilinmiyorsa `UNKNOWN`.
- Her business tablosu `tenant_id` taşır.
