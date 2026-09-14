# Saha Devreye Alma Kılavuzu

Bu belge, şantiyeye giden kurulum ekibi içindir. Spec §5 neyin yapılacağını listeler;
burada **hangi sırayla** yapılacağı, her adımın **nasıl doğrulandığı** ve adım
tutmazsa **ne yapılacağı** vardır.

Kural: her adım bir sonrakine geçmeden doğrulanır. Doğrulanmamış adım, gece alarm
gelmediğinde nereye bakılacağını bilemediğiniz adımdır.

---

## 0. Sahaya gitmeden önce

Ofiste bitir. Sahada internet zayıf, hava kötü ve ışık az olur.

| Yapılacak | Nasıl |
|-----------|-------|
| Site kaydı açılır | `POST /v1/sites` — ad, IANA timezone (`Europe/Istanbul`) |
| Cihaz kaydı açılır | `POST /v1/sites/{id}/devices` — ad, model, seri no |
| SMTP kimlik bilgisi üretilir | `POST /v1/devices/{id}/smtp-account` |
| Kullanıcı hesapları açılır | Nöbetçiler GUARD, şantiye şefi SITE_MANAGER |

**SMTP parolası bir kez gösterilir.** Yanıt geldiğinde kaydet. Kaybedersen kurtarma
yok, yalnızca rotasyon var — aynı uç tekrar çağrılır, eskisi pasifleşir.

Yanına al: DVR servis hesabı parolası, SMTP kullanıcı/parola listesi (cihaz başına
ayrı), kanal adları listesi, dizüstü, ağ kablosu.

---

## 1. Temel cihaz ayarları

Sırası önemli: **saat ayarı her şeyden önce gelir**. Yanlış saatli bir cihaz gece
kuralına takılmaz ve bunu fark etmeniz haftalar alır.

```
[ ] Firmware sürümü not edildi        (kayıt: cihazın firmware_version alanı)
[ ] NTP açık, timezone Europe/Istanbul, saat doğru
[ ] admin parolası değiştirildi
[ ] Nightshift için ayrı servis hesabı açıldı (admin kullanılmaz)
[ ] IP kanal genişletmesi KAPALI      (açıksa tüm AI fonksiyonları ölür — §2)
[ ] Kayıt planı aktif, HDD sağlıklı
[ ] Router'da DVR'a port yönlendirmesi YOK
```

**Doğrulama:** cihazın saat ekranı ile telefonunuzun saati aynı dakikayı göstermeli.

---

## 2. AI modu — geri dönüşü olan tek seçim burada

XVR5108HS-I3 üç AI modundan **yalnızca birini** çalıştırır (§2.2):

| Mod | Ne verir | Ne kaybeder |
|-----|----------|-------------|
| SMD | 8 kanalda insan/araç hareketi | Perimeter (tripwire/intrusion) yok |
| **IVS & SMD** | 8 kanal SMD + 4 kanal perimeter | — |
| Face | Yüz tespit/tanıma | SMD ve perimeter yok |

**Seçim: IVS & SMD.** Face modu ürün politikası gereği kullanılmaz (§19.3); sistem
yüz olayı gelse bile reddeder, işlemez ve saklamaz.

```
[ ] AI Mode = IVS & SMD
[ ] SMD Plus ilgili tüm kanallarda açık, hedef = Human (araç kararı sahaya göre)
[ ] Perimeter protection en kritik 4 kanalda (tripwire ve/veya intrusion)
[ ] Perimeter kurallarında hedef filtresi Human/Vehicle seçili
[ ] Hassasiyet ayarlandı
```

Hangi 4 kanal? Hırsızın **girmek zorunda olduğu** yerler: kapı, kırık çit, malzeme
istifi. Manzara güzel diye seçilmez.

---

## 3. E-posta ayarı — sistemin can damarı

Bu adım tutmazsa hiçbir şey çalışmaz. Alarm bize yalnızca e-posta ile ulaşır.

