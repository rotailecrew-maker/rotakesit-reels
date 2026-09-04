# rotakesit — Instagram Reels otomasyonu

Google Drive'daki hafta klasörlerinden günde iki kez otomatik Reels paylaşır.
GitHub Actions üzerinde çalışır, sunucu gerektirmez.

## Drive yapısı

```
RA 1/                        ← DRIVE_ROOT_FOLDER_ID
├── 1. Hafta/
│   ├── Reels/               ← videolar (kuyruk)
│   └── Capitons/            ← caption'lar, video adıyla aynı .txt
├── 2. Hafta/
│   └── ...
├── published/               ← yayınlanan videolar buraya taşınır
└── failed/                  ← 3 denemede yayınlanamayanlar
```

Hafta klasörleri **doğal sırayla** gezilir (`1. Hafta` → `2. Hafta` → `10. Hafta`).
Bir haftanın `Reels/` klasörü bitince otomatik olarak sonrakine geçer.
`published/` ve `failed/` isme göre bulunur, ID vermeye gerek yok.

**Caption dosyaları yerinde kalır.** `Capitons/` bir kütüphane gibi kullanılıyor;
sadece videolar hareket eder.

| Dosya | Görev |
|---|---|
| `post_reel.py` | Ana akış — kuyruktan bir video alır, paylaşır, taşır |
| `setup_oauth.py` | Bir kerelik Drive yetkilendirmesi |
| `refresh_token.py` | IG token'ının 60 günde ölmesini engeller |
| `.github/workflows/reels.yml` | Günde 2 paylaşım (09:17 / 18:17 TR) |
| `.github/workflows/keepalive.yml` | Haftalık: token yenileme + repo'yu canlı tutma |

---

## Neden service account değil de OAuth?

Service account bu senaryoda kullanılamıyor. Ölçüldü:

| İşlem | Service account | OAuth (kendi hesabınız) |
|---|---|---|
| Dosya oluşturma (`state.json`) | ❌ `storageQuotaExceeded` — kotası 0 | ✅ |
| Dosya taşıma | ❌ `cannotAddParent` | ✅ |
| Okuma / indirme | ✅ | ✅ |

Sebep: klasörler kişisel My Drive'da ve dosyaların sahibi siz. Service account
paylaşımlı Editor olduğu için Google ona `canMoveItemOutOfDrive: False` veriyor.
Shared Drive'da bu sorun yok ama Shared Drive Google Workspace gerektiriyor.

Kod hâlâ `GOOGLE_SERVICE_ACCOUNT_JSON`'u destekliyor (Shared Drive kullananlar
için), ama OAuth varsa onu tercih eder.

---

## Kurulum

### 1. Repoyu GitHub'a gönderin

**Bu adım atlanamaz.** `.github/workflows/` yalnızca GitHub'a push edilmiş bir
repoda anlam taşır. Zamanlanmış workflow'lar **sadece default branch'te** tetiklenir.

```bash
gh repo create rotakesit-reels --private --source=. --push
```

### 2. Instagram tarafı

Ön koşullar:

- Instagram hesabı **Professional** (Business veya Creator)
- Bir **Facebook Sayfası'na bağlı**
- Token izinleri: `instagram_basic`, `instagram_content_publish`,
  `pages_read_engagement`, `pages_show_list`

`IG_USER_ID` (Sayfa ID'si değil, Instagram hesap ID'si):

```bash
curl "https://graph.facebook.com/v23.0/me/accounts?access_token=TOKEN"
curl "https://graph.facebook.com/v23.0/PAGE_ID?fields=instagram_business_account&access_token=TOKEN"
```

### 3. Drive yetkilendirmesi (OAuth)

**a. OAuth consent screen** — GCP Console > APIs & Services > OAuth consent screen

