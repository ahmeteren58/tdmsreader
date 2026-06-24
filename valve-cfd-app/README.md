# Vana CFD Kataloğu

Çeşitli vanaların CFD analiz sonuçlarını **hızlıca bulmak, karşılaştırmak ve sunuma dönüştürmek** için tasarlanmış, tamamen **sunucusuz / kurulumsuz / internetsiz** çalışan bir masaüstü web uygulaması.

> Bu bir **prototip**tir. Amacı, yaklaşımı denemen ve yol haritasını birlikte netleştirmemizdir.

---

## Neden bu yaklaşım?

İhtiyaç: yerel ağdaki ortak bir bilgisayarda, internet olmadan, IT'den izin/port/kurulum istemeden çalışacak bir uygulama.

Bu yüzden uygulama:

- **Kurulum istemez** — Python, Node, sunucu, veritabanı YOK.
- **İnternet istemez** — tüm kütüphaneler (`assets/vendor/`) yerele gömülüdür.
- **IT izni istemez** — sadece ortak ağ klasörüne dosya koymak yeterli; port/firewall yok.
- **Tek noktadan güncellenir** — veriyi/görseli klasörde değiştir, herkes güncelini görür.

---

## Nasıl çalıştırılır?

1. `valve-cfd-app` klasörünün tamamını ortak ağ klasörüne kopyala
   (örn. `\\sunucu\paylasim\VanaCFD\`).
2. `index.html` dosyasına **çift tıkla** — varsayılan tarayıcıda açılır. Hepsi bu.

> İpucu: Kullanıcılar masaüstüne `index.html`'e bir kısayol koyabilir.

---

## Özellikler

- **Filtre + Arama:** vana tipi, ölçü (DN), akışkan, açıklık (%) aralığı, kavitasyon riski; serbest metin arama.
- **Kart görünümü:** her vana için anahtar metrikler (Cv, ΔP, K, kavitasyon σ) ve kontur küçük resmi.
- **Detay ekranı:** tüm çalışma koşulları + CFD sonuçları, görsel galerisi (tıkla-büyüt), notlar.
- **Karşılaştırma:** 2+ vanayı seç → yan yana tablo + Cv ve K faktörü grafikleri.
- **Sunum üretimi (PPTX):** seçtiğin vanalardan **tek tıkla PowerPoint** dosyası — tamamen tarayıcıda, offline.
- **Veri yükleme:** Excel/CSV'yi sürükle-bırak → `data.js` üretir (Python/araç gerekmez).

---

## Veri nasıl güncellenir?

İki yöntem var:

### A) Excel/CSV ile (önerilen)

1. `loader.html`'i aç (uygulamadaki **"Veri Yükle"** butonu).
2. **CSV Şablonu İndir** → her vana için bir satır doldur.
3. Doldurulmuş dosyayı sürükle-bırak → uygulama doğrular ve `data.js` üretir.
4. `data.js`'i indir, `assets/data.js`'in üzerine yaz.

### B) Elle düzenleme

`assets/data.js` içindeki `window.VALVE_DATA` dizisini doğrudan düzenleyebilirsin.

### Görseller

CFD görsellerini (PNG/JPG/SVG) `images/` klasörüne koy ve veri satırında yolunu yaz
(örn. `images/btf_dn100_pressure.png`). Her kayıt 3 görsele kadar destekler
(basınç konturu, hız konturu, akım çizgileri — istediğin gibi).

> Prototipteki örnek görseller temsilîdir (SVG placeholder). Gerçek CFD çıktılarınla değiştir.

---

## Veri alanları

| Alan | Açıklama | Zorunlu |
|------|----------|:------:|
| `id` | Benzersiz kimlik | ✓ |
| `name` | Görünen ad | ✓ |
| `type` | Vana tipi (Kelebek, Küresel, Glob, Kontrol, Çek…) | ✓ |
| `dn` | Ölçü (mm) | ✓ |
| `pressureClass` | Basınç sınıfı (PN16…) | |
| `opening` | Açıklık (%) | |
| `fluid`, `temperature` | Akışkan, sıcaklık (°C) | |
| `inletPressure`, `outletPressure` | Giriş/çıkış basıncı (bar) | |
| `flowRate`, `reynolds` | Debi (m³/h), Reynolds | |
| `cv`, `kv` | Akış katsayıları | |
| `deltaP` | Basınç düşümü (bar) | |
| `kFactor` | Kayıp katsayısı K (ζ) | |
| `cavitationIndex` | Kavitasyon indeksi σ (<1.5 riskli) | |
| `torque`, `massFlow` | Tork (Nm), kütle debisi (kg/s) | |
| `image1..3` (+`_label`) | Görsel yolları ve etiketleri | |
| `analysisDate`, `notes` | Analiz tarihi, notlar | |

---

## Klasör yapısı

```
valve-cfd-app/
├── index.html         # Ana katalog uygulaması
├── loader.html        # Excel/CSV → data.js dönüştürücü
├── README.md
├── assets/
│   ├── app.css
│   ├── app.js
│   ├── data.js        # Veri (buradan okunur)
│   └── vendor/        # Offline kütüphaneler (Chart.js, PptxGenJS, SheetJS)
├── images/            # CFD görselleri
└── tools/
    └── gen_sample_images.py  # Örnek görsel üretici (yalnız prototip için)
```

---

## Notlar / sınırlamalar (prototip)

- Veri salt-okunur olarak `data.js`'ten yüklenir; uygulama içinden düzenleme yoktur (yol haritasında değerlendirilebilir).
- `file://` ile açıldığında tarayıcılar harici dosya okumayı kısıtladığı için veri JSON değil `data.js` olarak tutulur (bu yüzden loader `data.js` üretir).
- Çok büyük görsellerde PPTX üretimi yavaşlayabilir; gerçek görseller makul boyutta (ör. <500 KB) tutulmalı.

---

## Sonraki adımlar için fikirler (yol haritası)

- Şirket PowerPoint şablonu ile marka uyumlu çıktı.
- PDF rapor dışa aktarma.
- Vana ailesi bazlı eğriler (Cv–açıklık, K–Reynolds) detay ekranında.
- Etiket/arama geçmişi, favoriler.
- İstenirse Streamlit/tam web sürümüne taşıma (çok kullanıcılı düzenleme gerekirse).