```
SMTP sunucusu : (kurulum ekibine verilen adres)
Port          : 587      ← önce bunu dene
Şifreleme     : STARTTLS / TLS
Kullanıcı     : dev-xxxxxxxxxxxx    (cihaza özel, ofiste üretildi)
Parola        : (cihaza özel)
Gönderen      : herhangi bir adres olabilir, kimlik AUTH'tan gelir
Alıcı         : hesapla birlikte verilen adres
Attach picture: İŞARETLİ
Send interval : 60 saniye veya altı
Health mail   : periyodik açık
```

**Port 25 asla kullanılmaz** — mobil operatörler engeller. 587 çalışmazsa 465
(implicit TLS), o da olmazsa 2525 denenir.

**Doğrulama:** cihazın kendi "Test" düğmesi yeşil dönmeli. Dönmezse §7'deki tabloya
bak — sorun neredeyse her zaman DNS, parola veya port.

---

## 4. Alarm linkage

Olay algılandığında cihazın ne yapacağı:

```
[ ] Snapshot açık            (fotoğraf olmadan alarm yarım bilgidir)
[ ] Record açık, pre-record ayarlı
[ ] Email açık               (bize ulaşan tek yol)
[ ] Voice prompt açık, ses dosyası yüklü   (caydırıcı — §6)
[ ] Buzzer tercihe göre
```

Cihazda FTP yükleme seçeneği yoktur; e-posta tek kanaldır (§2).

---

## 5. Kanal adlandırma

Kanal adı e-posta gövdesinde geliyorsa, alarmı doğru kameraya bağlayan anahtardır.

Her kanala **kararlı ve tekil** bir ad ver: `KAMERA-03 Depo Arka`. Aynısını
`POST /v1/devices/{id}/cameras` ile sisteme gir — `channel_number` cihazdaki kanal
numarasıyla birebir aynı olmalı.

Bir kanal numarasına yalnızca bir kamera kaydedilebilir; ikincisi 409 döner.

---

## 6. Test alarmı ve doğrulama

Şimdi asıl an. Cihazda bir test alarmı tetikle (kameranın önünden geç), sonra:

```bash
curl -X POST https://<api>/v1/devices/<device_id>/test-alarm \
  -H "Authorization: Bearer <token>"
```

Cevabın üç hali vardır:

**a) `verified: true`** — mesaj geldi, ayrıştırıldı, kanal numarası çıktı. Bu cihaz
tamam. Sonraki adıma geç.

**b) `verified: false`, "no message from this recorder"** — hiçbir şey gelmedi.
E-posta ayarına dön (§3). En sık sebepler:

| Belirti | Sebep | Çözüm |
|---------|-------|-------|
| Cihaz testi de başarısız | DNS çözülmüyor | DVR'a sabit DNS: 1.1.1.1 / 8.8.8.8 |
| Cihaz testi başarısız, "auth failed" | Parola yanlış veya hesap rotasyona uğramış | Kimlik bilgisini yeniden üret, cihaza gir |
| Cihaz testi başarılı, mesaj yok | Alarm linkage'da e-posta işaretli değil | §4'e dön |
| Ara ara geliyor | Send interval çok uzun | 60 sn veya altına çek |

**c) `verified: false`, "the message arrived but produced no event"** — mesaj geldi
ama formatı tanımıyoruz. Bu bir hata değil, beklenen durum: ayrıştırma profili
gerçek cihazdan gelen ilk mesajla kesinleşir. Ham örneği al ve geliştirmeye gönder:

```bash
curl "https://<api>/v1/devices/<id>/email-samples?include_raw=true&limit=3" \
  -H "Authorization: Bearer <token>"
```

Profil güncellenene kadar o cihazın alarmları olay üretmez; mesajlar kaydedilir,
hiçbiri kaybolmaz.

---

## 7. Bölgeler ve kural

