# Dahua XVR entegrasyonu (PHASE 1)

Bu doküman, `edge-agent/nightshift_edge/devices/dahua/` altındaki kodun ne yaptığını,
gerçek bir XVR5108HS-I3 üzerinde nasıl doğrulanacağını ve hangi sonucun "geçti" sayılacağını
anlatır. Kaynak karar dokümanı: `NIGHTSHIFT_SPEC.md` · cihaz detaylari icin eski surum `docs/legacy-siteguard-v1-spec.md` §1–§7, §50–§51, §62.

---

## 1. Modül haritası

| Dosya | Sorumluluk |
|-------|-----------|
| `cgi_client.py` | Digest auth'lu async HTTP istemcisi; hata sınıflandırması; **tek log redaksiyon noktası** |
| `event_parser.py` | `multipart/x-mixed-replace` akışının artımlı parse'ı + olay normalizasyonu |
| `event_stream.py` | `action=attach` bağlantısı, heartbeat watchdog, yeniden bağlanma merdiveni |
| `snapshot.py` | `snapshot.cgi`, JPEG doğrulama, kanal tabanı tespiti |
| `rtsp.py` | `cam/realmonitor` URL üretimi + ffprobe doğrulaması |
| `playback.py` | `cam/playback` URL'i, T-10/T+20 klip penceresi |
| `capabilities.py` | Cihaz kimliği, event caps, exposure events, saat/NTP kontrolü |
| `health.py` | Depolama ve kanal sağlığı (PHASE 10 için okuma tarafı) |
| `probe.py` | §50'deki 16 adımı yürüten orkestratör + rapor |
| `adapter.py` | `DahuaCgiAdapter`; mantıksal kanal ↔ cihaz kanalı sınırı |
| `netsdk.py` | Fallback yer tutucusu; sessizce çalışıyormuş gibi davranmaz |

---

## 2. Tasarımdaki üç kritik karar

**Content-Length ile anında yayınlama.** Dahua her parçadan *önce* boundary gönderir.
Parçayı bir sonraki boundary'yi bekleyerek kapatsaydık her alarm bir olay kadar gecikirdi.
Parser `Content-Length` varsa gövdeyi byte sayısı dolar dolmaz yayınlar
(test: `test_events_are_emitted_before_the_next_boundary_arrives`).

**Sessiz soket = kopuk bağlantı.** En sık görülen Dahua arızası TCP hatası değil,
olayların sessizce durmasıdır. `heartbeat=5` isteriz ve `3 × heartbeat` (min 15 sn)
boyunca hiç byte gelmezse bağlantıyı ölü sayıp yeniden kurarız.

**Kanal numarası asla sabit yazılmaz.** Snapshot kanalı dokümanda 1 tabanlı, sahada
0 tabanlı olabilir (§3.1). Probe önce 1'i, sonra 0'ı dener; ikisi de olmazsa sonuç
`None` kalır — tahmin üretilmez.

---

## 3. Ön koşullar (XVR tarafı)

Probe'u çalıştırmadan önce cihazda:

1. **SMD Plus açık** ve ilgili kanalda **Human** işaretli. Kapalıysa `SmartMotionHuman`
   hiç gelmez (§62.3) — bu bir yazılım hatası değildir.
2. **Vehicle** politikası belirlenmiş olmalı.
3. **Tarih/saat doğru, NTP açık.** Saat kayması hem olay zamanını hem playback
   penceresini bozar (§62.9). Probe 60 sn'den büyük kaymayı BLOCKER sayar.
4. **Ayrı servis hesabı** açılmış olmalı — `admin` ile entegrasyon yapılmaz (§33).
5. Kayıt planı (recording schedule) aktif olmalı; yoksa playback testi boş döner.
6. Edge gateway XVR ile aynı LAN'da; XVR'da **port forwarding yok** (§46).
7. **Yüz tespiti kapalı** — aşağıdaki bölüme bakın.

---

## 3a. Pilot cihaz: DH-XVR5108HS-I3/T

Spec XVR1B08H-I varsayımıyla yazıldı; pilot donanımı **DH-XVR5108HS-I3/T** oldu.
CGI API, RTSP URL biçimi, digest auth ve `eventManager.cgi` davranışı Dahua XVR/NVR
ailesinde ortak olduğu için entegrasyon kodunda değişiklik gerekmedi. Kodun hiçbir
yerinde model adı sabit değil; kabiliyetler cihazdan probe ile okunuyor.

Modelin getirdiği farklar:

**WizSense (-I3) daha fazla olay üretir.** SMD Plus'a ek olarak perimeter protection
(tripwire / intrusion) ve yüz tespiti recorder tarafında çalışabiliyor.
`CrossLineDetection` ve `CrossRegionDetection` zaten eşlenmiş durumda — açılırsa
PHASE 6'daki bölge işinin bir kısmını cihaz üstlenir. IVS olayları
(`WanderDetection` = loitering, `LeftDetection`, `TakenAwayDetection`,
`ParkingDetection`) da eşlendi, ancak **hiçbir kural bunlara probe doğrulaması
olmadan bağlanmamalı**.

