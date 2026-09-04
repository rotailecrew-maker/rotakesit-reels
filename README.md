# rotakesit — Instagram Reels otomasyonu

Google Drive'daki bir kuyruk klasöründen günde iki kez otomatik Reels paylaşır.
GitHub Actions üzerinde çalışır, sunucu gerektirmez.

```
Drive/QUEUE ──► indir ──► IG resumable upload ──► publish ──► Drive/PUBLISHED
                  │
                  └── hata ──► retry (yalnızca dosya hatalarında) ──► Drive/FAILED
```

| Dosya | Görev |
|---|---|
| `post_reel.py` | Ana akış — kuyruktan bir video alır, paylaşır, taşır |
| `refresh_token.py` | IG token'ının 60 günde ölmesini engeller |
| `.github/workflows/reels.yml` | Günde 2 paylaşım (09:00 / 18:00 TR) |
| `.github/workflows/keepalive.yml` | Haftalık: token yenileme + repo'yu canlı tutma |

---

## Kurulum

### 1. Repoyu GitHub'a gönderin

**Bu adım atlanamaz.** `.github/workflows/` yalnızca GitHub'a push edilmiş bir
repoda anlam taşır. Ayrıca zamanlanmış workflow'lar **sadece default branch'te**
tetiklenir — feature branch'e push etmek yetmez.

```bash
gh repo create rotakesit-reels --private --source=. --push
```

### 2. Instagram tarafı

Ön koşullar (biri eksikse API hiç çalışmaz):

- Instagram hesabı **Professional** (Business veya Creator) olmalı
- Bir **Facebook Sayfası'na bağlı** olmalı
- Meta uygulamanızda **Instagram Graph API** ürünü ekli olmalı
- Token'da şu izinler bulunmalı:
  `instagram_basic`, `instagram_content_publish`, `pages_read_engagement`,
  `pages_show_list`

**IG_USER_ID nasıl bulunur** (Sayfa ID'si değil, Instagram hesap ID'si):

```bash
curl "https://graph.facebook.com/v23.0/me/accounts?access_token=TOKEN"
# dönen page id ile:
curl "https://graph.facebook.com/v23.0/PAGE_ID?fields=instagram_business_account&access_token=TOKEN"
```

### 3. Google Drive tarafı

1. Google Cloud Console'da bir proje açın → **Drive API**'yi etkinleştirin
2. Bir **service account** oluşturun → JSON anahtarı indirin
3. Drive'da üç klasör açın: `QUEUE`, `PUBLISHED`, `FAILED`
4. **Üçünü de** service account e-postasına (`...@....iam.gserviceaccount.com`)
   **Editor** yetkisiyle paylaşın

> **Shared Drive kullanın.** Klasörler kişisel My Drive'daysa, script'in
> oluşturduğu `state.json` service account'a ait olur ve service account'ların
> depolama kotası olmadığı için `storageQuotaExceeded` alabilirsiniz.
> Shared Drive bu sorunu tamamen ortadan kaldırır (kod zaten
> `supportsAllDrives` gönderiyor).

> Yalnızca QUEUE paylaşılırsa `move_file` 404 verir. Üçü de gerekli.

Klasör ID'si = Drive URL'sindeki `/folders/` sonrası kısım.

### 4. Secret'ları girin

`Settings → Secrets and variables → Actions → New repository secret`

| Secret | Zorunlu | Açıklama |
|---|---|---|
| `IG_USER_ID` | ✅ | Instagram Business hesap ID'si |
| `IG_ACCESS_TOKEN` | ✅ | Long-lived access token |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | ✅ | JSON dosyasının **tamamı** |
| `DRIVE_QUEUE_FOLDER_ID` | ✅ | Üçü birbirinden **farklı** olmalı |
| `DRIVE_PUBLISHED_FOLDER_ID` | ✅ | |
| `DRIVE_FAILED_FOLDER_ID` | ✅ | |
| `IG_APP_ID` | ⭕ | Token yenileme için |
| `IG_APP_SECRET` | ⭕ | Token yenileme için |
| `GH_TOKEN` | ⭕ | Token'ı otomatik güncellemek için PAT |
| `NOTIFY_WEBHOOK` | ⭕ | Discord/Slack webhook — hata bildirimi |

> ⚠️ **Tanımsız bir secret hata vermez, boş string olur.** Bu yüzden script
> başlangıçta hepsini açıkça kontrol eder ve eksik olanı isimleriyle söyler.

Ayarlar için (secret değil) `Variables` sekmesi: `USER_TAGS`, `GRAPH_VERSION`,
`PUBLISHED_RETENTION_DAYS`, `REFRESH_BEFORE_DAYS`.

### 5. Test edin — Instagram'a bir şey göndermeden

`Actions → rotakesit reels → Run workflow → dry_run ✔`

