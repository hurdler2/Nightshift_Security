# Tehdit modeli

Kaynak: spec §33, §46, §47. Bu doküman neyi kabul etmediğimizi yazar; her kontrolün
koda hangi fazda girdiği belirtilmiştir.

## Varlıklar

| Varlık | Neden değerli |
|--------|---------------|
| XVR servis hesabı credential | Kameralara ve tüm kayıtlara erişim |
| Canlı ve kayıtlı görüntü | Şantiye mahremiyeti, müşteri sözleşmesi |
| Olay ve alarm geçmişi | Güvenlik istihbaratı; müşteriler arası sızıntı kabul edilemez |
| Kullanıcı oturum token | Hesap devralma |
| Edge makine kimliği | Sahte olay üretimi, sahte cihaz kaydı |

## Senaryolar ve kontroller

| # | Senaryo | Kontrol | Faz |
|---|---------|---------|-----|
| 1 | İnternetten XVR erişimi | Port yönlendirme yok; edge yalnızca outbound; RTSP/CGI dışarı kapalı | 0 (tasarım) |
| 2 | Gateway diski çalınıyor | Credential Fernet ile şifreli; anahtar diskte değil, systemd/vault env ile geliyor | 1 |
| 3 | Credential loglara sızıyor | Tek redaksiyon noktası (`security/redaction.py`), logging filtresi, pre-commit tarayıcı | 1 |
| 4 | Mobil uygulama credential görüyor | Mobil hiç RTSP URL almaz; yalnızca kısa ömürlü imzalı oynatma URL | 9 |
| 5 | Tenant A, tenant B verisini okuyor | Servis katmanında zorunlu tenant filtresi, PostgreSQL RLS, her endpoint için cross-tenant test | 12 |
| 6 | IDOR ile başka kameranın görüntüsü | Kaynak sahipliği kontrolü (`assert_owns`) + RBAC izinleri | 12 |
| 7 | Medya URL paylaşılıp süresiz kullanılıyor | Private bucket, kısa TTL presigned URL | 4 |
| 8 | Sahte edge kaydı | Tek kullanımlık enrollment code, cihaz token, rotasyon | 2 |
| 9 | Sahte olay enjeksiyonu | Edge endpointleri makine kimliği ister; `edge_event_id` üzerinde idempotency | 4 |
| 10 | Login brute force | Rate limit, Argon2id, hesap kilitleme, başarısız denemenin audit kaydı | 15 |
| 11 | Refresh token çalınması | Rotasyon, DB'de yalnızca hash, yeniden kullanım tespiti | 2 |
| 12 | Webhook sahteciliği | Sağlayıcı imza doğrulaması | 13 |
| 13 | Zararlı dosya yükleme | İçerik tipi ve magic byte doğrulaması; JPEG/MP4 dışına izin yok | 4 |
| 14 | XVR hesabının kilitlenmesi | Auth hatasında yeniden bağlanma merdiveni; sıkı döngü yok | 1 |
| 15 | Site interneti kesilince 70 alarm | Hiyerarşik bastırma: site → XVR → kanal | 10 |

## Bilinçli kapsam dışı (V1)

- Yüz tanıma yok. İnsan tespiti anonim nesne tespiti seviyesindedir; yüz tanıma ayrı
  hukuki ve mahremiyet incelemesi gerektirir (§47).
- Plaka tanıma (ANPR) yok.
- Otonom polis veya acil servis çağrısı yok.

## Sırlar

- `.env` git'e girmez; `.env.example` şablondur.
- XVR credential env değişkeninde tutulmaz; edge secret store kullanılır.
- `EDGE_SECRET_KEY` yalnızca çalışma zamanında enjekte edilir.
- `scripts/check_no_secrets.py` pre-commit adımında credential kalıplarını tarar.