**AI kanal bütçesi sınırlı.** SMD Plus tüm kanallarda çalışsa da perimeter
protection ve yüz tespiti yalnızca birkaç kanalda etkinleştirilebiliyor; kaç kanal
olduğu firmware'e göre değişir. Hangi kameraya hangi özelliğin verileceği bir
planlama kararıdır — probe raporundaki `caps_raw` ve `exposure_events` çıktısına
bakarak sabitleyin.

**Yüz olayları edge'de düşürülür.** V1 yüz işleme yapmaz (§47), ama cihaz yapabilir
ve `codes=[All]` ile dinlediğimiz için `FaceDetection` payload'ları bize ulaşırdı.
`event_parser.BLOCKED_EVENT_CODES` bu olayları gateway'de keser: cloud'a gitmez,
probe raporuna payload yazılmaz, yalnızca "cihazda yüz tespiti açık" uyarısı üretilir.
Doğru davranış bu özelliği cihaz üzerinde tamamen kapatmaktır; müşterinin yüz tespiti
için hukuki dayanağı varsa bu ayrı bir ürün ve sözleşme kararıdır.

**H.265 olasılığı daha yüksek.** 5 serisi varsayılan olarak daha agresif H.265
kullanır ve 5MP analog kamera destekler. Canlı yayında WebRTC için transcode
gerekebilir (§62.5, probe uyarı verir) ve snapshot boyutları büyür — olay başına
bant genişliği hesabını buna göre yapın.

**IP kanal genişletme uyarısı aynen geçerli** (§62.4): IP kanal modu recorder
tarafındaki AI kanallarını azaltabilir veya kapatabilir.

---

## 4. Probe'u çalıştırma

```bash
cd edge-agent
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

# Parola argüman olarak verilmez; sorulur (shell history / process list sızıntısı).
python ../scripts/dahua_probe.py \
  --host 192.168.1.108 \
  --username nightshift \
  --ask-password \
  --channels 8 \
  --listen 60 \
  --trigger-channel 1 \
  --print-commands
```

Yararlı bayraklar:

| Bayrak | Etki |
|--------|------|
| `--trigger-channel N` | Operatörün tetikleyeceği mantıksal kanal; event index eşlemesini kesinleştirir |
| `--main-stream` | Substream'e ek olarak main stream'i de dener (XVR bağlantı limitine dikkat, §62.6) |
| `--no-playback` | Playback testini atlar |
| `--codes '[SmartMotionHuman,SmartMotionVehicle]'` | Daraltılmış filtre (önce `[All]` ile doğrula, §2.1) |
| `--output rapor.json` | Rapor yolu (varsayılan `probe-reports/<host>-<zaman>.json`) |
| `DAHUA_PASSWORD` env | Etkileşimsiz çalıştırma (CI/saha scripti) |

`ffprobe` kurulu değilse RTSP ve playback sonuçları `UNKNOWN` kalır — `false` değil.
Kurulum: `apt install ffmpeg` / `winget install Gyan.FFmpeg`.

Çıkış kodu: **0** = blocker yok, **1** = en az bir blocker var.

---

## 5. Testin akışı

Probe 9. adımda 60 saniyelik dinlemeye geçer ve ekrana şunu yazar:

```
        >>> [10/16] TRIGGER SMD NOW <<<
```

O sırada:

1. Belirttiğiniz kanalın önünden **yürüyerek geçin** (insan).
2. Mümkünse bir **araç** hareket ettirin.
3. Ek olarak bir kameranın kablosunu çekip takın (VideoLoss) ve lensi kapatın (VideoBlind)
   — bunlar zorunlu değil ama PHASE 10 için erken sinyal verir.

Gelen her olay satır satır ekrana düşer:

```
        <- SmartMotionHuman action=Start index=0
```

---

## 6. Beklenen çıktı

```
====================================================================
 Nightshift Dahua probe — 192.168.1.108
====================================================================
 model            : XVR5108HS-I3
 firmware         : 4.004.0000001.0.R.250929
 serial           : 7L03CE2PAZ1B4F5
 channels         : 8

 digest auth      : SUPPORTED
 cgi              : SUPPORTED
 event stream     : SUPPORTED
 SmartMotionHuman : SUPPORTED
 SmartMotionVehicle: SUPPORTED
 snapshot         : SUPPORTED (base=1)
 rtsp live        : SUPPORTED (base=1)
 rtsp playback    : SUPPORTED
 event index base : 0

 observed event codes:
   SmartMotionHuman             x4
   VideoMotion                  x9
   Heartbeat                    x12

 verdict: PASS   (72.3s)
====================================================================
```

JSON raporu (`probe-reports/...json`) cihaz onboarding'inde
`device_capabilities` ve `device_channel_map` tablolarına yazılacak kaynaktır.

