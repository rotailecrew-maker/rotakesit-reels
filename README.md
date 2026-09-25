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

Kökte **yalnızca hafta klasörü gibi görünenler** kuyruğa girer: adı sayıyla
başlayan (`1. Hafta`, `10.Hafta`) ya da `Hafta 3` biçimindekiler. `Arşiv`,
`Taslak`, `Ham Çekim` gibi klasörler atlanır ve logda adıyla belirtilir —
içlerindeki videolar yanlışlıkla paylaşılmaz.

**Caption dosyaları yerinde kalır.** `Capitons/` bir kütüphane gibi kullanılıyor;
sadece videolar hareket eder.

| Dosya | Görev |
|---|---|
| `post_reel.py` | Ana akış — kuyruktan bir video alır, paylaşır, taşır |
| `setup_oauth.py` | Bir kerelik Drive yetkilendirmesi |
| `refresh_token.py` | IG token'ının 60 günde ölmesini engeller |
| `check_credentials.py` | Günlük ömür + paylaşım sağlığı denetimi — sessiz ölümü önler |
| `.github/workflows/reels.yml` | Saat başı denenir, pencere içinde paylaşır (09-14 / 17-22 TR) |
| `.github/workflows/keepalive.yml` | Günlük: token yenileme + sağlık denetimi; haftalık keepalive commit'i |
| `.github/workflows/test.yml` | Push/PR'da birim testleri (`tests/`) |

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
| `NOTIFY_WEBHOOK` | ⭕ Discord/Slack hata bildirimi — **şiddetle önerilir** |

> ⚠️ **Tanımsız bir secret hata vermez, boş string olur.** Script başlangıçta
> hepsini açıkça kontrol eder ve eksik olanı ismiyle söyler.

> ⚠️ `NOTIFY_WEBHOOK` yoksa tek bildirim kanalı GitHub'ın "workflow failed"
> e-postasıdır. Kuyruk bittiğinde, paylaşım durduğunda veya token ölmeye
> yaklaştığında haberiniz olsun diye bir Discord/Slack webhook'u tanımlayın.

Ayarlar için `Variables` sekmesi (hepsi opsiyonel, boşsa varsayılan kullanılır):

| Variable | Varsayılan | Ne işe yarar |
|---|---|---|
| `POST_WINDOWS` | `9-14,17-22` | Paylaşım pencereleri (TR, bitiş saati dahil) |
| `MIN_INTERVAL_HOURS` | `2` | İki paylaşım arası en az süre |
| `USER_TAGS` | `rota,rotaile,ramedyaresmi` | Reel'de etiketlenecek hesaplar |
| `DEFAULT_CAPTION` | bkz. `post_reel.py` | `.txt` yoksa kullanılan şablon (`{name}`) |
| `MAX_RETRIES` | `3` | Dosya hatasında `failed/`'a kadar deneme |
| `MAX_TRANSIENT_RETRIES` | `6` | Videoya bağlı geçici hatada ertelemeye kadar deneme |
| `GRAPH_VERSION` | `v23.0` | Graph API sürümü |
| `REFRESH_BEFORE_DAYS` | `15` | IG token'ı kaç gün kala yenilensin |
| `STALE_POST_HOURS` | `36` | Son paylaşım bundan eskiyse alarm |
| `QUEUE_CRITICAL` | `4` | Kuyrukta bu kadar video kalınca alarm |

### 5. Test edin — Instagram'a bir şey göndermeden

`Actions → rotakesit reels → Run workflow → dry_run ✔`

Elle **canlı** paylaşım: `Run workflow`'da `enforce_window` kutusu varsayılan
olarak işaretlidir (pencere dışında hiçbir şey yapmaz). Hemen paylaşmak için
kutuyu kaldırın.

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
- Caption 2200 karaktere, hashtag 30'a otomatik kırpılır (emoji 2 karakter
  sayılır; UTF-8, UTF-16 ve Windows-1254 `.txt` okunur)
- 1 GB üstü video indirilmeden reddedilir
- Drive kısayolları indirilemediği için atlanır (logda uyarı) — gerçek dosyayı koyun
- Yayınlanmış bir videoyu kuyruğa geri koyarsanız otomatik olarak tekrar
  `published/`'a taşınır. Yeniden paylaşmak için `state.json`'dan kaydını silin.
- Kuyruk ilk kez boşaldığı gün çalışma **bir kez başarısız** olur (bildirim
  gitsin diye); `check_credentials.py` de 4 video kalınca uyarır

