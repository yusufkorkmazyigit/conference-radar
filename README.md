# 🎯 Akademik Konferans Takip Botu

Her Pazartesi ve Perşembe 09:00'da (TSİ) AI konferans ve çalıştaylarını tarar. Son başvuruya
10–60 gün kalanları prestij rozetleriyle birlikte Gmail'ine gönderir. Sunucu gerektirmez,
GitHub Actions üzerinde ücretsiz çalışır.

## Klasör yapısı

```
akademik-konferans-takip/
├── .github/workflows/conference_tracker.yml   # zamanlayıcı (GitHub Actions)
├── state/seen.json                            # daha önce bildirilen etkinlikler (bot günceller)
├── tracker.py                                 # botun kendisi
├── requirements.txt
├── .gitignore
└── README.md
```

## Kaynaklar

| Kaynak | Ne sağlar |
|---|---|
| huggingface/ai-deadlines | Ana AI konferansları, CORE/CCF sıralaması |
| ccfddl/ccf-deadlines | AI, veri madenciliği ve disiplinlerarası konferanslar, CORE sıralaması |
| paperswithcode/ai-deadlines | Eski liste (Papers with Code kapandığı için büyük ölçüde güncel değil; zarar vermez) |
| WikiCFP | Uzay, uydu, uzaktan algılama, robotik, evrimsel hesaplama gibi niş çalıştaylar |

Kaynaklardan biri çökerse bot çalışmaya devam eder. E-postanın altındaki "Kaynak durumu" satırı
hangi kaynağın çalıştığını gösterir.

## 5 dakikada kurulum

1. **Gmail App Password al.** Google hesabında 2 Adımlı Doğrulama açık olmalı.
   https://myaccount.google.com/apppasswords adresinden "Konferans Botu" adıyla bir şifre oluştur
   ve 16 haneli şifreyi kopyala.
2. **Repoyu aç.** GitHub'da **New repository** butonuna bas, adı `akademik-konferans-takip` yap
   ve **Create** de. Repo private da olabilir; bu iş için aylık ücretsiz dakika kotası fazlasıyla yeter.
3. **Dosyaları yükle.** **Add file → Upload files** ile `tracker.py`, `requirements.txt`,
   `README.md` ve `.gitignore` dosyalarını sürükle ve **Commit** et.
4. **Workflow dosyasını oluştur.** **Add file → Create new file** de. Dosya adı kutusuna
   `.github/workflows/conference_tracker.yml` yaz (eğik çizgiler klasörleri otomatik oluşturur),
   içeriği yapıştır ve **Commit** et.
5. **Secrets ekle.** **Settings → Secrets and variables → Actions → New repository secret**:
   - `GMAIL_ADDRESS`: Gmail adresin
   - `GMAIL_APP_PASSWORD`: 1. adımdaki 16 haneli şifre
   - `RECIPIENT_EMAIL` (isteğe bağlı): farklı bir adrese gitsin istiyorsan; virgülle birden fazla adres yazılabilir
6. **Yazma iznini kontrol et.** **Settings → Actions → General → Workflow permissions**
   altında "Read and write permissions" seçili olsun.
7. **Test et.** **Actions** sekmesine geç, **Akademik Konferans Takip Botu** workflow'unu seç,
   **Run workflow** butonuna bas. Önce `dry_run` kutusunu işaretleyip çalıştır; oluşan `email-preview`
   artifact'ını indirip incele. Sonra kutu boş halde tekrar çalıştır ve gelen kutunu kontrol et.

## Yerelde deneme

```bash
pip install -r requirements.txt
DRY_RUN=1 python tracker.py            # Linux/macOS
$env:DRY_RUN="1"; python tracker.py    # Windows PowerShell
```

Bu komut e-posta göndermez; `preview.html` dosyasını tarayıcıda açıp sonucu görebilirsin.

## Ayarlar (ortam değişkenleri)

| Değişken | Varsayılan | Açıklama |
|---|---|---|
| `MIN_DAYS` / `MAX_DAYS` | 10 / 60 | Son tarih aralığı (gün) |
| `MAX_EVENTS` | 80 | E-postadaki azami etkinlik sayısı |
| `WIKICFP_ENABLED` | 1 | WikiCFP taramasını kapatmak için 0 |
| `WIKICFP_PAGES` | 2 | Kategori başına taranan sayfa sayısı |
| `SEND_IF_EMPTY` | 0 | Etkinlik yokken de e-posta gelsin istiyorsan 1 |
| `BLOCK_KEYWORDS` | — | Virgülle ayrılmış, gizlenecek kelimeler |

WikiCFP kategorileri, amiral gemisi konferans listesi ve yağmacı yayıncı filtresi
`tracker.py` dosyasının başındaki **AYARLAR** bölümünde bulunur.

## Bilinmesi gerekenler

- GitHub zamanlanmış görevleri bazen birkaç dakika, yoğun saatlerde daha uzun süre gecikebilir.
- Bot her çalışmada `state/seen.json` dosyasını commit'ler. Bu commit'ler repoyu "aktif" tuttuğu için
  GitHub'ın hareketsiz repolarda zamanlanmış görevleri 60 gün sonra kapatması da önlenmiş olur.
- Yayıncı rozetleri (IEEE/ACM/Springer) etkinlik adından sezgisel olarak çıkarılır. WikiCFP
  kayıtlarını başvurmadan önce CORE portalında ve resmi sitede mutlaka doğrula.