### PHASE 1 geçme ölçütü (spec §52)

- [ ] `digest_auth = SUPPORTED`
- [ ] `event_stream = SUPPORTED`
- [ ] `smart_motion_human = SUPPORTED` (gerçek insan tetiklemesiyle)
- [ ] `snapshot = SUPPORTED` ve doğru kanaldan görüntü geliyor
- [ ] `snapshot_channel_base` ve `event_index_base` belirlenmiş
- [ ] `errors` listesi boş

`smart_motion_vehicle` bu aşamada `UNKNOWN`/`UNSUPPORTED` olabilir (tetiklenmemiş olabilir);
rapor bunu warning olarak ayırt eder.

---

## 7. Manuel curl / ffprobe karşılıkları (spec §51)

> Gerçek parolayı shell history'de bırakmayın: `-u 'USER:'` yazıp parolayı sorduran
> biçimi tercih edin veya komuttan önce bir boşluk koyun.

```bash
# Tüm olaylar
curl --digest -u 'USER:PASS' \
  'http://XVR_IP/cgi-bin/eventManager.cgi?action=attach&codes=[All]&heartbeat=5'

# Sadece SMD
curl --digest -u 'USER:PASS' \
  'http://XVR_IP/cgi-bin/eventManager.cgi?action=attach&codes=[SmartMotionHuman%2CSmartMotionVehicle]&heartbeat=5'

# Snapshot
curl --digest -u 'USER:PASS' \
  'http://XVR_IP/cgi-bin/snapshot.cgi?channel=1' --output snapshot.jpg

# Exposure events / caps
curl --digest -u 'USER:PASS' 'http://XVR_IP/cgi-bin/eventManager.cgi?action=getExposureEvents'
curl --digest -u 'USER:PASS' 'http://XVR_IP/cgi-bin/eventManager.cgi?action=getCaps'

# Canlı RTSP
ffprobe -rtsp_transport tcp 'rtsp://USER:PASS@XVR_IP:554/cam/realmonitor?channel=1&subtype=1'

# Playback
ffprobe -rtsp_transport tcp \
  'rtsp://USER:PASS@XVR_IP:554/cam/playback?channel=1&starttime=2026_09_12_22_00_00&endtime=2026_09_12_22_00_30'
```

---

## 8. Sorun giderme

| Belirti | Olası neden | Yapılacak |
|---------|-------------|-----------|
| `digest auth rejected (401)` | Yanlış parola, ya da hesap kilitli | Cihaz web arayüzünden kilidi açın; servis hesabını doğrulayın |
| Akış açılıyor ama hiç olay yok | SMD kapalı veya kanalda Human seçili değil | XVR menüsünden SMD Plus + Human aktive edin (§62.3) |
| Sadece `VideoMotion` geliyor | Firmware SMD'yi VideoMotion içine gömüyor olabilir | `observed_events` içindeki `data` alanında `Object.ObjectType` arayın |
| Snapshot 400/boş | Kanal tabanı farklı | Probe zaten 1 ve 0'ı dener; ikisi de olmazsa kanal fiziksel olarak bağlı olmayabilir |
| RTSP `401` | RTSP için ayrı yetki gerekebilir | Servis hesabına "Live/Playback" yetkisi verin |
| RTSP codec `hevc` | H.265 | Live'da WebRTC için transcode gerekebilir (§62.5) — rapor uyarı verir |
| Playback boş | Kayıt planı kapalı ya da o dakikada kayıt yok | `--playback-offset` ile daha yakın bir zaman deneyin; kayıt planını kontrol edin |
| Olaylar 30-60 sn sonra kesiliyor | Heartbeat gelmiyor / cihaz bağlantıyı düşürüyor | Watchdog yeniden bağlanır; `dahua_event_stream_connected` metriğini izleyin |
| `no data for 15s` döngüsü | Ağ arasında bir cihaz idle bağlantıyı kesiyor | `--heartbeat 3` deneyin; switch/router idle timeout'una bakın |

---

## 9. Bilinen riskler (spec §62)

1. Kanal numaralandırması endpoint'ler arasında farklı olabilir → her taban ayrı ayrı saklanır.
2. Firmware'e göre CGI davranışı değişir → capability probe + `UNKNOWN` durumu.
3. SMD kapalıysa insan olayı gelmez → onboarding checklist maddesi.
4. IP kanal genişletme modu recorder-side SMD'yi devre dışı bırakabilir.
5. H.265 → live için transcode ihtiyacı.
6. Çok sayıda eşzamanlı RTSP bağlantısı XVR limitini zorlar → probe kanal testlerini
   en fazla 3 paralel yapar.
7. Digest davranışı firmware'e göre değişebilir.
8. Event stream kopar → yeniden bağlanma zorunlu (uygulandı).
9. Saat yanlışsa olay zamanı ve klip penceresi yanlış olur → probe blocker verir.
10. Kayıt planı klip için kritik → onboarding checklist maddesi.