Kamera görüntüsü üzerine bölge çiz. Koordinatlar **0–1 arası normalize** edilir —
piksel değeri kabul edilmez, çünkü çözünürlük değişince bölge kayar.

```bash
PUT /v1/cameras/{id}/zones
[{"name": "Depo Yasak", "zone_type": "RESTRICTED",
  "points": [[0.5,0.2],[0.95,0.2],[0.95,0.9],[0.5,0.9]]}]
```

Bölgeler **küme olarak** değişir: gönderdiğin liste o kameranın tüm bölgeleridir.

Gece kuralı:

```bash
POST /v1/rules
{"name": "Gece insan alarmı", "site_id": "...",
 "event_types": ["person_detected"],
 "schedule": {"timezone": "Europe/Istanbul", "start": "19:00", "end": "07:00"},
 "min_severity": "MEDIUM", "actions": ["push", "snapshot"]}
```

`19:00–07:00` gece yarısını geçer; pencere **açıldığı güne** aittir.

---

## 8. Telefonlar

Her nöbetçi uygulamadan giriş yapar; uygulama telefonu kendisi kaydeder
(`POST /v1/push-devices`). Kayıt olmamış telefon alarm almaz.

**Doğrulama:** kameranın önünden geç, telefon çalmalı. Çalmıyorsa alarm kaydına bak —
`alarm_deliveries` her denemeyi tutar, "bildirim gelmedi" sorusunun cevabı oradadır.

---

## 9. Sessizlik kontrolü

```bash
curl https://<api>/v1/devices/<id>/silence-state -H "Authorization: Bearer <token>"
```

Kurulum biter bitmez `NEVER_SEEN` değil `OK` görmelisin. `NEVER_SEEN` kalıyorsa cihaz
bize hiç ulaşmamış demektir — §6'ya dön.

Watchdog'un anlamı: kaydedici susarsa bu bir arıza **ya da** hırsızlıktır, ikisi
dışarıdan aynı görünür. Bu yüzden susma asla sessizce geçilmez.

---

## 10. Filoya yayılım — 14 cihaz

14 cihazı birden yapılandırmak, bir hatayı 14 kez yapmaktır.

1. **Bir cihazı** kusursuz yapılandır ve yukarıdaki her adımı doğrula.
2. Konfigürasyonu dışa aktar (`System → Backup/Import Config`).
3. **3 cihaza** uygula, bir gece bekle, alarmları ve yanlış pozitifleri gözden geçir.
4. Sorun yoksa kalan 10 cihaza yay.

Her cihazda yalnızca cihaza özel farkları ayarla:

```
cihaz adı                (e-posta gövdesinde görünür)
SMTP kullanıcı + parola  (cihaza özel — paylaşılmaz)
kanal adları
IVS/perimeter geometrisi (o sahanın görüntüsüne göre)
ağ ayarları
```

**Konfigürasyon şablonu parola içerir.** Sır muamelesi gör: şifreli sakla, repoya
koyma, kurulum bitince sil.

Filonun tamamı aynı model ve firmware olduğu için bir firmware güncellemesi 14 cihazı
birden bozabilir (§2.4b). Güncelleme yapılacaksa önce tek cihazda denenir.

---

## Bilinen sınırlar — müşteriye baştan söylenir

| Beklenti | Gerçek |
|----------|--------|
| Uygulamadan canlı izleme | V1'de yok. Saha CGNAT arkasında, DVR'a içeriden ulaşılamaz. Canlı görüntü DMSS uygulamasına yönlendirilir (§17). |
| Anında bildirim | Uçtan uca ~10–30 sn. Gecikmenin çoğu cihazın e-postayı hazırlayıp göndermesinde. |
| Yüz tanıma | Kullanılmaz, kapalıdır ve gelirse reddedilir (§19.3). |
| Video klip | V1'de yok; olay başına tek fotoğraf. |

Bu satırlar sözleşme öncesi konuşulur. Kurulumdan sonra öğrenilen sınır, sınır değil
şikâyettir.