---

## Hata yönetimi — en önemli davranış

Retry sayacı **yalnızca dosyanın kendi hatalarında** artar:

| Hata türü | Örnek | Sonuç |
|---|---|---|
| `FileError` | Container ERROR (`status_code=ERROR`), 1 GB aşımı, IG spec reddi | `retries` **artar** → 3'te `failed/` |
| `FileTransientError` | **Yükleme hataları**, işleme zaman aşımı, indirme hatası/eksik indirme, Graph `code=100`, çözülemeyen 4xx | `retries` artmaz; `transient_retries` artar → 6'da video **24 saat ertelenir** |
| `TransientError` | Token dolmuş, rate limit, paylaşım limiti, ağ kopması, Drive 403/5xx | hiçbir sayaç artmaz |

**Neden erteleme:** kuyruk her zaman sıradaki ilk videoyu seçer. Videoya bağlı
bir hata kalıcıysa (bozuk yükleme, adı sorunlu dosya) eskiden o video sonsuza
kadar denenir, arkasındaki hiçbir video paylaşılmazdı. Artık üst üste 6 kez
(`MAX_TRANSIENT_RETRIES`, pencere içinde ~yarım gün) aynı video patlarsa 24
saat (`DEFER_HOURS`) kenara alınır ve sıradaki videoya geçilir. `failed/`'a
taşınmaz, retry hakkı yanmaz; süre dolunca sırasına geri döner.

Drive çağrıları 5xx/429/ağ kopmalarında otomatik olarak 4 kez tekrar denenir
(`DRIVE_RETRIES`).

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

`publish()`'ten **hemen önce** container id'si `state.json`'a "bekleyen" olarak
yazılır. Sonra ne olursa olsun:

- **Yanıt kaybolursa** (timeout): container'a sorulur. `status_code=PUBLISHED`
  ise reel yayınlanmış sayılır. IG'ye ulaşılamazsa son paylaşımlar caption'a
  göre taranır; o da olmazsa karar sonraki çalışmaya bırakılır.
- **Çalışma yarıda ölürse** (timeout, iptal) ya da **paylaşım sonrası
  `state.json` yazılamazsa**: sonraki çalışma, paylaşım yapmadan önce bekleyen
  container'ı IG'ye sorar. Yayınlanmışsa işaretler ve videoyu `published/`'a
  taşır. IG'ye ulaşamazsa o çalışmada **hiç paylaşım yapmaz**.
- `state.json` yazılamasa bile video `published/`'a taşınmaya çalışılır;
  kuyruktan çıkan video tekrar seçilemez.

Çalışma kendi süresini 35 dk ile sınırlar (`RUN_BUDGET_SECONDS`). Süre azalınca
yeni yükleme/container denemesi başlatmaz, böylece GitHub'ın 45 dk'lık
timeout'u işi yarıda kesmez.

---

## Token yenileme

IG long-lived token'ı **60 günde** ölür. `keepalive.yml` her gün
`refresh_token.py` çalıştırır; ömrü 15 günün altına düştüğünde otomatik yeniler.
`expires_at` bildirmeyen (süresiz) token'larda yenileme atlanır.

- `IG_APP_ID` + `IG_APP_SECRET` yoksa → yenileme atlanır, sadece uyarı
- `GH_TOKEN` (**Secrets: read and write** yetkili PAT) varsa → yeni token
  secret'a şifreli yazılır, tamamen otomatik

```bash
python refresh_token.py --check
```

---

## Kimlik bilgileri ve ömürleri

Bu otomasyonun bütün sessiz ölüm sebepleri burada. `check_credentials.py`
her gün çalışır (`keepalive.yml`); eşiğe yaklaşan bir şey varsa iş **başarısız
olur**, böylece GitHub maili ve `NOTIFY_WEBHOOK` bildirimi tetiklenir.

Kimlik bilgilerine ek olarak Drive'daki `state.json`'dan **paylaşım sağlığını**
da denetler:

- **Son paylaşım 36 saatten eskiyse** (`STALE_POST_HOURS`): tetikleyiciler
  durmuş (cron-job.org işi kapatmış, PAT'i dolmuş) ya da her çalışma hata veriyor
- **Kuyrukta 4 veya daha az video kaldıysa** (`QUEUE_CRITICAL`, ~2 gün)

Keepalive commit'i önceki adımlar başarısız olsa da atılır. Eskiden kimlik
denetimi kırmızıya döndüğü an commit de duruyordu; sorun 60 gün çözülmezse
GitHub zamanlanmış workflow'ları kapatır, alarmlar da susardı.

| Kimlik | Ömür | Yenileme |
|---|---|---|
| `IG_ACCESS_TOKEN` (`expires_at`) | 60 gün | ✅ otomatik — `refresh_token.py` |
| `IG_ACCESS_TOKEN` (`data_access_expires_at`) | **90 gün** | ❌ **elle** — aşağıya bakın |
| `GOOGLE_OAUTH_REFRESH_TOKEN` | süresiz\* | — |
| `GH_TOKEN` (PAT) | seçtiğiniz süre | ❌ elle |
| `IG_APP_SECRET`, `GOOGLE_OAUTH_CLIENT_SECRET` | süresiz | — |

\* **Yalnızca** OAuth consent screen **In production** ise. `Testing` modunda
kalırsa refresh token verilişinden **7 gün** sonra ölür. Kontrol:
[console.cloud.google.com/auth/audience](https://console.cloud.google.com/auth/audience)

### 90 günlük Instagram adımı (kaçınılmaz)

`data_access_expires_at`, Facebook'un ayrı bir sayacıdır ve **token yenilemeyle
uzamaz** — ölçüldü: `fb_exchange_token` sonrası tarih aynı kalıyor. Yalnızca
Facebook giriş ekranından yeniden yetki verilince sıfırlanır.
App Review'dan geçmemiş uygulamalar için bu 90 günlük sınır kaldırılamıyor.

90 günde bir yapılacak (`check_credentials.py` 21 gün kala uyarır, 10 gün kala
işi kırmızıya çevirir):

1. [Graph API Explorer](https://developers.facebook.com/tools/explorer/) →
   uygulamayı seçin → **Generate Access Token** → izinleri onaylayın.
2. ⚠️ **Çıkan token'ı doğrudan secret'a yazmayın** — bu token **kısa ömürlüdür
   (~1-2 saat)**; secret'a yazılırsa paylaşım aynı gün durur. Önce yerel
   `.env`'deki `IG_ACCESS_TOKEN` satırına yapıştırın.
3. Uzun ömürlüye (60 gün) çevirin:
   ```bash
   set -a && . ./.env && set +a && python refresh_token.py
   ```
   Token ömrü 15 günün altında olduğu için script onu uzun ömürlü token'la
   değiştirir ve `.env`'e yazar. `GH_TOKEN` (Secrets: read and write) `.env`'de
   varsa GitHub secret'ı da otomatik güncellenir.
4. `GH_TOKEN` yoksa `.env`'deki **yeni** `IG_ACCESS_TOKEN` değerini
   `IG_ACCESS_TOKEN` secret'ına yazın.
5. Doğrulayın: `python check_credentials.py --report` — `expires_at` ~60 gün,
   `data_access_expires` ~90 gün göstermeli.

```bash
python check_credentials.py --report    # her zaman rapor, exit 0
python check_credentials.py             # eşik altındaysa exit 1
```
---

## Bilinen sınırlar

- **GitHub cron güvenilmez — buna göre tasarlandı.** GitHub zamanlanmış
  çalışmaları rastgele düşürüyor; bu repoda ilk iki deneme (`:00` ve `:17`)
  hiç tetiklenmedi (API'de `event=schedule` toplam 0). Bu yüzden cron **saat
  başı** çalışır ve paylaşımı `POST_WINDOWS` (varsayılan `9-14,17-22` TR)
  belirler. Ölçüldü: GitHub tetiklemelerin ~%39'unu çalıştırıyor, aralıklar 2-5 saat.
  6 saatlik pencerede 6 şans olur, biri tutunca pencere
  kapanır. Pencere dışı çalışmalar Drive/IG'ye hiç dokunmadan ~5 saniyede
  biter; genel repolarda Actions dakikaları ücretsiz. Güvenilirlik için
  harici tetikleyici ekleyin (aşağıda).
- **Üst üste tetikleyiciler.** Harici tetikleyici + cron birlikte kullanılırsa
  ikisi *farklı* videolar paylaşabilir — `state.json` bunu engellemez, o sadece
  aynı videonun tekrarını engeller. Bunu `posted_in_window` (pencere başına
  bir paylaşım) ve `MIN_INTERVAL_HOURS` (varsayılan 2) önler; aynı concurrency
  grubu da iki çalışmanın çakışmasını engeller. Yedekli tetikleme bu sayede güvenli.
- **60 gün kuralı.** GitHub, commit almayan repolarda cron'u kapatır.
  `keepalive.yml` haftada bir commit atarak önler — silmeyin.
- **Yerelden canlı çalıştırma** (`python post_reel.py`, `DRY_RUN` olmadan)
  GitHub'ın concurrency grubunun dışındadır; aynı anda Actions da çalışırsa
  iki paylaşım olabilir. Yerelde `DRY_RUN=1` kullanın.
- **OAuth consent "Testing" modu** refresh token'ı 7 günde öldürür.
  Mutlaka "In production" yapın.
- **Video ön-doğrulaması yalnızca boyut.** Süre/codec/en-boy IG tarafında
  reddedilir; `FileError` olduğu için 3 denemeden sonra `failed/` klasörüne gider.
- **IG paylaşım limiti** 24 saatte 50 post. Günde 2 ile sorun yok.
- **Pencereler gece yarısını aşamaz.** `22-2` yerine `22-23,0-2` yazın.
  Geçersiz parçalar uyarıyla atlanır; hiç geçerli pencere kalmazsa çalışma
  başarısız olur (eskiden sessizce hiç paylaşmıyordu).
- **Graph API sürümü** (`v23.0`) Meta'nın takvimine göre yaklaşık 2027 ortasında
  emekliye ayrılır; öncesinde `GRAPH_VERSION` variable'ı ile yükseltip DRY RUN
  ile deneyin.
- **Repo public** olduğu için Actions logları herkese açık: sıradaki video
  adları ve DRY RUN'daki caption görünür. Secret'lar maskelenir.

---

## Harici tetikleyici (cron-job.org)

GitHub cron'u tek başına güvenilmez; aynı işi dışarıdan saat başı tetikleyin.

**Önerilen: `workflow_dispatch` + yalnızca "Actions: write" yetkili PAT.**

1. GitHub → Settings → Developer settings → **Fine-grained token** →
   yalnızca bu repo → Repository permissions → **Actions: Read and write**
   (başka hiçbir yetki yok).
2. cron-job.org'da saat başı bir iş:
   - `POST https://api.github.com/repos/<kullanıcı>/rotakesit-reels/actions/workflows/reels.yml/dispatches`
   - Başlıklar: `Authorization: Bearer <PAT>`, `Accept: application/vnd.github+json`
   - Gövde: `{"ref": "main"}` → yanıt `204`
3. `enforce_window` girdisi varsayılan olarak **açık** olduğu için pencere
   kuralı geçerlidir; gece hiçbir şey paylaşılmaz.

**Neden `repository_dispatch` değil:** o uç nokta PAT'te **Contents: write**
ister. Bu yetki repoya kod push edebilir; üçüncü taraftaki token sızarsa
saldırgan workflow'u değiştirip bütün secret'ları (tüm Drive'a erişen Google
token'ı dahil) çalabilir. "Actions: write" ise yalnızca workflow tetikler, kodu
değiştiremez. `repository_dispatch` geriye uyumluluk için hâlâ destekleniyor;
geçiş yaptıktan sonra eski PAT'i silin.

> Harici tetikleyicinin PAT'i `GH_TOKEN` secret'ından **ayrıdır** ve süresi
> dolarsa `check_credentials.py` bunu doğrudan göremez. Ama paylaşımlar
> durursa "son paylaşım 36 saatten eski" alarmı devreye girer.

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
| `kokte N adet state.json var` | Fazla kopyaları silin; en yeni değiştirilen kullanılıyor |
| Cron çalışmıyor | Workflow default branch'te mi? Repo 60 gündür sessiz mi? |
| `KUYRUK BOS` | Hafta klasörlerine video ekleyin (günde bir kez bildirilir) |
| `Video 24 saat ERTELENDI` | Aynı video üst üste 6 kez geçici hata verdi; logdaki son hataya bakın |
| `Hafta klasoru sayilmadi` | Klasör adı sayıyla başlamıyor / `Hafta N` değil — bilerek mi? |
| `bekleyen paylasim dogrulanamadi` | Önceki publish'in sonucu IG'ye sorulamadı (ağ/token); çift paylaşım olmasın diye beklendi |
| `POST_WINDOWS ... gecerli bir pencere icermiyor` | `POST_WINDOWS` variable'ını düzeltin (ör. `9-14,17-22`) |