DRY RUN modu tüm zinciri doğrular (secret'lar, Drive erişimi, klasör izinleri,
caption çözümleme, indirme bütünlüğü) ama Instagram'a **tek istek atmaz**.

Yerelde:

```bash
cp .env.example .env    # doldurun
set -a && . ./.env && set +a
DRY_RUN=1 python post_reel.py
```

---

## Kullanım

Videoyu QUEUE klasörüne atın. Bitti.

- **Caption**: aynı isimli `.txt` (harf duyarsız — `Video.MP4` → `video.txt` olur).
  Yoksa `DEFAULT_CAPTION` şablonu kullanılır.
- **Sıra**: doğal sıralama — `2.mp4`, `10.mp4` doğru sırada gider
  (düz alfabetik sıralama bunun tersini yapardı).
- **Limitler**: caption 2200 karaktere, hashtag 30'a otomatik kırpılır;
  1 GB üstü video indirilmeden reddedilir.

---

## Hata yönetimi — en önemli davranış

Retry sayacı **yalnızca dosyanın kendi hatalarında** artar:

| Hata türü | Örnek | Sayaç |
|---|---|---|
| `FileError` | Container ERROR, 1 GB aşımı, eksik indirme, IG spec reddi | **artar** |
| `TransientError` | Token dolmuş, rate limit, ağ kopması, Drive 403/5xx | **artmaz** |

Bu ayrım olmasaydı: token'ın dolduğu bir haftada her çalışma sıradaki videonun
bir retry hakkını yakar, 3 çalışmada **kusursuz bir video** FAILED'a sürülürdü.
Günde 2 çalışmayla 1.5 günde bir video kaybı demekti.

Geçici hata durumunda video kuyrukta sırasını korur; sorunu çözdüğünüzde
kaldığı yerden devam eder.

### Çift paylaşım koruması

`publish()` isteği Instagram'a ulaşıp yanıtı kaybolursa (timeout), script son 10
paylaşımı caption'a göre tarar. Reel gerçekten yayınlanmışsa başarı sayar —
aynı video ikinci kez paylaşılmaz.

---

## Token yenileme

IG long-lived token'ı **60 günde** ölür. `keepalive.yml` her pazartesi
`refresh_token.py` çalıştırır; ömrü 15 günün altına düştüğünde otomatik yeniler.

- `IG_APP_ID` + `IG_APP_SECRET` yoksa → yenileme atlanır, sadece uyarı
- `GH_TOKEN` (PAT, **Secrets: read and write** yetkili) varsa → yeni token
  secret'a şifreli olarak yazılır, tamamen otomatik
- `GH_TOKEN` yoksa → log "elle güncelleyin" der (token asla loglanmaz)

Elle kontrol:

```bash
python refresh_token.py --check
```

Token tamamen geçersizse yenileme işe yaramaz; Graph API Explorer'dan yeni token
üretmeniz gerekir.

---

## Bilinen sınırlar

- **Cron kayması.** GitHub zamanlanmış çalışmaları 5-30 dk geciktirir, yoğun
  saatlerde atlayabilir. Dakikası önemliyse harici bir tetikleyici
  (cron-job.org → `workflow_dispatch` API çağrısı) kullanın.
- **60 gün kuralı.** GitHub, commit almayan repolarda cron'u kapatır.
  `keepalive.yml` haftalık boş commit atarak bunu önler; workflow'u silmeyin.
- **PUBLISHED klasörü büyür.** `PUBLISHED_RETENTION_DAYS` variable'ını >0 yapın
  (örn. 90). Dosyalar kalıcı silinmez, Drive **çöp kutusuna** taşınır.
- **Video ön-doğrulaması yalnızca boyut.** Süre/codec/en-boy oranı IG tarafında
  reddedilir; bu bir `FileError` olduğu için 3 denemeden sonra FAILED'a gider.
- **IG paylaşım limiti** 24 saatte 50 post. Günde 2 ile sorun yok.
- **Graph API sürümü** `v23.0`. Meta sürümleri ~2 yılda emekliye ayırır;
  `GRAPH_VERSION` variable'ı ile güncelleyebilirsiniz.

---

## Sorun giderme

| Belirti | Sebep |
|---|---|
| `su ortam degiskenleri eksik veya bos` | Secret tanımlanmamış — isim birebir eşleşmeli |
| `Drive HTTP 404` | Klasör service account'a paylaşılmamış |
| `Drive HTTP 403` + `storageQuotaExceeded` | Klasörler My Drive'da; Shared Drive'a taşıyın |
| `[code=190]` | Token dolmuş → `refresh_token.py` |
| `Container ERROR` | Video IG spec'ine uymuyor (süre/codec/en-boy) |
| `state.json okunamadi` | Drive'daki `state.json` bozuk. Silin — geçmiş sıfırlanır ama kuyruk çalışır |
| Cron çalışmıyor | Workflow default branch'te mi? Repo 60 gündür sessiz mi? |
