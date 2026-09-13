# Nightshift Security — V1 Teknik Spesifikasyon

> **Amaç:** Şantiyeye **hiçbir ek donanım koymadan**, müşterinin elindeki Dahua DVR'ı
> kullanarak hırsızı olay anında yakalamak: insan algılandığı saniyede sahada sesli
> caydırıcı devreye girsin, telefona fotoğraflı alarm düşsün.
>
> **Pilot donanım:** Dahua **DH-XVR5108HS-I3/T**, 8 analog kanal, WizSense.
> Firmware: **4.004.0000001.0.R.250929** (29 Eylül 2025 release, cihazın en güncel sürümü).
>
> **Tarih:** 2026-09-13 · **Sürüm:** V1 (zero-device)
>
> Bu doküman **source of truth**'tur. Önceki sürüm (edge gateway mimarisi)
> `docs/legacy-siteguard-v1-spec.md` altında referans olarak durur.

---

# 0. UYGULAYICI İÇİN ANA TALİMAT

1. Bir özelliği sessizce atlama; atlanacaksa §17'ye yaz.
2. TODO gerekiyorsa `TODO(V1-BLOCKER)` veya `TODO(V1-NONBLOCKER)` etiketi kullan.
3. **Donanım davranışını uydurma.** Datasheet'te olmayan bir yetenek, cihaz üzerinde
   doğrulanana kadar `UNKNOWN`'dır. Asla `true` raporlama.
4. Şantiyeye ek cihaz gerektiren çözüm üretme. Tek istisna §18'deki opsiyonel tünel.
5. DVR'a internetten port açmayı gerektiren çözüm üretme. CGNAT altında çalışmak zorunlu.
6. DVR kullanıcı adı/parolası mobil uygulamaya veya cloud loglarına gitmez.
7. Çoklu müşteri izolasyonu (`tenant_id`) en baştan.
8. Yüz tanıma/tespiti V1'de **yok** — §19.3.
9. Python 3.12+, FastAPI, PostgreSQL, Flutter. Backend modular monolith.
10. API ve olay payload'ları OpenAPI/JSON Schema ile belgelenir.

---

# 1. NEDEN BU SÜRÜM FARKLI

Önceki mimari her şantiyeye bir **edge gateway mini PC** koyuyordu. O cihaz canlı
yayını, klip üretimini ve uzaktan konfigürasyonu mümkün kılıyordu; karşılığında her
saha için donanım maliyeti, kurulum, arıza ve bakım getiriyordu.

Bu sürümde saha donanımı **yalnızca DVR**. Kayıp özellikler §17'de açıkça listelendi.
Ana hedef — *hırsızı suçüstü yakalamak* — donanımsız da karşılanıyor, çünkü o hedefin
üç bileşeni var ve üçü de DVR'ın kendi yetenekleriyle çalışıyor:

1. **Doğru algılama** → DVR'ın SMD Plus'ı insan/araç ayrımını cihaz üzerinde yapıyor.
2. **Anında caydırma** → DVR alarm anında sahada sesli uyarı çalabiliyor (§6).
3. **Anında haber** → DVR alarm e-postasını fotoğrafla birlikte dışarı gönderebiliyor.

---

# 2. DOĞRULANMIŞ DONANIM GERÇEKLERİ

Kaynak: DH-XVR5108HS-I3 resmi datasheet (Rev 002.000, 2023-07-20). Aşağıdakiler
datasheet'ten **birebir doğrulanmıştır**; işaretsiz her şey cihaz üzerinde
doğrulanacaktır.

## 2.1 AI yetenekleri

| Özellik | Kanal sayısı |
|---------|--------------|
| SMD Plus (insan / motorlu araç ikincil filtreleme) | **8 kanal** (hepsi) |
| Perimeter Protection (tripwire, intrusion), kanal başına 10 IVS kuralı | **4 kanal** (General Model) |
| Face Detection | 2 kanal |
| Face Recognition | 2 kanal, 10 veritabanı / 10.000 yüz |

## 2.2 En kritik bulgu: AI Mode tekil bir seçimdir

Datasheet dipnotları:

> *SMD Plus takes effect when **SMD or IVS&SMD** is selected in AI Mode.*
> *Perimeter Protection takes effect when **IVS&SMD** is selected in AI Mode.*
> *Face Recognition takes effect when **Face** is selected in AI Mode.*

Yani cihazda tek bir AI Mode seçilir ve modlar birbirini dışlar.

**V1 kararı: `AI Mode = IVS&SMD`.** Bu seçimle 8 kanalda SMD Plus + 4 kanalda perimeter
protection elde ederiz ve **yüz tespiti donanım seviyesinde hiç çalışmaz**. Gizlilik
sorunu yazılımla değil, cihaz modu ile kökten çözülür (§19.3).

## 2.3 Alarm türleri ve linkage

```
General Alarm      : motion detection; video loss; video tampering
Anomaly Alarm      : no disk; disk error; disk full; offline; IP conflict; MAC conflict
Intelligent Alarm  : face detection; face recognition; perimeter protection
Alarm Linkage      : Record; snapshot (panoramic); IPC external alarm output;
                     voice prompt; buzzer; log; email
```

**Linkage listesinde FTP yok.** Bu modelin protokol listesinde de FTP yok. Dolayısıyla
**medya çıkışı için tek datasheet-doğrulamalı yol e-posta ekidir**. FTP/SFTP üzerine
tasarım yapılmayacak; cihaz arayüzünde varsa §24'te doğrulanacak.

## 2.4 Ağ protokolleri

```
HTTP; HTTPS; TCP/IP; IPv4; IPv6; RTSP; UDP; NTP; DHCP; DNS; SMTP; UPnP;
IP Filter; DDNS; Alarm Server; P2P; Auto Registration
Interoperability: ONVIF 22.12 (Profile T/S/G); CGI; SDK
```

`SMTP`, `Alarm Server`, `P2P` ve `Auto Registration` bu mimarinin dayanak noktaları.

**Sahada doğrulandı:** cihazın P2P menüsünde **DoLynk** aktif. Bu, DVR'ın Dahua
bulutuna outbound bağlanabildiğinin kanıtıdır ve iki sonucu vardır:

1. **DMSS ile canlı izleme çalışır** → §12'deki yönlendirme gerçekten uygulanabilir.
2. **DoLynk Care ile uzaktan konfigürasyon çalışır** → 14 DVR'ın alarm, e-posta ve AI
   ayarları sahaya gitmeden masadan değiştirilebilir. Bu operasyonel olarak büyük bir
   kolaylık; kurulum sonrası ayar değişiklikleri için §5 akışına alternatif olur.

**Ama DoLynk üzerine ürün kurulmayacak** — gerekçe §3.1.

## 2.4b Firmware taban çizgisi

Pilot cihazın firmware'i **4.004.0000001.0.R.250929** ve DMSS "en güncel sürüm" diyor.
Bunun üç pratik sonucu var:

1. **Modern taban çizgisi.** 4.004 + 2025 sonu build demek: eski firmware tuzaklarının
   (eksik `getExposureEvents`, tuhaf digest davranışı, 0 tabanlı snapshot kanalı,
   TLS'siz SMTP) görülme olasılığı düşük. Yine de probe doğrulayacak — varsayım değil.
2. **Sabit hedef.** `R` release build ve en güncel sürüm olduğu için pilot boyunca
   altımızdan davranış değişmez. E-posta ayrıştırma profili (`firmware_email_profiles`)
   bu sürüme sabitlenir.
3. **Sıkılaştırılmış güvenlik varsayılanları.** Yeni Dahua firmware'leri parola
   karmaşıklığı dayatır, hesap kilitleme uygular ve bazı erişimleri (CGI/özel protokol,
   ONVIF) açıkça etkinleştirmeyi ister. Servis hesabı oluştururken ve probe'un yeniden
   deneme davranışında bu hesaba katılmalı (§24.13–15).

**Filo tek tip.** 14 cihazın tamamı aynı model ve aynı firmware. Sonuçları:

- **Tek e-posta ayrıştırma profili** yeter. `firmware_email_profiles` tablosu yine de
  kalır (SaaS ileride başka model görecek), ama V1 tek satırla çalışır ve o satır
  gerçek e-postalarla doğrulanır.
- **Tek kurulum şablonu** — bir cihaz mükemmel yapılandırılıp konfigürasyon diğerlerine
  kopyalanır (§5.6).
- **Tek doğrulama seti** yeter; probe bir cihazda geçerse diğerlerinde de geçmesi beklenir
  (yine de her cihazda çalıştırılır, sonuç kayda geçer).

**Monokültür riski.** Madalyonun diğer yüzü: bir firmware davranışı bizi kırarsa
14 sahayı **aynı anda** kırar. Karşı önlemler:

- Ayrıştırıcı hiçbir koşulda olayı düşürmez; ayrıştırılamayan alan `UNKNOWN` olur (§7.3).
- Her cihazın firmware sürümü kayıtlıdır; değişirse uyarı üretilir.
- **Ayrıştırma başarısızlık oranı izlenir**; ani yükseliş firmware güncellemesi
  şüphesidir ve ham e-posta örneği otomatik saklanır (`email_samples`).
- Müşteriye firmware güncellemelerini bize haber vermesi sözleşmede belirtilir.

## 2.5 Fiziksel portlar

```
Analog giriş : 8 BNC (HDCVI/AHD/TVI/CVBS auto-detect)
Ses giriş    : 1 RCA + 8 coaxial audio       Ses çıkış: 1 RCA
Two-way talk : var (1. kanalın ses girişini paylaşır)
HDD          : 1 SATA, 16 TB'a kadar
Ağ           : 1 × 10/100 Mbps                RS-485: 1 (PTZ)
Alarm in/out : YOK — cihazda alarm terminali bulunmuyor
```

**Alarm rölesi olmadığı için siren doğrudan sürülemez.** Caydırıcı ses **audio out (RCA)**
üzerinden verilir (§6).

## 2.6 Diğer sınırlar

- IP kanal eklenirse: *"After IP channels are added beyond the existing channels, the
  AI Function (IVS, SMD, FACE) will be disabled."* → **IP kanal genişletmesi yapılmayacak.**
- 5MP modda encoder bütçesi düşük: 4 kanal 5MP@1–6 fps. Kamera çözünürlüğü/fps seçimi
  kurulumda bilinçli yapılmalı.
- Ağ portu 100 Mbps; 8 kanal main stream aynı anda dışarı taşınamaz (zaten taşınmayacak).
- Video: AI Coding / Smart H.265+ / H.265 / Smart H.264+ / H.264.

---

# 3. MİMARİ KARARI: DIŞARI ÇIKIŞ YOLU

Şantiyede ek cihaz yok, CGNAT var, port açmak yasak. DVR'ın **kendi başlattığı**
bağlantılar dışında seçenek yoktur. Değerlendirilen yollar:

| Yol | Durum | V1 kararı |
|-----|-------|-----------|
| **SMTP alarm e-postası (snapshot ekli)** | Datasheet: linkage listesinde `email`, protokolde `SMTP` | **Birincil taşıma** |
| **Auto Registration** (cihaz dışarı bağlanır, platform içeri erişir) | Datasheet'te var; Dahua SDK sunucu tarafı gerekir | Tier B, §18 |
| **Alarm Server** | Datasheet'te var; konuştuğu protokol doğrulanmadı | §24'te doğrulanacak; HTTP ise e-postanın önüne geçer |
| P2P (DMSS) | Vendor kapalı ekosistemi | Canlı izleme için müşteriye yönlendirme (§12) |
| FTP/SFTP | Bu modelde listelenmiyor | Tasarımda kullanılmayacak |
| ONVIF event subscribe | İçeri bağlantı gerektirir | CGNAT altında imkânsız |
| Port forwarding / DDNS | Yasak ve tehlikeli | **Reddedildi** — §19.1 |

E-posta modern bir entegrasyon yolu değil, ama bu kısıt kümesinde **çalışan tek
datasheet-doğrulamalı yol** o. Zayıflıkları ve karşı önlemleri §7.4'te.

## 3.1 Neden DoLynk / P2P üzerinden gitmiyoruz

Cihazda DoLynk aktif ve Dahua bulutu alarmları zaten taşıyor. Cazip görünüyor, ama:

- **Açık API yok.** Dahua'nın üçüncü taraf entegrasyon kapısı DEPP'tir
  (kayıt + NDA + API lisans anahtarı) ve oradaki alarm abonelik API'leri **DSS
  Professional** ile cihaz SDK'sı etrafında kurgulanmıştır. DoLynk tüketici/kurulumcu
  bulutundan alarm çekmek için yayınlanmış bir API bulunmuyor.
- **Rakibin bulutuna bağımlılık.** Ürünün kalbini kapalı bir vendor bulutuna bağlamak,
  Dahua'nın bir sürümde davranışı değiştirmesiyle ürünü durdurabilir.
- **Sözleşme riski.** Ticari bir SaaS'ı vendor'ın son kullanıcı bulutu üzerinden
  çalıştırmak kullanım şartlarına takılabilir; hukuki inceleme olmadan yapılmaz.

**Karar:** DoLynk operasyonel araç olarak kullanılır (canlı izleme yönlendirmesi,
uzaktan ayar), veri hattı olarak kullanılmaz. DEPP üzerinden resmi ortaklık
başvurusu §24'te değerlendirme maddesidir; olumlu sonuçlanırsa mimari yeniden gözden
geçirilir.

## 3.2 Ürün konumlandırması — DoLynk zaten alarm gönderiyorsa neden Nightshift?

Bu sorunun net cevabı olmadan ürün satılmaz. DMSS/DoLynk Care ücretsiz olarak
insan algılama bildirimi gönderiyor. Nightshift'in var oluş sebebi bunlar:

| Nightshift | Dahua uygulaması |
|---|---|
| **Kanıt bulutta** — hırsız DVR'ı çalsa bile fotoğraf elimizde (§13.1) | Kanıt yalnızca çalınan diskte |
| **AI ikinci doğrulama** — yanlış alarm azalır | Filtresiz bildirim; kullanıcı bildirimi susturur ve gerçek hırsızlığı kaçırır |
| **Kural motoru** — bölge + saat + risk skoru | Kanal bazında aç/kapa |
| **Escalation + ACK** — kim gördü, kaç saniyede onayladı | Sorumluluk zinciri yok |
| **Çoklu şantiye / çoklu müşteri panosu, roller** | Cihaz listesi |
| **Sesli caydırıcı iş akışı ve kurulum standardı** | Elle ayar |

Bunların en güçlüsü ilki: şantiye hırsızları **kaydediciyi de götürür**. DVR gidince
delil de gider. Bu tek başına ürünün gerekçesidir.

---

# 4. V1 MİMARİSİ

```
   Şantiye (ek donanım YOK)                 │            Nightshift Cloud
                                            │
  Analog kameralar                          │
        │                                   │
   Dahua XVR5108HS-I3                       │
   AI Mode = IVS&SMD                        │
        ├── SMD Plus (8 kanal)              │
        ├── Perimeter (4 kanal)             │
        │                                   │
        ├─► voice prompt ─► RCA ─► horn hoparlör   (anında, internetsiz çalışır)
        ├─► record + snapshot ─► yerel HDD           (delil, DMSS ile izlenir)
        │                                   │
        └─► SMTP (TLS, outbound) ───────────┼──► Ingest MTA
                                            │         │
                                            │    E-posta ayrıştırıcı
                                            │    (site kimliği, kanal, olay, JPEG)
                                            │         │
                                            │    AI ikinci doğrulama (insan var mı?)
                                            │         │
                                            │    Zone + Schedule + Risk
                                            │         │
                                            │    Alarm + Escalation
                                            │         │
                                            │    FCM / APNs ──► Flutter uygulaması
                                            │                        │
                                            │                   canlı görüntü →
                                            │                   DMSS deep link
```

Cloud bileşenleri değişmedi: PostgreSQL (kaynak veri), Redis (cache/lock), RabbitMQ
(AI ve bildirim kuyruğu), S3/MinIO (medya, private bucket + presigned URL).

---

# 5. CİHAZ KURULUMU (COMMISSIONING)

Kurulum tek seferlik ve elle yapılır; ürünün bir parçasıdır, "kullanıcı halleder"
denmez. Kurulum ekibi için kontrol listesi:

## 5.1 Temel

```
[ ] Firmware sürümü kaydedildi
[ ] Tarih/saat doğru, NTP açık, timezone doğru
[ ] admin parolası değiştirildi
[ ] Nightshift için ayrı servis hesabı açıldı (admin kullanılmaz)
[ ] IP kanal genişletmesi KAPALI (AI fonksiyonlarını devre dışı bırakır)
[ ] Kayıt planı 7/24 veya en az olay bazlı aktif
[ ] HDD sağlıklı, kapasite retention hedefine uygun
[ ] Router üzerinde DVR'a port yönlendirmesi YOK
[ ] DVR internete çıkabiliyor: SMTP testi 587 ile geçti (yedek 465, 2525) — port 25 kullanılmaz
[ ] DVR DNS ayarı çalışıyor (gerekirse sabit 1.1.1.1 / 8.8.8.8)
```

## 5.2 AI

```
[ ] AI Mode = IVS&SMD
[ ] SMD Plus tüm ilgili kanallarda açık, hedef = Human (+ Vehicle, karara göre)
[ ] Perimeter protection en kritik 4 kanalda: tripwire ve/veya intrusion
[ ] Perimeter kurallarında hedef filtresi Human/Vehicle seçili
[ ] Yüz tespiti kullanılmıyor (AI Mode zaten dışlıyor) — §19.3
[ ] Hassasiyet ve zaman planı ayarlandı (gündüz/gece farklı olabilir)
```

## 5.3 Alarm linkage

```
[ ] Snapshot açık (alarm anı görüntüsü)
[ ] Record açık, pre-record süresi ayarlandı
[ ] Email açık, "attach picture" işaretli
[ ] Email alıcısı = siteye özel ingest adresi (§7.1)
[ ] Email gönderim aralığı (send interval) 60 sn veya altına çekildi
[ ] Health/test e-postası periyodik açık (§13)
[ ] Voice prompt açık, ses dosyası yüklendi (§6)
[ ] Buzzer tercihe göre
```

## 5.4 Kanal isimlendirme

Kanal adları e-posta gövdesinde geliyorsa eşlemenin anahtarıdır. Kurulumda her kanala
`KAMERA-01 Depo Arka` gibi **kararlı ve tekil** bir ad verilir ve aynısı Nightshift
uygulamasına girilir.

## 5.5 Doğrulama

Kurulum ekibinin dizüstü bilgisayarından, DVR ile aynı ağda, tek seferlik:

```bash
python scripts/dahua_probe.py --host <DVR_IP> --username <servis_hesabı> \
  --ask-password --trigger-channel 1 --listen 60
```

Bu bir saha aracı; sahada kalıcı olarak çalışmaz. Ürettiği JSON rapor cihaz kaydına
iliştirilir: model, firmware, kanal eşlemeleri, gerçekten görülen olay kodları.

## 5.6 Filo dağıtımı — 14 özdeş cihaz

Filonun tamamı aynı model ve aynı firmware (§2.4b). Bu, kurulumu 14 kez tekrarlanan
bir el işi olmaktan çıkarır.

### Konfigürasyon şablonu

1. **Bir cihazı referans olarak mükemmel yapılandır** (§5.1–5.4) ve doğrula (§5.5).
2. Web arayüzünden konfigürasyonu dışa aktar (`System → Backup/Import Config`).
3. Kalan 13 cihaza içe aktar.
4. Her cihazda yalnızca **cihaza özel farkları** ayarla:

```
cihaz adı                (e-posta gövdesinde görünür, site ile eşleşmeli)
SMTP kullanıcı + parola  (cihaza özel, §7.1)
kanal adları             (o sahanın kameraları)
IVS/perimeter kural geometrisi (o sahanın görüntüsüne göre çizilir)
ağ ayarları              (DHCP değilse)
```

Şablon dosyası **parola içerdiği için** sır muamelesi görür: şifreli olarak saklanır,
repoya girmez, kurulum sonrası silinir.

### Kademeli yayılım

14 cihazı birden yapılandırmak, bir hatayı 14 kez yapmak demektir.

```
1 saha   → tam doğrulama, bir hafta gerçek kullanım, yanlış pozitif ölçümü
3 saha   → şablonun farklı sahalarda tuttuğunu doğrula (aydınlatma, kamera açısı)
10 saha  → kalan yayılım
```

### Perimeter kanal planı

Cihaz başına perimeter protection **4 kanalla sınırlı** (§2.1), saha başına ~5 kamera
var. Yani her sahada 4 kameraya tripwire/intrusion kuralı yazılabilir; 5. kamera
yalnızca SMD Plus ile korunur — bu bir eksiklik değil, SMD Plus zaten 8 kanalda çalışıyor.

Seçim kuralı: **perimeter kuralları çevreye bakan kameralara** verilir (çit hattı, giriş
kapısı, malzeme sahası sınırı). İç alana bakan kamera SMD Plus ile bırakılır.

---

# 6. CAYDIRICILIK — EN ÖNEMLİ TEK ÖZELLİK

Bir hırsızı "suçüstü yakalamak" iki sonuçtan biriyle biter: ya adam kaçar (mal kurtulur),
ya yakalanır (birinin gelmesi gerekir). İkincisi dakikalar sürer; birincisi **saniyeler**.

DVR'ın `voice prompt` linkage'ı, alarm anında yerel olarak, internet olmadan, cloud
gecikmesi olmadan bir ses dosyası çalar. RCA audio out'a bir **amfili horn hoparlör**
bağlanır.

```
Kamera → SMD Plus insan tespiti → voice prompt → hoparlör:
  "Bu alan 24 saat kamera ile kayıt altındadır. Güvenlik birimi bilgilendirildi."
```

Bu, mimarinin geri kalanından bağımsız çalışır ve gecikmesi ~1 saniyedir. Maliyeti bir
hoparlör + kablo. Kurulum paketinin zorunlu parçası olmalı.

Notlar:

- Ses seviyesi ve saat aralığı gürültü mevzuatına göre ayarlanmalı; gece 23:00–07:00
  arası ses seviyesi yerel kurallara tabidir. Müşteriye bu sorumluluk yazılı bildirilir.
- Alarm rölesi olmadığı için elektrikli siren doğrudan sürülemez (§2.5). Röle gerekiyorsa
  bir TiOC/alarm çıkışlı IP kamera üzerinden `IPC external alarm output` linkage'ı
  kullanılabilir — bu ek donanımdır, V1 zorunluluğu değildir.

---

# 7. E-POSTA INGEST SÖZLEŞMESİ

## 7.1 Kimlik = cihaz başına SMTP hesabı

DVR bir SMTP **istemcisidir**: bir sunucuya kullanıcı adı/parola ile bağlanıp mail
gönderir. Bu sunucu biz oluruz. Böylece kimlik doğrulama gerçek olur:

```
DVR SMTP ayarı
  Sunucu   : mail.nightshift.<tld>
  Port     : 587 (STARTTLS)  ·  yedek 465 (implicit TLS)  ·  yedek 2525
  Kullanıcı: dev-<device-uuid>          ← cihaza özel, yalnızca bu cihaz
  Parola   : <rastgele 32 karakter>     ← cihaza özel
  Alıcı    : alarm@in.nightshift.<tld>  ← sabit, sır değil
```

Kural:

- Her DVR'ın **kendi SMTP hesabı** vardır. Hesap yalnızca gönderim yetkilidir, yalnızca
  tek alıcıya gönderebilir, hız limitlidir.
- Cihaz kimliği **SMTP AUTH kullanıcı adından** çözülür; e-posta gövdesindeki cihaz
  adından değil. Gövde ayrıştırılamasa bile olayın hangi cihazdan geldiği kesindir.
- Bir hesap sızarsa yalnızca o cihazın alarmları taklit edilebilir; hesap tek komutla
  döndürülür ve DoLynk Care üzerinden DVR'daki ayar uzaktan güncellenir (§2.4).
- Alıcı adresi tekil olmak zorunda değildir; istenirse cihaz başına alt-adres
  (`alarm+<device-uuid>@…`) yönlendirme kolaylığı için kullanılır — ama **kimlik
  doğrulama işini adres değil, SMTP AUTH yapar**.

Alan adı ayrı tutulur (`in.` alt alanı) ki kurumsal e-posta trafiğiyle karışmasın.

## 7.2 Ingest hattı

```
DVR ──SMTP AUTH + TLS──► kendi MTA'mız (Postfix/Haraka)
      (giden bağlantı,        │  auth kullanıcısı → device_id
       NAT sorunu yok)        ▼
                        ingest kuyruğu (RabbitMQ)
                              ▼
                        email_parser  →  event + media_asset
```

Üçüncü parti posta kutusundan IMAP ile çekmek yerine **kendi MTA'mıza teslim** tercih
edilir: gecikme düşer, spam filtresi araya girmez ve kimlik doğrulamayı biz yaparız.

MTA **587, 465 ve 2525** portlarını birlikte dinler. Gerekçe §7.5.

## 7.5 NAT, CGNAT ve operatör kısıtları

NAT yalnızca **içeri** gelen bağlantıyı engeller. Bu mimaride sahadan içeri hiçbir
bağlantı gelmez; DVR her şeyi kendisi başlatır. Cihazda DoLynk'in çalışıyor olması
(§2.4) çıkışın açık olduğunun canlı kanıtıdır — DoLynk bağlanabiliyorsa SMTP de
bağlanır.

Gerçek riskler NAT değil, operatör filtreleridir:

| Sorun | Belirti | Çözüm |
|-------|---------|-------|
| **Port 25 engeli** (neredeyse tüm mobil/4G operatörlerinde) | Test maili gitmiyor | Port 25 **hiç kullanılmaz**; 587 birincil, 465 yedek |
| 587 de engelli (bazı kurumsal hatlar) | Aynı | MTA 2525'i de dinler |
| Firmware TLS uyumsuzluğu | Bağlanıyor, auth sonrası kopuyor | MTA TLS 1.2 kabul eder, yaygın güvenilen sertifika (Let's Encrypt) kullanır |
| DNS bozuk (4G router garip DNS dağıtıyor) | Sunucu adı çözülmüyor | DVR'a sabit DNS (1.1.1.1 / 8.8.8.8) girilir |
| NTP engelli | Olay saatleri yanlış | Kurulum kontrolü (§5.1); saat kayması blocker |
| Operatör şeffaf proxy'si | HTTP dışı protokoller bozuluyor | 465 (implicit TLS) denenir; çözülmezse o hat için Tier B |

**Bant genişliği** bu tasarımda sorun değil: alarm başına ~150–300 KB (bir JPEG).
Gecede 50 alarm ≈ 10 MB, ayda ≈ 300 MB/DVR. Kotalı 4G hatlarında bile rahat çalışır —
sürekli video akışına göre iki büyüklük mertebesi az.

**Statik IP gerekmez**, DDNS gerekmez, port yönlendirme gerekmez.

## 7.3 Ayrıştırma

E-posta gövdesinin birebir biçimi firmware'e göre değişir. **Biçim koda gömülmez.**

- Kurulumda cihazdan gelen ilk e-posta `email_samples` tablosuna ham olarak kaydedilir.
- Ayrıştırıcı, firmware profiline bağlı **regex kural setiyle** çalışır
  (`firmware_email_profiles`).
- Çıkarılacak alanlar: olay türü, kanal numarası, kanal adı, cihaz adı, olay zamanı.
- Ayrıştırılamayan alan → `UNKNOWN`, olay yine de kaydedilir ve alarm üretebilir.
  **Ayrıştırma hatası olayı düşürmez.**
- Ekteki JPEG'ler `media_assets` olarak S3'e yazılır; içerik tipi ve magic byte doğrulanır.

## 7.4 E-posta taşımasının zayıflıkları ve karşı önlemler

| Risk | Önlem |
|------|-------|
| Sahte alarm enjeksiyonu | **SMTP AUTH** (cihaz başına hesap, §7.1); hesap başına hız limiti; tek alıcıya kısıt. Gönderen IP'si CGNAT altında paylaşımlı ve değişken olduğu için **kimlik doğrulamada kullanılmaz** — yalnızca zayıf anomali sinyali |
| Gecikme | Kendi MTA'mız, üçüncü parti kutu yok; gecikme ölçülür ve metriklenir (§22) |
| E-posta hiç gelmiyor | Sağlık watchdog (§13): beklenen periyodik e-posta gelmezse site "sessiz" alarmı |
| Aynı olay için onlarca e-posta | DVR send interval + cloud dedup (§8.2) |
| DVR SMTP kimlik bilgisi | Site başına ayrı, yalnızca gönderim yetkili hesap; sızarsa tek site etkilenir |
| Ek boyutu / kota | Ek boyut sınırı, kota aşımında metadata korunur, medya düşürülür |

---

# 8. OLAY MODELİ

## 8.1 Olay türleri (DVR kaynaklı)

```
person_detected      ← SMD Plus human
vehicle_detected     ← SMD Plus vehicle
line_crossing        ← perimeter tripwire
zone_intrusion       ← perimeter intrusion
motion_detected      ← klasik hareket (düşük güven)
video_loss           ← kanal görüntü kaybı
video_tampering      ← kamera kapatma/çevirme
storage_*            ← disk yok / hata / dolu
device_offline       ← anomaly alarm veya watchdog
```

## 8.2 Dedup

```
Anahtar : tenant_id + site_id + device_id + channel + event_type
Pencere : 60 saniye (varsayılan; kural bazında değiştirilebilir)
```

Eski spec'te 15 sn idi; e-posta taşımasında DVR'ın kendi gönderim aralığı da devrede
olduğu için 60 sn daha gerçekçi.

## 8.3 Yaşam döngüsü

```
NEW → VERIFYING → VERIFIED → ALERTED → ACKNOWLEDGED → RESOLVED
                     └────────────────────────────► FALSE_POSITIVE
```

## 8.4 İdempotency

E-posta yeniden teslim edilebilir. Anahtar: `message_id` + cihaz + olay zamanı.
`UNIQUE(source_message_id)`.

---

# 9. AI İKİNCİ DOĞRULAMA

DVR'ın SMD Plus'ı birinci filtre. Cloud AI ikinci filtre; girdi **e-posta ekindeki
JPEG**.

```
Snapshot → person/vehicle detector → bounding box + confidence
        → zone testi (bizim polygon'larımız, snapshot üzerinde)
        → schedule testi (site timezone)
        → risk skoru → alarm / log-only
```

Model iş mantığına gömülmez; `DetectorProvider` arayüzü üzerinden çalışır (ONNX /
TensorRT / uzak GPU değiştirilebilir). Ticari lisans uyumu kontrol edilmeden kısıtlı
lisanslı model production'a alınmaz.

**AI servisi çökerse alarm kaybedilmez** (§11.3).

---

# 10. ZONE, SCHEDULE, RISK

## 10.1 Zone

DVR'a uzaktan kural yazamayız, ama **kendi zone'larımızı snapshot üzerinde**
uygulayabiliriz. Kullanıcı uygulamada kameranın son görüntüsü üzerine polygon çizer;
AI'ın bulduğu kişinin ayak noktası (bounding box alt-orta) polygon içinde mi diye
bakılır.

Koordinatlar normalize (0.0–1.0). Zone tipleri: `NORMAL`, `RESTRICTED`, `IGNORE`,
`ENTRY`, `EXIT`, `VEHICLE_ONLY`, `FIRE_RISK`.

Kamera açısı değişirse zone geçersizleşir → tamper tespiti zone'ları da geçersiz kılar.

## 10.2 Schedule

Site'in IANA timezone'una göre değerlendirilir. Gece yarısını geçen aralıklar
(19:00–06:00) doğru desteklenmelidir. Tüm timestamp'ler DB'de UTC `timestamptz`.

## 10.3 Risk skoru (varsayılan, konfigüre edilebilir)

```
person_detected                 +10
outside_business_hours          +25
restricted_zone                 +30
perimeter_rule_triggered        +15
repeated_entry_same_camera      +10
camera_tamper_near_event        +20
vehicle_in_restricted_zone      +20
ai_confidence_below_threshold   -15
```

```
0–29 INFO · 30–49 LOW · 50–69 MEDIUM · 70–84 HIGH · 85–100 CRITICAL
```

---

# 11. ALARM VE BİLDİRİM

## 11.1 Push

```json
{
  "type": "security_alert",
  "event_id": "uuid",
  "severity": "CRITICAL",
  "site_name": "Beton Tesisi",
  "camera_name": "Depo Arka",
  "title": "İnsan algılandı",
  "body": "Yasak bölgede kişi tespit edildi",
  "occurred_at": "2026-09-12T22:14:32Z"
}
```

Push içinde DVR credential veya RTSP URL bulunmaz. Uygulama alarm detayına deep-link
eder; snapshot alarm ekranında ilk görünen şeydir.

## 11.2 Escalation

```
T+0   → Bekçi / saha sorumlusu
T+60  → Şantiye şefi
T+180 → Güvenlik müdürü / firma sahibi
```

Biri ACK yaparsa zincir durur. Eski spec'teki 30/60/120 sn kademeleri, e-posta
taşımasının gerçek gecikmesi ölçülene kadar 60/180 olarak gevşetildi.

## 11.3 AI kullanılamıyorsa

```
if_ai_unavailable: ALERT_ON_VENDOR_EVENT   (güvenlik kuralları için varsayılan)
                   QUEUE_ONLY
                   DROP
```

Push metni: *"İnsan alarmı — AI doğrulama geçici olarak kullanılamıyor"*.

## 11.4 Çözüm kodları

```
TRUE_SECURITY_INCIDENT · SUSPICIOUS_ACTIVITY · AUTHORIZED_PERSON · FALSE_POSITIVE
ANIMAL · WEATHER · LIGHT_CHANGE · MAINTENANCE · CAMERA_ERROR · OTHER
```

Bu geri bildirim AI eğitim/ayar veri setinin temelidir.

---

# 12. CANLI GÖRÜNTÜ VE KAYIT — DÜRÜST ÇÖZÜM

Ek donanım olmadan bulut üzerinden canlı yayın **veremiyoruz**. Uydurma bir çözüm
sunmak yerine akışı şöyle kuruyoruz:

- **Alarm anı görüntüsü** uygulamada anında görünür (e-posta eki).
- **Canlı izleme** için uygulama, Dahua'nın kendi **DMSS** uygulamasına deep-link eder.
  Müşteri DMSS'i zaten kuruyor; P2P ile CGNAT arkasından çalışır.
- **Olay kaydı** DVR'ın diskinde durur. Uygulama alarmın tam zaman damgasını ve kanalını
  gösterir; kullanıcı DMSS playback'te o saniyeye gider. Kurulum sırasında bu akış
  müşteriye gösterilir.
- Tier B açılırsa (§18) canlı görüntü ve 30 sn klip Nightshift içine taşınır.

Bu, ürünün en zayıf noktası ve satışta böyle anlatılmalı: **Nightshift alarm ve kanıt
üretir; canlı izleme DVR'ın kendi uygulamasındadır.**

---

# 13. SAĞLIK İZLEME VE DVR HIRSIZLIĞI

## 13.1 DVR çalınırsa

Şantiye hırsızlığının klasik senaryosu: hırsız önce kaydediciyi alır, sonra rahatça
çalışır. Bu durumda DVR'daki tüm kayıt gider.

Nightshift'te olay zinciri şöyle işler ve **delil korunur**:

```
t+0    kamera insanı görür → SMD Plus → sesli caydırıcı çalar
t+0    snapshot + kayıt DVR'a yazılır
t+2-30 snapshot e-posta ile buluta çıkar   ◄── DVR sonradan çalınsa bile bu kayıp değil
t+30   telefona fotoğraflı push
...
t+X    hırsız DVR'ı söker → cihaz sessizleşir
t+X+   watchdog "site SILENT" alarmı üretir (§13.3)
```

Tasarım sonuçları:

- Snapshot **DVR'dan silinemez**; buluta çıktığı anda tenant'ın arşivindedir.
- Watchdog periyodu kısa tutulmalı: 6 saat, DVR sökülmesini fark etmek için çok uzun.
  Kritik sitelerde test e-postası periyodu **30–60 dakikaya** çekilir.
- `SILENT` durumu normal bir bakım uyarısı değil, **güvenlik olayıdır**: gece saatinde
  gelirse risk skoru yükseltilir ve escalation zinciri başlatılır.
- DVR'ı görecek bir kamera konumlandırmak kurulum tavsiyesidir (kaydedici dolabına
  bakan kanal).

## 13.2 İzlenebilenler

Ek cihaz olmadığı için sağlık bilgisi de DVR'ın gönderdikleriyle sınırlı:

| Sinyal | Kaynak |
|--------|--------|
| DVR çevrimdışı / internet kesik | Beklenen periyodik e-posta gelmedi (watchdog) |
| Kanal görüntü kaybı | `video loss` alarm e-postası |
| Kamera kapatıldı/çevrildi | `video tampering` alarm e-postası |
| Disk yok / hata / dolu | anomaly alarm e-postası |
| Kayıt gerçekten yapılıyor mu | **UNKNOWN** — uzaktan doğrulanamaz |

## 13.3 Watchdog

DVR'da periyodik test/health e-postası açılır (kritik sitelerde 30-60 dk, §13.1).
`son_eposta + 1.5 × periyot` aşılırsa site `SILENT` durumuna geçer ve uyarı üretilir.

**Bastırma hiyerarşisi:** site sessizse tek alarm üretilir, kanal alarmları bastırılır.

`RECORDING_OK` gibi doğrulanamayan bir durum asla raporlanmaz.

---

# 14. MULTI-TENANCY, RBAC, KİMLİK

Değişmedi:

- Hiyerarşi: Tenant → Sites → Devices → Cameras; Users, Roles, Subscription, Audit Logs.
- Her business tablosu `tenant_id`; PostgreSQL RLS + servis katmanında zorunlu filtre.
- Roller: `OWNER`, `ADMIN`, `SECURITY_MANAGER`, `SITE_MANAGER`, `SUPERVISOR`, `GUARD`,
  `VIEWER`, `BILLING_ADMIN`.
- Kimlik: e-posta/parola, Argon2id, kısa ömürlü access JWT, refresh token rotasyonu,
  push token yönetimi, opsiyonel TOTP iskeleti.
- Cihaz kimliği artık makine token'ı değil, **cihaza özel SMTP hesabıdır** (§7.1);
  enrollment akışı bu hesabın üretilmesi ve DVR'a girilmesinden ibarettir.

---

# 15. VERİ MODELİ DEĞİŞİKLİKLERİ

Kaldırılan: `edge_gateways`, `edge_commands`, `edge_command_results`.

Eklenen:

```
device_smtp_accounts      (device_id, username, password_hash, recipient, rate_limit,
                           rotated_at, active)
email_messages            (id, device_id, smtp_auth_user, message_id, sender_ip,
                           received_at, raw_headers, parse_status, parse_profile_id)
email_samples             (device_id, firmware, raw_email, captured_at)
firmware_email_profiles   (model, firmware_pattern, field_regexes, verified_at)
device_silence_state      (device_id, last_message_at, expected_interval_s, state)
```

Korunan: `tenants`, `subscriptions`, `users`, `roles`, `sites`, `devices`,
`device_capabilities`, `cameras`, `camera_zones`, `camera_ai_profiles`, `rules`,
`events`, `event_detections`, `alarms`, `alarm_deliveries`, `alarm_acknowledgements`,
`alarm_resolutions`, `media_assets`, `push_devices`, `ai_jobs`, `ai_results`,
`health_checks`, `audit_logs`, `usage_counters`.

`events` tablosuna eklenecek: `source` (`email` | `tunnel`), `source_message_id`.

---

# 16. API DEĞİŞİKLİKLERİ

Kaldırılan: `/internal/edge/v1/*` ve `/v1/edges/*` (Tier B açılırsa geri gelir).

Eklenen:

```
POST  /v1/devices/{id}/smtp-account          hesap üret / parolayı döndür (rotate)
GET   /v1/devices/{id}/email-samples         kurulum doğrulaması için ham örnekler
POST  /v1/devices/{id}/test-alarm            beklenen test e-postasını doğrula
GET   /v1/devices/{id}/silence-state         watchdog durumu
```

Korunan: auth, tenants, sites, devices, cameras, events, alarms, rules, analytics,
billing, `/v1/realtime`.

`POST /v1/cameras/{id}/live-sessions` V1'de `501 Not Implemented` döner ve DMSS
yönlendirme bilgisini içerir — sessizce boş dönmez.

---

# 17. NEYDEN VAZGEÇTİK

Açıkça ve eksiksiz:

| Özellik | V1 durumu | Neden |
|---------|-----------|-------|
| Uygulama içi canlı izleme | **Yok** — DMSS'e yönlendirme | İçeri bağlantı gerektirir |
| 30 sn olay klibi | **Yok** — kayıt DVR'da, zaman damgası verilir | Medya çıkışı yalnızca e-posta eki |
| Uygulamadan DVR konfigürasyonu | **Yok** — kurulumda elle | Cihaza uzaktan erişim yok |
| Otomatik kanal keşfi | **Yok** — kurulumda elle girilir | Aynı sebep |
| Annotated snapshot indirme | Var (cloud AI üretir) | — |
| Sub-8 sn push | **Hedef 30 sn** | E-posta gecikmesi (§22) |
| Kayıt tazeliği doğrulama | **UNKNOWN** | Uzaktan okunamaz |
| Sürekli AI (PPE, yangın/duman, crowd) | **Yok** | Sürekli video akışı gerektirir |
| Loitering | Sınırlı | Perimeter kuralı varsa cihazdan, yoksa yok |
| Offline olay kuyruğu | DVR'ın kendi kuyruğu | Edge spool yok |

Bu listedeki her satır, ek donanımın gerçek bedelidir. Müşteriye Tier B teklif edilirken
bu tablo kullanılır.

---

# 18. TIER B — OPSİYONEL TÜNEL MODU

Sahada **yeni cihaz almadan** tam özellik seti mümkün olabilir; iki yol:

**B1 — Mevcut router'da WireGuard/Tailscale.** MikroTik, OpenWrt ve birçok 4G router
outbound VPN kurabilir. Cloud DVR'a LAN üzerinden erişir: canlı yayın, klip, uzaktan
konfigürasyon, tam capability probe. Ek donanım yok, sadece router konfigürasyonu.

**B2 — Dahua Auto Registration.** DVR datasheet'te destekliyor: cihaz dışarı bağlanır,
bizim sunucumuz içeri erişir. Router konfigürasyonu bile gerekmez. Bedeli: Dahua NetSDK
tabanlı bir kayıt sunucusu yazmak ve vendor SDK lisansını incelemek.

Mevcut `edge-agent/` kodu **silinmiyor**: CGI istemcisi, olay ayrıştırıcı, snapshot,
RTSP/playback ve probe Tier B'de aynen kullanılacak. Bugün kurulum aracı olarak
(§5.5) zaten kullanılıyor.

Tier B açık bir sitede `events.source = tunnel` olur ve §17 tablosundaki kayıplar geri gelir.

---

# 19. GÜVENLİK VE GİZLİLİK

## 19.1 Ağ

- DVR'a port yönlendirmesi yok, DDNS ile açma yok, UPnP kapalı.
- Dahua kaydediciler internete açıldığında kitlesel taramanın hedefi olur; bu bir
  tercih değil, yasak.
- Cloud yalnızca TLS. Ingest MTA yalnızca TLS kabul eder.

## 19.2 Sırlar

- Ingest adresi sırdır; döndürülebilir.
- DVR SMTP hesabı site başına ayrı ve yalnızca gönderim yetkili.
- Servis hesabı parolası hiçbir logda görünmez (redaksiyon zorunlu).
- Object storage private, presigned URL kısa ömürlü, public bucket yasak.

## 19.3 Yüz verisi

V1 yüz tanıma **ve** yüz tespiti yapmaz. Üç katmanlı garanti:

1. **Cihaz:** `AI Mode = IVS&SMD` seçilir; yüz motoru donanımda çalışmaz (§2.2).
2. **Ingest:** yüz olayı yine de gelirse edge/ingest tarafında düşürülür
   (`BLOCKED_EVENT_CODES`), payload saklanmaz.
3. **Ürün:** insan tespiti anonim nesne tespitidir; kimlik eşleştirme yapılmaz.

Yüz tanıma ileride istenirse ayrı hukuki inceleme, ayrı sözleşme ve ayrı modül gerekir.

## 19.4 Görüntü mahremiyeti

- Kameralar komşu parsel ve kamusal alanı görmeyecek şekilde konumlandırılmalı;
  kurulum raporuna yazılır.
- Retention plan bazlı (7/30/90 gün), müşteriye gösterilir.
- Sesli caydırıcı kayıt yapmaz; two-way talk V1'de kullanılmaz.

## 19.5 Denetim

`login`, `failed_login`, `user_invited`, `role_changed`, `device_added`,
`smtp_account_rotated`, `alarm_acknowledged`, `alarm_resolved`, `rule_changed`,
`subscription_changed` audit'lenir.

---

# 20. FAZLAR

| Faz | Kapsam | Çıkış kriteri |
|-----|--------|---------------|
| **0** | Monorepo, docker compose, servis iskeletleri, lint/test | ✅ tamamlandı |
| **1** | Dahua CGI probe (kurulum aracı) | ✅ tamamlandı — gerçek cihaz doğrulaması bekliyor |
| **2** | Kimlik doğrulamalı SMTP endpoint, MIME/medya ayıklama, profil motoru, ham e-posta yakalama | ✅ kod tamam — gerçek DVR'dan gelen e-posta bekleniyor |
| **3** | Provisional profili gerçek e-postayla değiştir, `verified=True` yap | Kanal, olay türü, zaman ve JPEG gerçek cihazdan doğru çıkarılıyor |
| **4** | Olay kaydı + snapshot storage + realtime | Kameranın önünden geçince uygulamada fotoğraflı olay görünüyor |
| **5** | AI ikinci doğrulama | SMD olayı AI ile doğrulanıyor, annotated snapshot üretiliyor |
| **6** | Zone + schedule + risk | Gece yasak alanda insan alarm üretiyor, normal alanda üretmiyor |
| **7** | Push + alarm workflow + escalation | Olay → push → alarm detayı → ACK zinciri çalışıyor |
| **8** | Sağlık: watchdog, video loss, tamper, disk, bastırma hiyerarşisi | İnternet kesintisi tek alarm üretiyor |
| **8b** | Filo yayılımı: 1 → 3 → 10 saha, konfigürasyon şablonu (§5.6) | 14 cihaz aynı şablonla kurulu ve olay üretiyor |
| **9** | Caydırıcı kurulum paketi + DMSS deep link | Sahada ses çalıyor, uygulamadan DMSS açılıyor |
| **10** | Multi-tenant sertleştirme + RBAC | Tenant A, tenant B verisine hiçbir endpointten erişemiyor |
| **11** | Abonelik ve kullanım sayaçları | Plan limitleri gerçek fonksiyonları etkiliyor |
| **12** | Analitik | Alarm/gün, yanıt süresi, doğru/yanlış pozitif |
| **13** | Production hardening | Rate limit, yedek, DR, metrik, yük testi, mobil release |
| **B** | Tier B tünel (opsiyonel) | Canlı görüntü ve klip Nightshift içinde |

PHASE 1 gerçek cihazda doğrulanmadan PHASE 2'ye geçilmez.

---

# 21. TEST STRATEJİSİ

**Unit:** e-posta ayrıştırıcı (kaydedilmiş gerçek e-postalarla), dedup, schedule
değerlendirici, zone geometrisi, risk skoru, RBAC, Dahua olay ayrıştırıcı (mevcut).

**Integration:** MTA → kuyruk → parser → event; AI → backend; medya yükleme; push
soyutlaması; watchdog zamanlayıcı.

**Donanım döngüsünde (gerçek DVR):** SMD human, SMD vehicle, perimeter tripwire,
video loss (kablo çek), video tampering (lens kapat), disk çıkar, internet kes,
DVR kapat, gece/gündüz aydınlatma farkı, yağmur/rüzgâr yanlış pozitifleri.

**Mobil E2E:** login, push, deep link, ACK, DMSS yönlendirme.

**Kritik regresyon:** yüz olayının hiçbir katmanda saklanmadığı testle sabitlenir.

---

# 22. KAPASİTE, MALİYET VE PİLOT METRİKLERİ

## 22.1 Filo boyutlandırma

14 DVR × ~5 kamera = **~70 kamera**. Olay hacmi tahmini: aktif gecelerde saha başına
~40 insan olayı → **~560 olay/gece**, ~17.000 olay/ay.

| Kaynak | Hesap | Sonuç |
|--------|-------|-------|
| Gelen e-posta | 17.000 × ~250 KB | **~4 GB/ay** |
| Medya deposu (30 gün, orijinal + annotated) | 17.000 × 250 KB × 2 | **~9 GB** sabit durum |
| Saha başına veri | 40 × 250 KB × 30 | **~300 MB/ay/DVR** |
| AI çıkarımı | ~570 görüntü/gün | **GPU gerekmiyor** |
| MTA | 560 mesaj/gece | Tek küçük sunucu fazlasıyla yeter |

**En önemli sonuç: V1 için GPU yok.** Olay başına tek bir JPEG işlendiği için CPU
üzerinde ONNX çalıştırmak yeterli — günde bir dakikanın altında toplam çıkarım süresi.
GPU ancak sürekli video analizine geçilirse gerekir ki V1 kapsamında o yok (§17).

Tüm V1 altyapısı (backend + AI + MTA + PostgreSQL + object storage) tek bir orta
boy sunucuda çalışır. Bu, edge gateway mimarisine göre hem donanım hem işletme
maliyetinde büyük fark demektir.

Sınır uyarısı: bu rakamlar **olay-tetiklemeli** tasarıma bağlıdır. Yanlış pozitif
oranı kontrolden çıkarsa (rüzgâr, yağmur, böcek, aydınlatma değişimi) e-posta hacmi
katlanır. Bu yüzden §22.2'deki yanlış pozitif metriği yalnızca kalite değil, **maliyet
metriğidir**.

## 22.2 Pilot başarı metrikleri

Bir haftalık gerçek şantiye testinde ölç:

```
insan olayı sayısı / doğrulanan / yanlış pozitif
kaçırılan olay (kayıttan geriye bakarak)
SMD → e-posta teslim gecikmesi (p50 / p95)
e-posta → push gecikmesi (p50 / p95)
uçtan uca gecikme (p50 / p95)
snapshot eki gelme oranı
caydırıcı ses sonrası ayrılma oranı (kayıttan gözlem)
DVR sessizlik (watchdog) olayları
aylık e-posta hacmi ve depolama
```

Hedefler:

```
SMD → push p95          < 30 sn
snapshot eki geliş      > 95%
yanlış pozitif / gece   < 3 / kamera / gece
kritik olay kaybı       0 (DVR ve internet çalışırken)
cross-tenant sızıntı    0
```

Eski spec'in 8 sn hedefi e-posta taşımasında gerçekçi değil; 30 sn ölçülerek
doğrulanacak ve gerekirse Tier B gerekçesi olacak.

---

# 23. V1 TAMAMLANDI SAYILMA KOŞULLARI

- 14 DVR eklenebiliyor, her biri kendi ingest adresiyle.
- Kurulum kontrol listesi (§5) her cihaz için doldurulmuş.
- İnsan olayı 30 sn içinde fotoğraflı push olarak telefona düşüyor.
- Sahada sesli caydırıcı çalışıyor.
- AI ikinci doğrulama çalışıyor; yanlış pozitif oranı ölçülüyor.
- Zone ve schedule çalışıyor; risk skoru alarm seviyesini belirliyor.
- ACK ve escalation çalışıyor.
- Video loss, tampering, disk ve sessizlik alarmları çalışıyor, bastırma hiyerarşisi doğru.
- Yüz verisi hiçbir katmanda saklanmıyor (testle kanıtlı).
- Multi-tenant izolasyon testleri geçiyor; RBAC çalışıyor.
- Android ve iOS build alınabiliyor; push deep-link çalışıyor.
- Yedek/geri yükleme test edilmiş, monitoring panosu hazır.

---

# 24. CİHAZDA DOĞRULANACAKLAR

Datasheet'in cevaplamadığı, ilk kurulumda **ölçülecek** sorular. Hiçbiri varsayılmayacak:

0. **DEPP ortaklık başvurusu** (https://depp.dahuasecurity.com) yapılsın mı? NDA
   sonrası alarm abonelik API'sinin DoLynk P2P cihazlarını kapsayıp kapsamadigi
   ogrenilir. Kapsiyorsa e-posta hatti yerine resmi API kullanilir (§3.1).
1. `Alarm Server` menüsü hangi protokolü konuşuyor? HTTP POST kabul ediyorsa e-postanın
   yerine geçer — mimarinin en değerli iyileştirmesi bu olur.
2. Arayüzde FTP/SFTP yükleme var mı? Varsa video klip çıkışı mümkün olabilir.
3. Alarm e-postasının tam gövde biçimi ve kaç JPEG eklendiği.
4. Minimum "send interval" değeri ve alarm başına e-posta davranışı.
5. `voice prompt` için desteklenen ses dosyası biçimi, süresi ve tetikleme gecikmesi.
6. Periyodik health/test e-postası ayarı var mı, minimum periyodu ne?
7. SMTP TLS modu (SSL 465 / STARTTLS 587) ve sertifika doğrulama davranışı; firmware
   hangi TLS sürümlerini kabul ediyor?
7b. E-posta konusu (subject/title) alanı özelleştirilebiliyor mu? Mümkünse cihaz kimliği
   oraya da yazılır (SMTP AUTH'a ek ikinci sinyal).
8. Perimeter protection 4 kanalda mı (General Model) yoksa 2'de mi (Advanced Model)?
9. SMD Plus olay e-postası ile klasik motion e-postası ayırt edilebiliyor mu?
10. Cihaz saati NTP ile senkron mu; e-postadaki zaman damgası hangi timezone'da?
11. DoLynk Care ile uzaktan alarm/e-posta/AI ayarı değiştirilebiliyor mu? (14 cihazın
    bakımı buna bağlı — §2.4)
12. Test e-postası periyodu 30-60 dakikaya indirilebiliyor mu? (§13.1 hırsızlık tespiti)
13. Yeni firmware'de CGI / özel protokol erişimi ayrıca etkinleştirilmeyi gerektiriyor mu?
14. Hesap kilitleme politikası nedir (kaç hatalı denemede, ne kadar süre)? Probe ve
    event stream yeniden deneme aralıkları buna göre ayarlanır.
15. Servis hesabı için parola karmaşıklık kuralı nedir?
16. Konfigürasyon dışa/içe aktarma (`System → Backup/Import Config`) bu firmware'de
    çalışıyor mu ve içe aktarım hangi alanları geçersiz kılıyor? (§5.6 şablon akışı buna bağlı)

Bu sorular `scripts/dahua_probe.py` çıktısı + cihaz arayüzü ekran görüntüleriyle
yanıtlanır ve §24 bu dokümanda cevaplarla güncellenir.

---

# 25. KAYNAKLAR

1. DH-XVR5108HS-I3 datasheet (Rev 002.000, 2023-07-20) —
   https://material.dahuasecurity.com/uploads/cpq/prm-os-srv-res/smart/datasheetzipfiles/XVR5108HS-I3_V3_datasheet_20230720.pdf
2. Dahua XVR5108HS-I3 ürün sayfası —
   https://www.dahuasecurity.com/uk/products/All-Products/HDCVI-Recorders/WizSense-Series/I3-Series/5M-N/1080P-Series/XVR5108HS-I3
3. Önceki sürüm spesifikasyonu (edge gateway mimarisi) — `docs/legacy-siteguard-v1-spec.md`
4. Dahua CGI/HTTP API notları ve olay kodları — `docs/dahua-integration.md`
