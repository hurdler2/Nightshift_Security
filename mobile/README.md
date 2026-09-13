# Nightshift Security — Mobile (Flutter)

Android + iOS, tek kod tabanı (spec 14).

PHASE 0 durumu: proje kabuğu, dark security-console teması ve faz durum ekranı.
Ekranlar spec 41'deki listeye göre, sahiplenen fazlarda eklenir.

## Kurulum

```bash
flutter pub get
flutter run
flutter test
```

`android/` ve `ios/` platform klasörleri henüz oluşturulmadı; ilk ihtiyaç
duyulduğunda `flutter create . --platforms=android,ios` ile üretilir (Firebase
yapılandırması PHASE 7 ile birlikte gelir).

## Tasarım kuralları (spec 42)

- Dark mode security-console görünümü.
- Kritik alarm görsel olarak baskın.
- Tek dokunuşla ACK.
- Push'tan canlı görüntüye en fazla 2 dokunuş.
- Yavaş internet için önce thumbnail.