- User type: **External**
- App name: `rotakesit-reels`, destek e-postası: kendi adresiniz
- Test users: kendi adresinizi ekleyin
- ⚠️ **Publishing status → PUBLISH APP (In production).**
  "Testing" durumunda kalırsa **refresh token 7 günde ölür**.
  "Doğrulanmamış uygulama" uyarısı normaldir — kendi uygulamanız.

**b. OAuth client** — APIs & Services > Credentials > Create credentials >
OAuth client ID > Application type: **Desktop app** → JSON'u indirin.

**c. Yetkilendirin:**

```bash
pip install -r requirements.txt
python setup_oauth.py "C:\yol\client_secret_....json"
```

Tarayıcı açılır, hesabınızı seçip izin verirsiniz. Refresh token `.env`
dosyasına yazılır, ekrana basılmaz.

**d. Drive API'yi etkinleştirin** (bir kez):
[console.cloud.google.com/apis/library/drive.googleapis.com](https://console.cloud.google.com/apis/library/drive.googleapis.com)

### 4. Secret'ları girin

`Settings → Secrets and variables → Actions`

```bash
python setup_oauth.py --github    # hangi değerlerin gireceğini listeler
```

| Secret | Zorunlu |
|---|---|
| `IG_USER_ID` | ✅ |
| `IG_ACCESS_TOKEN` | ✅ |
| `DRIVE_ROOT_FOLDER_ID` | ✅ Kök klasör (`RA 1`) |
| `GOOGLE_OAUTH_CLIENT_ID` | ✅ |
| `GOOGLE_OAUTH_CLIENT_SECRET` | ✅ |
| `GOOGLE_OAUTH_REFRESH_TOKEN` | ✅ |
| `IG_APP_ID` / `IG_APP_SECRET` | ⭕ Token yenileme |
| `GH_TOKEN` | ⭕ Token'ı otomatik güncellemek için PAT |
| `NOTIFY_WEBHOOK` | ⭕ Discord/Slack hata bildirimi |

> ⚠️ **Tanımsız bir secret hata vermez, boş string olur.** Script başlangıçta
> hepsini açıkça kontrol eder ve eksik olanı ismiyle söyler.

Ayarlar için `Variables` sekmesi: `USER_TAGS`, `GRAPH_VERSION`, `REFRESH_BEFORE_DAYS`.

### 5. Test edin — Instagram'a bir şey göndermeden

`Actions → rotakesit reels → Run workflow → dry_run ✔`

Yerelde:

```bash
set -a && . ./.env && set +a && DRY_RUN=1 python post_reel.py
```

DRY RUN hafta sırasını, seçilecek videoyu, caption'ı ve indirme bütünlüğünü
doğrular; Instagram'a **tek istek atmaz**.

---

## Kullanım

Videoyu ilgili haftanın `Reels/` klasörüne, caption'ı aynı adla `Capitons/`
klasörüne koyun. Bitti.

- **Caption eşleşmesi** harf duyarsız: `Faiz Nedir.mp4` → `faiz nedir.txt` de olur
- `Capitons/`'da yoksa videonun yanındaki `.txt`'ye, o da yoksa
  `DEFAULT_CAPTION` şablonuna düşer
- Caption 2200 karaktere, hashtag 30'a otomatik kırpılır
- 1 GB üstü video indirilmeden reddedilir

---

## Hata yönetimi — en önemli davranış

Retry sayacı **yalnızca dosyanın kendi hatalarında** artar:

| Hata türü | Örnek | Sayaç |
|---|---|---|
| `FileError` | Container ERROR (`status_code=ERROR`), 1 GB aşımı, eksik indirme | **artar** |
| `TransientError` | Token dolmuş, rate limit, ağ kopması, Drive 403/5xx, **yükleme hataları** | **artmaz** |

**Yükleme hataları neden dosya hatası sayılmaz:** `rupload.facebook.com` sadece
byte alır, video içeriğini doğrulamaz — içerik kontrolü sonraki adımda,
`status_code=ERROR` ile yapılır. Dolayısıyla bir yükleme hatası videonun bozuk
olduğunun kanıtı değildir. Meta'nın `ProcessingFailedError`'ı (`retriable:false`
dese bile) spec'i kusursuz videolarda da görülüyor. Yükleme patlarsa aynı çalışma
içinde sıfırdan yeni container ile tekrar denenir (`CONTAINER_ATTEMPTS`).

Bu ayrım olmasaydı: token'ın dolduğu bir haftada her çalışma sıradaki videonun
bir retry hakkını yakar, 3 çalışmada **kusursuz bir video** `failed/` klasörüne
sürülürdü. Günde 2 çalışmayla 1.5 günde bir video kaybı demekti.

### Çift paylaşım koruması

`publish()` isteği Instagram'a ulaşıp yanıtı kaybolursa (timeout), script son 10
paylaşımı caption'a göre tarar. Reel gerçekten yayınlanmışsa başarı sayar.

---

## Token yenileme

IG long-lived token'ı **60 günde** ölür. `keepalive.yml` her pazartesi
`refresh_token.py` çalıştırır; ömrü 15 günün altına düştüğünde otomatik yeniler.

- `IG_APP_ID` + `IG_APP_SECRET` yoksa → yenileme atlanır, sadece uyarı
- `GH_TOKEN` (**Secrets: read and write** yetkili PAT) varsa → yeni token
  secret'a şifreli yazılır, tamamen otomatik

```bash
python refresh_token.py --check
```

---

## Bilinen sınırlar

- **Cron kayması.** GitHub zamanlanmış çalışmaları geciktirir, yoğun saatlerde
  tamamen atlayabilir. Saat başı (`:00`) en kötü an — bu yüzden cron `:17`'ye
  alındı. Dakikası önemliyse harici tetikleyici (cron-job.org →
  `workflow_dispatch`) kullanın; `workflow_dispatch` kuyruğa girmez.
- **Üst üste tetikleyiciler.** Harici tetikleyici + cron birlikte kullanılırsa
  ikisi *farklı* videolar paylaşır ve günlük sayı ikiye katlanır — `state.json`
  bunu engellemez, o sadece aynı videonun tekrarını engeller. `MIN_INTERVAL_HOURS`
  (varsayılan 6) bunun için var: son paylaşımdan bu yana bu süre geçmediyse
  çalışma sessizce biter. Yedekli tetikleme bu sayede güvenli.
- **60 gün kuralı.** GitHub, commit almayan repolarda cron'u kapatır.
  `keepalive.yml` haftalık boş commit atarak önler — silmeyin.
- **OAuth consent "Testing" modu** refresh token'ı 7 günde öldürür.
  Mutlaka "In production" yapın.
- **Video ön-doğrulaması yalnızca boyut.** Süre/codec/en-boy IG tarafında
  reddedilir; `FileError` olduğu için 3 denemeden sonra `failed/` klasörüne gider.
- **IG paylaşım limiti** 24 saatte 50 post. Günde 2 ile sorun yok.

---

## Sorun giderme

| Belirti | Sebep |
|---|---|
| `su ortam degiskenleri eksik veya bos` | Secret tanımlanmamış — isim birebir eşleşmeli |
| `Drive kimlik bilgisi yok` | `setup_oauth.py` çalıştırılmamış |
| `storageQuotaExceeded` | Service account kullanıyorsunuz → OAuth'a geçin |
| `cannotAddParent` | Aynı sebep — service account My Drive'da taşıyamaz |
| `kok klasorde ... bulunamadi` | Kökte `published` / `failed` klasörü yok |
| `invalid_grant` | Refresh token ölmüş (consent "Testing" modunda mı?) |
| `[code=190]` | IG token dolmuş → `refresh_token.py` |
| `Container ERROR` | Video IG spec'ine uymuyor (süre/codec/en-boy) |
| `state.json okunamadi` | Kökteki `state.json` bozuk. Silin — geçmiş sıfırlanır |
| Cron çalışmıyor | Workflow default branch'te mi? Repo 60 gündür sessiz mi? |
