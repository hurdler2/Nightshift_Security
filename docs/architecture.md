# Mimari

Kaynak: `NIGHTSHIFT_SPEC.md` (guncel) · eski surum: `docs/legacy-siteguard-v1-spec.md` §8–§11, §34, §35.

## Veri akışı

```
Analog kameralar
      │
Dahua XVR5108HS-I3          (LAN, public port yok)
      │
Nightshift Edge Agent      (yalnızca outbound bağlantı — CGNAT-safe)
      │  HTTPS / WSS
Cloud API (FastAPI modular monolith)
      ├── PostgreSQL   (kaynak veri, tenant_id her tabloda)
      ├── Redis        (cache, lock, kısa ömürlü durum)
      ├── RabbitMQ     (AI ve bildirim işleri)
      └── S3 / MinIO   (snapshot, klip — private bucket + presigned URL)
      │
AI Inference Service      (ikinci doğrulama katmanı)
      │
Rule / Risk Engine → Alert Engine → FCM / APNs
      │
Flutter mobil uygulama
```

## Neden bu şekilde

**Edge Agent zorunlu.** XVR'a doğrudan buluttan veya telefondan erişmek üç şeyi
gerektirirdi: public port forwarding, XVR credential'ının cihaz dışına çıkması ve
CGNAT'ın delinmesi. Üçü de spec tarafından yasaklanmış. Edge yalnızca dışarı bağlanır;
komutlar açık tutulan WSS kanalından içeri iner.

**Event-driven AI.** 70 kamerayı 7/24 buluta taşımak hem bant genişliği hem GPU
maliyetini savurur. XVR'ın kendi SMD'si birinci filtre, bizim AI'ımız ikinci doğrulama.
Yalnızca sürekli izleme gerektiren özellikler (PPE, yangın/duman, loitering, crowd)
seçili kameralarda `CONTINUOUS_*` moduna alınır.

**Modular monolith.** V1'de mikroservise bölmek, henüz sınırları netleşmemiş bir domaini
ağ çağrılarıyla dondurur. Modüller (`app/modules/*`) kendi router/service/model
katmanlarını taşır; ayrılmaları gerektiğinde sınır zaten orada.

**İki katmanlı tamper tespiti.** Dahua'nın `VideoBlind`/`VideoLoss` sinyalleri hızlı ama
kaba; AI tarafında sahne karşılaştırması (blur, siyah kare oranı, açı kayması) ikinci
katman. "Kamera görüntüsü değişti + mesai dışı + yakın zamanda insan olayı" kombinasyonu
risk skorunu yükseltir.

## Faz sahiplikleri

| Faz | Kapsam | Ana bileşen |
|-----|--------|-------------|
| 0 | Monorepo, compose, iskeletler | hepsi |
| 1 | Dahua probe | edge-agent |
| 2 | Edge enrollment, heartbeat, komut kanalı | edge + backend |
| 3 | Site/XVR/kamera onboarding | backend + edge |
| 4 | Olay akışı, dedup, snapshot upload | edge + backend |
| 5 | AI doğrulama | ai-service |
| 6 | Zone, schedule, risk | backend + ai-service |
| 7 | Push, alarm workflow, escalation | backend + mobile |
| 8 | Event klipleri | edge + backend |
| 9 | Canlı yayın | edge + mediamtx + mobile |
| 10 | Sağlık izleme, alarm bastırma hiyerarşisi | edge + backend |
| 11 | İleri AI modülleri | ai-service |
| 12 | Multi-tenant sertleştirme, RBAC | backend |
| 13 | Abonelik ve kullanım sayaçları | backend |
| 14 | Analitik | backend + mobile |
| 15 | Production hardening | infra |

## Değişmez kurallar

- Cihaz erişimi yalnızca Edge Agent üzerinden; cloud ve mobil hangi adapter'ın
  (CGI / NetSDK) kullanıldığını bilmez.
- XVR credential'ı edge'de şifreli durur; cloud'a ve mobile asla gitmez; loglanmaz.
- Her business tablosu `tenant_id` taşır; tenant kimliği yalnızca doğrulanmış token'dan
  gelir, istemciden değil.
- Tüm timestamp'ler UTC `timestamptz`; site'in IANA timezone'u kural değerlendirmesinde
  kullanılır.
- Doğrulanmamış hiçbir kabiliyet `true` raporlanmaz; `UNKNOWN` geçerli bir cevaptır.
