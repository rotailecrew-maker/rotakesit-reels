#!/usr/bin/env python3
"""
rotakesit - Instagram Reels otomasyonu

Drive yapisi:
    RA 1/                        <- DRIVE_ROOT_FOLDER_ID
    |-- 1. Hafta/
    |   |-- Reels/               <- videolar (kuyruk)
    |   +-- Capitons/            <- caption'lar, video adiyla ayni .txt
    |-- 2. Hafta/
    |   +-- ...
    |-- published/               <- yayinlanan videolar buraya tasinir
    +-- failed/                  <- 3 denemede yayinlanamayanlar

Akis:
  1. Hafta klasorlerini dogal sirayla gez (1. Hafta -> 2. Hafta -> 10. Hafta)
  2. Ilk uygun videoyu bul; hafta bitince sonrakine gec
  3. Caption'i o haftanin Capitons/ klasorunden ayni adla al
  4. Indir + boyut dogrula -> IG resumable upload -> poll -> publish
     (publish'ten hemen once container id'si state.json'a "bekleyen" olarak
     yazilir; calisma yarida kalirsa sonraki calisma container'in gercekten
     yayinlanip yayinlanmadigini IG'ye sorar - cift paylasim olmaz)
  5. Basarili: videoyu kokteki published/ klasorune tasi
  6. Hatali: SADECE dosyaya ozgu hatalarda retry sayacini artir;
     token/kota/ag hatalari sayaci yakmaz. MAX_RETRIES'te failed/ klasorune.
     Videoya bagli gecici hatalar (yukleme, isleme zaman asimi) ayri sayilir;
     MAX_TRANSIENT_RETRIES'te video DEFER_HOURS ertelenir ve kuyruk tikanmaz.

Caption dosyalari YERINDE KALIR - kutuphane gibi kullanildiklari icin
tasinmazlar, sadece videolar hareket eder.

Calistirma:
  python post_reel.py            # canli
  DRY_RUN=1 python post_reel.py  # Instagram'a hicbir sey gondermez
"""

import io
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

import requests
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

# Windows konsolu cp1254/cp850 olabiliyor; DRY_RUN caption'i basarken
# Turkce karakterler UnicodeEncodeError'a yol acmasin
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


# --------------------------------------------------------------------------
# Hata siniflari - retry sayacinin yanip yanmayacagini bunlar belirler
# --------------------------------------------------------------------------

class TransientError(Exception):
    """Dosyayla ilgisi olmayan hata (token, kota, ag, izin).
    Retry sayaci ARTMAZ - yoksa saglam videolar failed/ klasorune surulur."""


class FileTransientError(TransientError):
    """Bu videoya bagli ama kalici oldugu kanitlanmamis hata (yukleme reddi,
    isleme zaman asimi, indirme hatasi, Graph code 100).

    Retry sayacini YAKMAZ (video failed/'a gitmez), ama transient_retries
    sayacini artirir. MAX_TRANSIENT_RETRIES'te video DEFER_HOURS ertelenir -
    yoksa kuyrugun basindaki tek bir sorunlu video tum kuyrugu kilitler."""


class FileError(Exception):
    """Dosyaya ozgu hata (bozuk video, IG spec reddi, boyut).
    Retry sayaci ARTAR."""


# --------------------------------------------------------------------------
# Env dogrulama
#
# NOT: GitHub'da tanimsiz bir secret KeyError vermez, BOS STRING olur.
# Bu yuzden os.environ[...] korumasi yetmez; acik kontrol sart.
# --------------------------------------------------------------------------

REQUIRED_ENV = ("IG_USER_ID", "IG_ACCESS_TOKEN", "DRIVE_ROOT_FOLDER_ID")

OAUTH_ENV = ("GOOGLE_OAUTH_CLIENT_ID",
             "GOOGLE_OAUTH_CLIENT_SECRET",
             "GOOGLE_OAUTH_REFRESH_TOKEN")


def _str_env(name, default):
    """Bos degeri 'tanimsiz' sayar.

    GitHub'da tanimsiz bir secret/variable bos string olur ve
    os.environ.get(name, default) varsayilani DONDURMEZ - boslugu dondurur.
    USER_TAGS bu yuzden sessizce devre disi kalmisti.
    """
    return os.environ.get(name, "").strip() or default


def _bool_env(name, default=False):
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on", "evet")


def _int_env(name, default):
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _has_oauth():
    return all(os.environ.get(k, "").strip() for k in OAUTH_ENV)


def _has_service_account():
    return bool(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip())


def _validate_env():
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k, "").strip()]
    if missing:
        sys.exit(
            "HATA - su ortam degiskenleri eksik veya bos: "
            + ", ".join(missing)
            + "\nGitHub Actions kullaniyorsaniz: Settings > Secrets and variables"
              " > Actions altinda tanimli olduklarindan emin olun."
              " Tanimsiz bir secret hata vermez, sessizce bos gelir."
        )

    if not _has_oauth() and not _has_service_account():
        sys.exit(
            "HATA - Drive kimlik bilgisi yok. Ikisinden biri gerekli:\n"
            "  (onerilen) " + ", ".join(OAUTH_ENV) + "\n"
            "             -> python setup_oauth.py ile uretilir\n"
            "  (Shared Drive kullaniyorsaniz) GOOGLE_SERVICE_ACCOUNT_JSON\n"
            "     UYARI: service account kisisel My Drive'da dosya olusturamaz"
            " ve tasiyamaz."
        )

    if _has_service_account() and not _has_oauth():
        try:
            json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
        except json.JSONDecodeError as e:
            sys.exit(f"HATA - GOOGLE_SERVICE_ACCOUNT_JSON gecerli JSON degil: {e}")


_validate_env()


# --------------------------------------------------------------------------
# Konfig
# --------------------------------------------------------------------------

DRY_RUN = _bool_env("DRY_RUN")

GRAPH_VERSION = _str_env("GRAPH_VERSION", "v23.0")
GRAPH_HOST = f"https://graph.facebook.com/{GRAPH_VERSION}"
RUPLOAD_HOST = f"https://rupload.facebook.com/ig-api-upload/{GRAPH_VERSION}"

IG_USER_ID = os.environ["IG_USER_ID"].strip()
IG_ACCESS_TOKEN = os.environ["IG_ACCESS_TOKEN"].strip()

# Opsiyonel - varsa token omru dogru sorgulanir ve yenileme yapilabilir
IG_APP_ID = os.environ.get("IG_APP_ID", "").strip()
IG_APP_SECRET = os.environ.get("IG_APP_SECRET", "").strip()

ROOT_FOLDER_ID = os.environ["DRIVE_ROOT_FOLDER_ID"].strip()
# Bos birakilirsa kok altinda isme gore bulunur
PUBLISHED_FOLDER_ID = os.environ.get("DRIVE_PUBLISHED_FOLDER_ID", "").strip()
FAILED_FOLDER_ID = os.environ.get("DRIVE_FAILED_FOLDER_ID", "").strip()

FOLDER_MIME = "application/vnd.google-apps.folder"
# Kisayol, Google Docs vb. - indirilemezler (get_media 403 verir)
GOOGLE_APPS_MIME_PREFIX = "application/vnd.google-apps."

# Klasor adi eslesmeleri - hepsi harf duyarsiz
REELS_NAMES = ("reels", "reel", "videolar")
# "Capitons" mevcut yazim; "Captions" ileride duzeltilirse de calissin
CAPTION_NAMES = ("capitons", "captions", "caption", "captionlar")
# Kok altinda hafta sayilmayacak klasorler
SKIP_ROOT_NAMES = {"published", "failed", "retry", "archive", "arsiv", "arşiv"}
# Kok altinda SADECE hafta klasoru gibi gorunenler kuyruga girer:
# "1. Hafta", "10.Hafta", "Hafta 3". "Arşiv", "Taslak", "Ham Çekim" gibi
# klasorlerdeki videolar yanlislikla paylasilmasin.
WEEK_RE = re.compile(r"^\s*\d|hafta\s*\d", re.IGNORECASE)

# Etiketler - Reels'te user_tags sadece username alir, x/y koordinati yok
USER_TAGS = [
    u.strip().lstrip("@")
    for u in _str_env("USER_TAGS", "rota,rotaile,ramedyaresmi").split(",")
    if u.strip()
]

# .txt bulunamazsa kullanilacak sablon. {name} = uzantisiz dosya adi
DEFAULT_CAPTION = _str_env(
    "DEFAULT_CAPTION",
    "{name}\n\n@rota @rotaile @ramedyaresmi\n\n#rotakesit #kesit #video",
)

MAX_RETRIES = _int_env("MAX_RETRIES", 3)
# Videoya bagli gecici hatalarda kac denemeden sonra video ertelensin.
# Pencere icinde saat basi deneme var (gunde ~12); 6 = yaklasik yarim gun.
MAX_TRANSIENT_RETRIES = _int_env("MAX_TRANSIENT_RETRIES", 6)
DEFER_HOURS = _int_env("DEFER_HOURS", 24)
STATE_FILENAME = "state.json"
# state.json'da video olmayan tek anahtar: kuyruk sayisi, bildirim tarihleri
META_KEY = "_meta"

# Drive cagrilari icin otomatik tekrar (5xx, 429, rate limit 403, ag kopmasi).
# Tek bir 500 yuzunden paylasim sonrasi state yazilamazsa ayni reel ikinci
# kez paylasilirdi.
DRIVE_RETRIES = _int_env("DRIVE_RETRIES", 4)

POLL_INTERVAL = 5                                      # saniye
POLL_TIMEOUT = _int_env("POLL_TIMEOUT", 480)           # 8 dk
# requests'te bu TOPLAM sure degil, tek bir okuma/yazma icin bekleme siniri
UPLOAD_TIMEOUT = _int_env("UPLOAD_TIMEOUT", 600)
UPLOAD_ATTEMPTS = _int_env("UPLOAD_ATTEMPTS", 3)
# Yukleme tamamen basarisiz olursa sifirdan yeni container ile kac kez denensin
CONTAINER_ATTEMPTS = _int_env("CONTAINER_ATTEMPTS", 2)
# Toplam calisma butcesi. Workflow timeout'u 45 dk; GitHub isi oldururse
# hicbir sey kaydedilemez. Bu yuzden butce azalinca YENI deneme baslatilmaz
# (baslamis bir publish her zaman tamamlanir).
RUN_BUDGET_SECONDS = _int_env("RUN_BUDGET_SECONDS", 35 * 60)
_STARTED = time.monotonic()

VIDEO_EXTS = (".mp4", ".mov")
MAX_VIDEO_BYTES = 1024 * 1024 * 1024                   # IG Reels siniri: 1 GB

CAPTION_MAX_CHARS = 2200
CAPTION_MAX_HASHTAGS = 30

TOKEN_WARN_DAYS = 7

# Ust uste tetikleyicilere karsi koruma. Harici tetikleyici 18:00'de,
# cron 18:17'de calisirsa ikisi FARKLI videolar paylasir - state.json bunu
# engellemez, o sadece AYNI videonun tekrarini engeller. Bu esik, son
# paylasimdan bu yana yeterli sure gecmediyse calismayi sessizce bitirir.
# 0 = kapali. Eskiden 6 (sonra 4) saatti;
# posted_in_window zaten ayni pencerede ikinci paylasimi engelliyor; bu esik
# sadece dakikalar arayla gelen tekrar tetiklemelere karsi. Pencereler
# genisledigi icin dusuruldu (sabah 14:59 + aksam 17:00 = 2.0 saat).
MIN_INTERVAL_HOURS = _int_env("MIN_INTERVAL_HOURS", 2)

# GitHub cron zamanlanmis calismalari rastgele dusuruyor - bu repoda 2/2
# kacirdi. Cozum: saat basi denemek ve pencere disinda hicbir sey yapmamak.
# Varsayilan sabah 09-14, aksam 17-22 (TR, bitis saati dahil). Her pencerede
# 6 deneme sansi var; biri tutarsa o pencere icin is bitmis olur.
TZ_OFFSET_HOURS = _int_env("TZ_OFFSET_HOURS", 3)   # Turkiye UTC+3, DST yok
# Pencere genisligi olculdu, tahmin degil: 5-6 Eylul 2026'da GitHub saat basi
# tetiklemelerin %39'unu calistirdi (7/18) ve araliklar 2.0-5.0 saat arasindaydi.
# 3 saatlik pencere bu boslukta tamamen kacirilabiliyor - 6 Eylul sabahi oyle
# oldu. 6 saatlik pencerede 6 sans var; %61 dusme oraniyla kacirma ihtimali
# ~%5. Gec paylasmak, hic paylasmamaktan iyi.
POST_WINDOWS = _str_env("POST_WINDOWS", "9-14,17-22")

# Pencere kurali otomatik tetiklemelerde gecerli.
#
# repository_dispatch da otomatiktir: harici tetikleyici (cron-job.org)
# GitHub'in guvenilmez cron'unu yedeklemek icin saat basi vuruyor. Bunu
# pencere disinda birakmak gece 03:00'te paylasim demek olurdu.
#
# workflow_dispatch'te workflow ENFORCE_WINDOW'u "enforce_window" girdisinden
# acikca verir (varsayilan acik). Boylece harici tetikleyici workflow_dispatch
# API'sini de guvenle kullanabilir; elle hemen paylasmak icin kutu kaldirilir.
ENFORCE_WINDOW = _bool_env(
    "ENFORCE_WINDOW",
    os.environ.get("GITHUB_EVENT_NAME", "") in ("schedule", "repository_dispatch"))
STATE_RETENTION_DAYS = _int_env("STATE_RETENTION_DAYS", 90)

# Graph API hata kodlari - bunlar dosyanin sucu degil, hicbir sayaci yakmasinlar
TRANSIENT_GRAPH_CODES = {
    1,    # Unknown / gecici
    2,    # Service temporarily unavailable
    4,    # Application request limit reached
    9,    # Paylasim limiti (subcode 2207042) - 24 saat dolunca gecer
    10,   # Permission denied
    17,   # User request limit reached
    32,   # Page request limit reached
    102,  # Session expired
    190,  # Access token gecersiz / suresi dolmus
    200,  # Permissions error
    341,  # Application limit reached
    368,  # Temporarily blocked
    613,  # Rate limit
}

# Videoya bagli olabilen ama kalici oldugu kesin olmayan kodlar.
# failed/'a surmezler, ama tekrarlarsa video ertelenir (kuyruk tikanmaz).
FILE_TRANSIENT_GRAPH_CODES = {
    100,   # Invalid parameter - konfig hatasi da olabilir, caption/etiket de
    9007,  # Media not ready for publishing (subcode 2207027)
}


def redact(text):
    """Sirlarin log'a veya state.json'a sizmasini engeller."""
    text = str(text)
    for secret in (IG_ACCESS_TOKEN, IG_APP_SECRET,
                   os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN", ""),
                   os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")):
        if secret and len(secret) > 8:
            text = text.replace(secret, "***")
    return text


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {redact(msg)}", flush=True)


def time_left():
    """RUN_BUDGET_SECONDS'tan kalan saniye."""
    return RUN_BUDGET_SECONDS - (time.monotonic() - _STARTED)


# --------------------------------------------------------------------------
# HTTP yardimcilari
# --------------------------------------------------------------------------

def http(method, url, **kwargs):
    """Ag hatalarini TransientError'a cevirir."""
    try:
        return requests.request(method, url, **kwargs)
    except requests.exceptions.RequestException as e:
        raise TransientError(f"Ag hatasi ({type(e).__name__}): {e}") from e


def graph_failure(resp, context):
    """Graph API yanitindan uygun hata sinifini uretir."""
    try:
        body = resp.json()
    except ValueError:
        body = {}
    err = body.get("error", {}) if isinstance(body, dict) else {}
    code = err.get("code")
    sub = err.get("error_subcode")
    msg = err.get("message") or resp.text[:300]
    detail = f"{context}: HTTP {resp.status_code} [code={code} sub={sub}] {msg}"

    if resp.status_code >= 500 or code in TRANSIENT_GRAPH_CODES:
        return TransientError(detail)
    # Kod cozulemediyse (JSON olmayan 4xx, proxy sayfasi vb.) videoyu
    # suclamak icin kanit yok - retry hakki yakilmaz
    if code is None or code in FILE_TRANSIENT_GRAPH_CODES:
        return FileTransientError(detail)
    return FileError(detail)


def drive_exec(request, context):
    """Drive cagrilarini calistirir, hatalari siniflandirir.

    5xx, 429, rate limit 403 ve ag kopmalari once DRIVE_RETRIES kez ustel
    beklemeyle tekrar denenir (googleapiclient num_retries). Yine olmazsa:
    403 (izin/kota), 404 (paylasilmamis), 429, 5xx - hicbiri videonun sucu
    degil, hepsi TransientError.
    """
    try:
        return request.execute(num_retries=DRIVE_RETRIES)
    except HttpError as e:
        status = getattr(e.resp, "status", 0)
        raise TransientError(
            f"{context}: Drive HTTP {status} {str(e)[:200]}") from e
    except Exception as e:
        raise TransientError(f"{context}: {type(e).__name__} {e}") from e


# --------------------------------------------------------------------------
# Token saglik kontrolu
# --------------------------------------------------------------------------

def check_token_expiry():
    """Token'in ne zaman olecegini soyler. Calismayi asla durdurmaz."""
    verifier = (f"{IG_APP_ID}|{IG_APP_SECRET}"
                if IG_APP_ID and IG_APP_SECRET else IG_ACCESS_TOKEN)
    try:
        r = requests.get(
            f"{GRAPH_HOST}/debug_token",
            params={"input_token": IG_ACCESS_TOKEN, "access_token": verifier},
            timeout=30,
        )
        data = r.json().get("data", {})
    except Exception as e:
        log(f"UYARI: token omru kontrol edilemedi ({type(e).__name__})")
        return

    if data.get("is_valid") is False:
        log("!!! TOKEN GECERSIZ - yenilenmeden hicbir paylasim yapilamaz "
            "(bkz README > Token yenileme)")
        return

    expires_at = data.get("expires_at")
    if not expires_at:
        log("Token: suresiz gorunuyor (expires_at bildirilmedi)")
        return

    left = datetime.fromtimestamp(expires_at, timezone.utc) - datetime.now(timezone.utc)
    days = left.days
    if days <= 0:
        log("!!! TOKEN SURESI DOLMUS - yenilenmeli")
    elif days <= TOKEN_WARN_DAYS:
        log(f"!!! TOKEN {days} GUN SONRA DOLUYOR - refresh_token.py calistirin")
    else:
        log(f"Token gecerli, {days} gun omru kaldi")


# --------------------------------------------------------------------------
# Google Drive
# --------------------------------------------------------------------------

def drive_client():
    """OAuth (kendi hesabiniz) tercih edilir; yoksa service account.

    Service account kisisel My Drive'da dosya olusturamaz (kota 0) ve
    tasiyamaz (cannotAddParent) - sadece Shared Drive'da is gorur.
    """
    if _has_oauth():
        from google.oauth2.credentials import Credentials
        creds = Credentials(
            token=None,
            refresh_token=os.environ["GOOGLE_OAUTH_REFRESH_TOKEN"].strip(),
            client_id=os.environ["GOOGLE_OAUTH_CLIENT_ID"].strip(),
            client_secret=os.environ["GOOGLE_OAUTH_CLIENT_SECRET"].strip(),
            token_uri="https://oauth2.googleapis.com/token",
            scopes=["https://www.googleapis.com/auth/drive"],
        )
        log("Drive kimligi: OAuth (kullanici hesabi)")
    else:
        from google.oauth2 import service_account
        info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/drive"])
        log("Drive kimligi: service account "
            "(My Drive'da tasima/olusturma calismaz)")
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def list_folder(drive, folder_id, label=""):
    """Klasordeki tum ogeleri dondurur (sayfalama dahil)."""
    files, page_token = [], None
    while True:
        resp = drive_exec(
            drive.files().list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields=("nextPageToken, files(id, name, mimeType, size, "
                        "parents, createdTime, modifiedTime)"),
                pageSize=1000,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ),
            f"Klasor listelenemedi ({label or folder_id})",
        )
        files.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            return files


def subfolders(entries):
    return [f for f in entries if f["mimeType"] == FOLDER_MIME]


def find_folder(entries, names):
    for f in subfolders(entries):
        if f["name"].strip().lower() in names:
            return f
    return None


def temp_video_path(video, prefix="reel_"):
    """Gecici dosya yolu - video ADINDAN degil, guvenli bir addan.

    Drive dosya adinda '/' olabilir ('Bolum 1/2.mp4'): os.path.join bunu
    olmayan bir alt klasor sanar, '/' ile baslayan ad ise gecici klasorun
    disina cikar. Ikisi de indirmeyi her seferinde patlatip kuyrugu kilitlerdi.
    """
    ext = os.path.splitext(video["name"])[1].lower()
    if ext not in VIDEO_EXTS:
        ext = ".mp4"
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=ext)
    os.close(fd)
    return path


def download_file(drive, meta, dest_path):
    """Indirir ve Drive'in bildirdigi boyutla karsilastirir.

    Hatalar FileTransientError: listeleme ayni kimlikle az once calisti,
    yani sorun buyuk ihtimalle bu dosyada (indirme kapali, kisayol) ya da
    agda. Retry hakki yakmaz ama tekrarlarsa video ertelenir.
    """
    request = drive.files().get_media(fileId=meta["id"], supportsAllDrives=True)
    try:
        with io.FileIO(dest_path, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request, chunksize=8 * 1024 * 1024)
            done = False
            while not done:
                _, done = downloader.next_chunk(num_retries=DRIVE_RETRIES)
    except HttpError as e:
        raise FileTransientError(
            f"Indirme hatasi: Drive HTTP {getattr(e.resp, 'status', 0)}") from e
    except Exception as e:
        raise FileTransientError(f"Indirme hatasi: {type(e).__name__} {e}") from e

    expected = int(meta.get("size") or 0)
    actual = os.path.getsize(dest_path)
    if expected and actual != expected:
        # Kopan baglanti da buna yol acar - videonun bozuk oldugu kanitlanmadi
        raise FileTransientError(
            f"Indirme eksik: {actual} bayt indi, {expected} bayt bekleniyordu")
    return actual


def read_text_file(drive, file_id):
    """UTF-16 (BOM'lu), UTF-8, cp1254 (Windows Notepad), o da olmazsa kayipli."""
    data = drive_exec(
        drive.files().get_media(fileId=file_id, supportsAllDrives=True),
        f"Metin dosyasi okunamadi ({file_id})",
    )
    # Notepad "Unicode" = UTF-16 + BOM. cp1254 bunu da hatasiz cozer ama
    # her harfin arasina NUL koyar - bozuk caption paylasilirdi.
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16").strip()
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8-sig", "cp1254"):
        try:
            return data.decode(enc).strip()
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace").strip()


def move_file(drive, meta, to_folder):
    """Dosyayi hedef klasore tasir.

    removeParents icin meta['parents'] kullanilir: files.get parent
    dondurmeyebiliyor (paylasimli erisimde), files.list donduruyor.
    """
    parents = meta.get("parents") or []
    if not parents:
        raise TransientError(
            f"{meta['name']}: mevcut klasor belirlenemedi, tasima atlandi")
    drive_exec(
        drive.files().update(
            fileId=meta["id"],
            addParents=to_folder,
            removeParents=",".join(parents),
            fields="id, parents",
            supportsAllDrives=True,
        ),
        f"Dosya tasinamadi ({meta['name']})",
    )


# --------------------------------------------------------------------------
# Durum dosyasi (kokte state.json)
# --------------------------------------------------------------------------

def load_state(drive, root_entries):
    """(state_file_id, state, ok) dondurur.

    ok=False ise state.json var ama okunamadi. O durumda UZERINE YAZMADAN
    cikmak gerekir - yoksa tum yayin gecmisi ve retry sayaclari silinir.

    Birden fazla state.json varsa (ilk olusturma tekrar denenirse olabilir)
    EN SON degistirilen kullanilir - rastgele secim gecmisi kaybettirirdi.
    """
    adaylar = [f for f in root_entries if f["name"] == STATE_FILENAME]
    if not adaylar:
        return None, {}, True
    if len(adaylar) > 1:
        log(f"UYARI: kokte {len(adaylar)} adet state.json var; en yenisi "
            f"kullaniliyor. Digerlerini Drive'dan silin.")
    f = max(adaylar, key=lambda x: x.get("modifiedTime") or "")
    try:
        raw = read_text_file(drive, f["id"])
        state = json.loads(raw or "{}")
        if not isinstance(state, dict):
            raise ValueError("kok nesne sozluk degil")
        return f["id"], state, True
    except Exception as e:
        log(f"state.json okunamadi: {type(e).__name__} {e}")
        return f["id"], {}, False


def video_entries(state):
    """state.json'daki video kayitlari (_meta haric)."""
    return [(k, v) for k, v in state.items()
            if k != META_KEY and isinstance(v, dict)]


def save_state(drive, state_file_id, state):
    """state.json'u yazar, dosya id'sini dondurur.

    Donen id saklanmali: ilk calismada dosya yoksa olusturulur ve ayni
    calismadaki sonraki kayitlar YENI dosya acmak yerine onu guncellemeli.
    """
    payload = json.dumps(state, ensure_ascii=False, indent=2)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as tmp:
            tmp.write(payload)
            tmp_path = tmp.name
        media = MediaFileUpload(tmp_path, mimetype="application/json",
                                resumable=False)
        try:
            if state_file_id:
                drive_exec(
                    drive.files().update(fileId=state_file_id, media_body=media,
                                         supportsAllDrives=True),
                    "state.json guncellenemedi",
                )
                return state_file_id
            created = drive_exec(
                drive.files().create(
                    body={"name": STATE_FILENAME, "parents": [ROOT_FOLDER_ID]},
                    media_body=media, fields="id", supportsAllDrives=True,
                ),
                "state.json olusturulamadi (service account kullaniyorsaniz "
                "kotasi 0'dir; OAuth'a gecin veya dosyayi elle olusturun)",
            )
            return (created or {}).get("id")
        finally:
            # Windows'ta acik kalan tanitici unlink'i engelliyor
            fd = getattr(media, "_fd", None)
            if fd is not None:
                try:
                    fd.close()
                except Exception:
                    pass
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def local_now():
    """Yerel (TR) saat, naive. zoneinfo runner'da hep bulunmayabiliyor."""
    return (datetime.now(timezone.utc)
            + timedelta(hours=TZ_OFFSET_HOURS)).replace(tzinfo=None)


def to_local(when):
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (when.astimezone(timezone.utc)
            + timedelta(hours=TZ_OFFSET_HOURS)).replace(tzinfo=None)


def parse_windows(spec):
    """"9-14,17-22" -> [(9, 14), (17, 22)]. Bitis saati dahil (14:59'a kadar).

    Gece yarisini asan pencere ("22-2") desteklenmez - "gunde bu pencerede
    paylasim yapildi mi" kontrolu tarih degisince bozulur. Oyle bir parca
    sessizce hic eslesmemek yerine uyariyla atlanir.
    """
    out = []
    for parca in spec.split(","):
        parca = parca.strip()
        if not parca:
            continue
        try:
            if "-" in parca:
                a, b = parca.split("-", 1)
                lo, hi = int(a), int(b)
            else:
                lo = hi = int(parca)
        except ValueError:
            log(f"UYARI: POST_WINDOWS icinde cozulemeyen parca: {parca!r}")
            continue
        if not (0 <= lo <= 23 and 0 <= hi <= 23):
            log(f"UYARI: POST_WINDOWS parcasi 0-23 disinda, atlandi: {parca!r}")
            continue
        if lo > hi:
            log(f"UYARI: POST_WINDOWS parcasi gece yarisini asiyor, desteklenmez "
                f"({parca!r}); iki parcaya bolun, or. '{lo}-23,0-{hi}'")
            continue
        out.append((lo, hi))
    return out


def current_window(now_local, windows):
    for lo, hi in windows:
        if lo <= now_local.hour <= hi:
            return (lo, hi)
    return None


def posted_in_window(state, now_local, window):
    """Bu pencerede bugun zaten paylasim yapildi mi?"""
    lo, hi = window
    for _, entry in video_entries(state):
        raw = entry.get("published_at")
        if not raw:
            continue
        try:
            when = to_local(datetime.fromisoformat(raw))
        except (ValueError, TypeError):
            continue
        if when.date() == now_local.date() and lo <= when.hour <= hi:
            return True
    return False


def last_published_at(state):
    """state.json'daki en son basarili paylasim zamani. Yoksa None."""
    stamps = []
    for _, entry in video_entries(state):
        raw = entry.get("published_at")
        if not raw:
            continue
        try:
            when = datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        stamps.append(when)
    return max(stamps) if stamps else None


def prune_state(state, live_ids):
    """Kuyrukta artik olmayan ve suresi gecmis kayitlari atar.

    Kuyrukta HALA duran hicbir kayda dokunmaz - yoksa 'published' isareti
    kaybolur ve video ikinci kez paylasilir.
    """
    if STATE_RETENTION_DAYS <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=STATE_RETENTION_DAYS)
    dropped = []
    for file_id, entry in video_entries(state):
        if file_id in live_ids or entry.get("pending_container"):
            continue
        stamp = entry.get("published_at") or entry.get("last_attempt")
        if not stamp:
            continue
        try:
            when = datetime.fromisoformat(stamp)
        except (ValueError, TypeError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when < cutoff:
            dropped.append(file_id)
    for file_id in dropped:
        del state[file_id]
    if dropped:
        log(f"state.json temizlendi: {len(dropped)} eski kayit atildi")
    return len(dropped)


# --------------------------------------------------------------------------
# Caption
# --------------------------------------------------------------------------

def render_default_caption(stem):
    """format() yerine replace - dosya adindaki { } cokmeye yol acmasin."""
    return DEFAULT_CAPTION.replace("{name}", stem)


def ig_len(text):
    """Karakter sayisi UTF-16 birimiyle - emoji 2 sayilir.

    IG'nin 2200 sinirini nasil saydigi belgelenmemis; Python len() emojiyi 1
    sayar. Temkinli olan (buyuk olan) olcu kullanilir, sinir asilmasin.
    """
    return len(text.encode("utf-16-le")) // 2


def _cut_ig(text, limit):
    """text'in ig_len <= limit olan en uzun basi."""
    units = 0
    for i, ch in enumerate(text):
        units += 2 if ord(ch) > 0xFFFF else 1
        if units > limit:
            return text[:i]
    return text


def normalize_caption(text):
    """IG limitlerine uydurur: 30 hashtag, 2200 karakter."""
    # Windows'ta yazilmis .txt CRLF getirir; IG'ye ve cift paylasim
    # karsilastirmasina tek bicimde gitsin
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    tags = list(re.finditer(r"#\w+", text, flags=re.UNICODE))
    if len(tags) > CAPTION_MAX_HASHTAGS:
        fazla = tags[CAPTION_MAX_HASHTAGS:]
        # Tam olarak o konumlari sil - str.replace ilk eslesmeyi silerdi:
        # fazla '#rota', bastaki '#rotakesit'i 'kesit'e cevirirdi
        for m in reversed(fazla):
            text = text[:m.start()] + text[m.end():]
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r"[ \t]+\n", "\n", text).strip()
        log(f"UYARI: {len(tags)} hashtag vardi, son {len(fazla)} tanesi cikarildi "
            f"(IG siniri {CAPTION_MAX_HASHTAGS})")
    if ig_len(text) > CAPTION_MAX_CHARS:
        # Tek karakterlik '…' - eskiden [:2199] + '...' 2202 karakter uretiyordu
        text = _cut_ig(text, CAPTION_MAX_CHARS - 1).rstrip() + "…"
        log(f"UYARI: caption {CAPTION_MAX_CHARS} karaktere kisaltildi")
    return text


def caption_file_for(candidates, video_name):
    """Ayni isimli .txt'yi HARF DUYARSIZ arar."""
    stem = os.path.splitext(video_name)[0].strip().lower()
    for f in candidates:
        if f["name"].strip().lower() == f"{stem}.txt":
            return f
    return None


def resolve_caption(drive, job):
    """Once Capitons/ klasoru, sonra videonun yanindaki .txt, sonra sablon."""
    video_name = job["video"]["name"]
    stem = os.path.splitext(video_name)[0]

    for pool, where in ((job["captions"], "Capitons"),
                        (job["videos"], "Reels")):
        txt = caption_file_for(pool, video_name)
        if not txt:
            continue
        try:
            text = read_text_file(drive, txt["id"])
        except Exception as e:
            log(f"UYARI: {txt['name']} okunamadi ({e}), sonraki kaynaga geciliyor")
            continue
        if text:
            log(f"Caption kaynagi: {where}/{txt['name']}")
            return normalize_caption(text), txt
        log(f"UYARI: {txt['name']} bos")

    log("Caption kaynagi: varsayilan sablon")
    return normalize_caption(render_default_caption(stem)), None


# --------------------------------------------------------------------------
# Instagram
# --------------------------------------------------------------------------

def create_container(caption):
    """Resumable upload session acar, container id dondurur."""
    params = {
        "media_type": "REELS",
        "upload_type": "resumable",
        "caption": caption,
        "share_to_feed": "true",
        "access_token": IG_ACCESS_TOKEN,
    }
    if USER_TAGS:
        params["user_tags"] = json.dumps([{"username": u} for u in USER_TAGS])

    r = http("POST", f"{GRAPH_HOST}/{IG_USER_ID}/media", data=params, timeout=60)
    try:
        body = r.json()
    except ValueError:
        body = {}

    if r.status_code != 200 or "id" not in body:
        err = body.get("error", {}) if isinstance(body, dict) else {}
        # user_tags kaynakli hatada etiketsiz tekrar dene - post kaybolmasin
        if USER_TAGS and "user_tags" in json.dumps(err):
            log(f"user_tags reddedildi ({err.get('message')}), etiketsiz deneniyor")
            params.pop("user_tags")
            r = http("POST", f"{GRAPH_HOST}/{IG_USER_ID}/media",
                     data=params, timeout=60)
            try:
                body = r.json()
            except ValueError:
                body = {}
        if "id" not in body:
            raise graph_failure(r, "Container olusturulamadi")
    return body["id"]


def upload_offset(container_id):
    """Yarim kalan yuklemenin kaldigi yeri sorar. Bilinemezse 0."""
    try:
        r = requests.get(
            f"{RUPLOAD_HOST}/{container_id}",
            headers={"Authorization": f"OAuth {IG_ACCESS_TOKEN}"},
            timeout=60,
        )
        body = r.json()
    except Exception:
        return 0
    if not isinstance(body, dict):
        return 0
    for key in ("offset", "received_bytes", "bytes_received"):
        value = body.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return 0


def upload_video(container_id, path):
    """Video byte'larini rupload'a yukler; kopan yuklemeyi kaldigi yerden surdurur."""
    size = os.path.getsize(path)
    if size > MAX_VIDEO_BYTES:
        raise FileError(f"Video {size / 1024 / 1024:.0f} MB - IG siniri 1024 MB")

    offset = 0
    last_error = None
    for attempt in range(1, UPLOAD_ATTEMPTS + 1):
        if attempt > 1 and time_left() < 5 * 60:
            last_error = f"{last_error} (calisma butcesi bitti, deneme kesildi)"
            break
        try:
            with open(path, "rb") as fh:
                fh.seek(offset)
                headers = {
                    "Authorization": f"OAuth {IG_ACCESS_TOKEN}",
                    "offset": str(offset),
                    "file_size": str(size),
                }
                if offset:
                    log(f"Yukleme {offset / 1024 / 1024:.1f} MB'tan devam ediyor "
                        f"(deneme {attempt}/{UPLOAD_ATTEMPTS})")
                r = requests.post(f"{RUPLOAD_HOST}/{container_id}",
                                  headers=headers, data=fh, timeout=UPLOAD_TIMEOUT)
        except requests.exceptions.RequestException as e:
            last_error = f"{type(e).__name__}: {e}"
            log(f"Yukleme koptu ({type(e).__name__}), kalinan yer soruluyor")
            offset = upload_offset(container_id)
            continue

        try:
            body = r.json()
        except ValueError:
            body = {}

        if isinstance(body, dict) and body.get("success"):
            log(f"Yuklendi: {size / 1024 / 1024:.1f} MB")
            return

        # rupload sadece byte alir, video icerigini DOGRULAMAZ - icerik
        # kontrolu wait_until_finished'te yapilir (status_code=ERROR).
        # Bu yuzden yukleme hatasi "video bozuk" demek degildir ve retry
        # sayacini yakmamalidir. ProcessingFailedError bunun tipik ornegi:
        # retriable:false dese de spec'i kusursuz videolarda da gorulur.
        last_error = f"HTTP {r.status_code}: {r.text[:200]}"
        log(f"Yukleme reddedildi ({last_error}), tekrar denenecek")
        offset = upload_offset(container_id)

    # Videoya bagli ama kalici oldugu kanitlanmamis: 24 Eylul'de ayni video
    # 17:47'de ProcessingFailedError ile dustu, 18:03'te sorunsuz yuklendi.
    raise FileTransientError(
        f"Yukleme {UPLOAD_ATTEMPTS} denemede tamamlanamadi. Son hata: {last_error}")


def container_status(container_id):
    """Container'in status_code'u: IN_PROGRESS, FINISHED, PUBLISHED, ERROR, EXPIRED.

    PUBLISHED, publish yaniti kaybolsa bile yayinin gerceklestigini kesin
    olarak soyler - caption karsilastirmasina gerek kalmaz.
    """
    r = http(
        "GET",
        f"{GRAPH_HOST}/{container_id}",
        params={"fields": "status_code,status", "access_token": IG_ACCESS_TOKEN},
        timeout=30,
    )
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code != 200 or not isinstance(body, dict) or "error" in body:
        raise graph_failure(r, "Container durumu sorgulanamadi")
    return body.get("status_code"), body.get("status")


def wait_until_finished(container_id):
    # Publish icin en az 3 dk birak; butce azsa bekleme kisalir
    budget = max(60, min(POLL_TIMEOUT, time_left() - 180))
    deadline = time.time() + budget
    while time.time() < deadline:
        try:
            r = http(
                "GET",
                f"{GRAPH_HOST}/{container_id}",
                params={"fields": "status_code,status",
                        "access_token": IG_ACCESS_TOKEN},
                timeout=30,
            )
        except TransientError as e:
            # Tek bir ag kopmasi yuklenmis videoyu cope atmasin
            log(f"Durum sorgusu basarisiz ({e}), tekrar denenecek")
            time.sleep(POLL_INTERVAL)
            continue
        try:
            body = r.json()
        except ValueError:
            body = {}

        if r.status_code >= 500:
            log(f"Durum sorgusu HTTP {r.status_code}, tekrar denenecek")
            time.sleep(POLL_INTERVAL)
            continue
        if r.status_code != 200 or not isinstance(body, dict) or "error" in body:
            # Token/izin hatasi 8 dk "Isleniyor... (None)" diye beklenmesin,
            # hemen gercek hatayla dussun
            raise graph_failure(r, "Durum sorgusu")

        code = body.get("status_code")
        if code == "FINISHED":
            return
        if code == "ERROR":
            # Islemede hata = videonun kendisiyle ilgili (codec, sure, en-boy)
            raise FileError(f"Container ERROR: {body.get('status')}")
        if code == "EXPIRED":
            raise FileTransientError(f"Container EXPIRED: {body.get('status')}")
        log(f"Isleniyor... ({code})")
        time.sleep(POLL_INTERVAL)
    raise FileTransientError(f"Isleme {budget:.0f}s icinde bitmedi")


def publish(container_id):
    r = http(
        "POST",
        f"{GRAPH_HOST}/{IG_USER_ID}/media_publish",
        data={"creation_id": container_id, "access_token": IG_ACCESS_TOKEN},
        timeout=120,
    )
    try:
        body = r.json()
    except ValueError:
        body = {}
    if "id" not in body:
        raise graph_failure(r, "Yayinlanamadi")
    return body["id"]


def find_recent_media(caption, since=None):
    """Son paylasimlarda bu caption ile baslayan reel'in media_id'si.

    Yayinin OLUP OLMADIGINA container_status karar verir (PUBLISHED); bu
    fonksiyon oncelikle media_id'yi bulmak icin, container sorgulanamazsa
    da yedek kanit olarak kullanilir. `since`: bu andan (5 dk payla) once
    paylasilanlar sayilmaz. Varsayilan son 60 dk.
    """
    head = normalize_caption_head(caption)
    if not head:
        return None
    try:
        r = requests.get(
            f"{GRAPH_HOST}/{IG_USER_ID}/media",
            params={"fields": "id,caption,timestamp", "limit": 10,
                    "access_token": IG_ACCESS_TOKEN},
            timeout=30,
        )
        data = r.json().get("data", [])
    except Exception:
        return None

    if since is None:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=60)
    else:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        cutoff = since - timedelta(minutes=5)
    for item in data:
        try:
            when = datetime.strptime(item.get("timestamp", ""),
                                     "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            continue
        if when < cutoff:
            continue
        if normalize_caption_head(item.get("caption"), None).startswith(head):
            return item.get("id")
    return None


def normalize_caption_head(caption, n=80):
    """Karsilastirma icin: satir sonlari tek bicim, bas/son bosluk yok."""
    text = (caption or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return text if n is None else text[:n]


def recover_publish(container_id, caption, since):
    """Publish hata verdi - reel yine de yayinlanmis olabilir mi?

    (durum, media_id) dondurur:
      "published"   container PUBLISHED (ya da caption'la bulundu)
      "unpublished" container kesin yayinlanmamis (FINISHED/ERROR/EXPIRED)
      "unknown"     IG'ye ulasilamadi - karar sonraki calismaya kalir
    """
    for deneme in range(3):
        if deneme:
            time.sleep(10)
        try:
            code, _ = container_status(container_id)
        except Exception as e:
            log(f"Container durumu sorgulanamadi ({e})")
            continue
        if code == "PUBLISHED":
            return "published", find_recent_media(caption, since)
        if code in ("FINISHED", "ERROR", "EXPIRED"):
            return "unpublished", None
        # IN_PROGRESS / bos - IG hala isliyor olabilir, tekrar sor
    media_id = find_recent_media(caption, since)
    if media_id:
        return "published", media_id
    return "unknown", None


# --------------------------------------------------------------------------
# Kuyruk - hafta klasorleri
# --------------------------------------------------------------------------

def natural_key(name):
    """'10. Hafta' > '2. Hafta' olsun diye - duz sort bunun tersini yapar."""
    return [int(p) if p.isdigit() else p.lower()
            for p in re.split(r"(\d+)", name)]


def discover_root(drive):
    """Kok klasoru cozer: published/, failed/ ve hafta klasorleri."""
    entries = list_folder(drive, ROOT_FOLDER_ID, "kok klasor")
    folders = subfolders(entries)

    published = PUBLISHED_FOLDER_ID or (find_folder(entries, {"published"}) or {}).get("id")
    failed = FAILED_FOLDER_ID or (find_folder(entries, {"failed"}) or {}).get("id")

    eksik = [n for n, v in (("published", published), ("failed", failed)) if not v]
    if eksik:
        sys.exit(
            f"HATA - kok klasorde su alt klasorler bulunamadi: {', '.join(eksik)}\n"
            f"  Drive'da kok klasorun ({ROOT_FOLDER_ID}) altinda 'published' ve "
            f"'failed' adinda birer klasor olusturun, ya da\n"
            f"  DRIVE_PUBLISHED_FOLDER_ID / DRIVE_FAILED_FOLDER_ID degiskenleriyle "
            f"ID'lerini verin."
        )

    weeks, ignored = [], []
    for f in folders:
        name = f["name"].strip()
        if f["id"] in (published, failed) or name.lower() in SKIP_ROOT_NAMES:
            continue
        if WEEK_RE.search(name):
            weeks.append(f)
        else:
            ignored.append(name)
    if ignored:
        log(f"Hafta klasoru sayilmadi, kuyruga girmez: {', '.join(sorted(ignored))}")
    weeks.sort(key=lambda f: natural_key(f["name"]))
    return entries, published, failed, weeks


def week_contents(drive, week):
    """(reels_klasoru, videolar, caption_dosyalari) dondurur."""
    subs = list_folder(drive, week["id"], week["name"])
    reels = find_folder(subs, REELS_NAMES)
    caps = find_folder(subs, CAPTION_NAMES)

    if reels:
        videos = list_folder(drive, reels["id"], f"{week['name']}/{reels['name']}")
    else:
        # Reels/ alt klasoru yoksa videolar dogrudan hafta klasorunde olabilir
        videos = [f for f in subs if f["mimeType"] != FOLDER_MIME]
        reels = week if any(v["name"].lower().endswith(VIDEO_EXTS)
                            for v in videos) else None

    captions = (list_folder(drive, caps["id"], f"{week['name']}/{caps['name']}")
                if caps else [])
    return reels, videos, captions


def scan_weeks(drive, weeks):
    """Hafta agacini BIR KEZ listeler: [(hafta, reels, videolar, captionlar)].

    Eskiden sweep ve pick_job ayni agaci ayri ayri listeliyordu.
    """
    scanned = []
    for week in weeks:
        reels, videos, captions = week_contents(drive, week)
        if reels is None:
            log(f"UYARI: {week['name']} icinde Reels klasoru veya video yok, atlandi")
        for v in videos:
            if (v["name"].lower().endswith(VIDEO_EXTS)
                    and v["mimeType"].startswith(GOOGLE_APPS_MIME_PREFIX)):
                log(f"UYARI: {week['name']}/{v['name']} bir Drive kisayolu/dokumani, "
                    f"indirilemez - atlandi. Gercek video dosyasini koyun.")
        scanned.append((week, reels, videos, captions))
    return scanned


def _parse_utc(raw):
    try:
        when = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when


def deferred_until(entry):
    """Video ertelenmisse erteleme bitisi, degilse None."""
    when = _parse_utc(entry.get("deferred_until"))
    if when and when > datetime.now(timezone.utc):
        return when
    return None


def eligible(video, state):
    entry = state.get(video["id"], {})
    return (video["name"].lower().endswith(VIDEO_EXTS)
            and not video.get("mimeType", "").startswith(GOOGLE_APPS_MIME_PREFIX)
            and not entry.get("published")
            and entry.get("retries", 0) < MAX_RETRIES
            and not deferred_until(entry))


def pick_job(scanned, state):
    """Hafta sirasiyla ilk uygun videoyu bulur.

    (job, kuyruktaki tum id'ler, paylasilmayi bekleyen video sayisi) dondurur.
    """
    job, seen_ids, remaining = None, set(), 0
    for week, reels, videos, captions in scanned:
        seen_ids.update(v["id"] for v in videos)
        if reels is None:
            continue
        adaylar = sorted((v for v in videos if eligible(v, state)),
                         key=lambda f: natural_key(f["name"]))
        remaining += len(adaylar)
        if job is None and adaylar:
            job = {"week": week, "reels": reels, "video": adaylar[0],
                   "videos": videos, "captions": captions}
    return job, seen_ids, remaining


def deferred_names(scanned, state):
    """Su an ertelenmis videolarin adlari."""
    out = []
    for week, reels, videos, _ in scanned:
        if reels is None:
            continue
        for v in videos:
            entry = state.get(v["id"], {})
            if not entry.get("published") and deferred_until(entry):
                out.append(f"{week['name']}/{v['name']}")
    return out


def sweep(drive, scanned, state, published_folder, failed_folder):
    """Kuyrukta durmamasi gereken videolari yerine tasir.

    - MAX_RETRIES'i asmis olanlar -> failed/ (kuyruk tikanmasin)
    - Yayinlanmis ama hala kuyrukta olanlar -> published/ (onceki calismada
      tasima patladiysa ya da yayin sonradan dogrulandiysa)

    Caption'lar YERINDE KALIR: Capitons/ bir kutuphane, sadece video hareket eder.
    """
    for week, reels, videos, _ in scanned:
        if reels is None:
            continue
        for v in videos:
            entry = state.get(v["id"])
            if not entry:
                continue
            if entry.get("published"):
                hedef, ad = published_folder, "published"
            elif (entry.get("retries", 0) >= MAX_RETRIES
                  and not entry.get("moved_to_failed")):
                hedef, ad = failed_folder, "failed"
            else:
                continue
            try:
                move_file(drive, v, hedef)
                if ad == "failed":
                    entry["moved_to_failed"] = True
                log(f"{ad}/ klasorune tasindi: {week['name']}/{v['name']}")
            except Exception as e:
                log(f"{ad}/ tasima hatasi ({v['name']}): {e}")


PENDING_KEYS = ("pending_container", "pending_at", "pending_caption")


def resolve_pending(state):
    """Onceki calismadan kalan dogrulanmamis publish'leri cozer.

    Publish'ten hemen once container id'si state'e yazilir. Calisma o anda
    olurse (timeout, iptal, state kaydinin patlamasi) video kuyrukta kalir
    ve eskiden sonraki pencerede IKINCI KEZ paylasiliyordu. Artik container'a
    sorulur: PUBLISHED ise yayinlanmis sayilir, degilse kayit temizlenir ve
    video normal sirasiyla tekrar denenir.

    IG'ye hic ulasilamazsa (ag, token, rate limit) TransientError firlatir -
    o durumda bu calismada paylasim YAPILMAMALI.
    Degisiklik olduysa True dondurur.
    """
    changed = False
    for vid, entry in video_entries(state):
        cid = entry.get("pending_container")
        if not cid:
            continue
        ad = entry.get("name", vid)
        since = _parse_utc(entry.get("pending_at"))

        if entry.get("published"):
            code, media_id = "PUBLISHED", entry.get("media_id")
        else:
            media_id = None
            try:
                code, _ = container_status(cid)
            except (FileError, FileTransientError) as e:
                # Container artik sorgulanamiyor (silinmis/gecersiz) - yedek
                # kanit: son paylasimlarda ayni caption var mi
                log(f"Bekleyen container {cid} sorgulanamadi ({e}); "
                    f"caption ile araniyor")
                media_id = find_recent_media(entry.get("pending_caption"), since)
                code = "PUBLISHED" if media_id else None

        if code == "PUBLISHED":
            if not entry.get("published"):
                media_id = media_id or find_recent_media(
                    entry.get("pending_caption"), since)
                entry.update({
                    "published": True,
                    "media_id": media_id,
                    "container_id": cid,
                    "published_at": entry.get("pending_at")
                    or datetime.now(timezone.utc).isoformat(),
                })
                log(f"Onceki calisma {ad} videosunu YAYINLAMIS ama kaydedememis "
                    f"(container {cid} PUBLISHED). Yayinlandi olarak isaretlendi - "
                    f"tekrar paylasilmayacak.")
        else:
            log(f"Bekleyen container {cid} ({ad}) yayinlanmamis (durum: {code}); "
                f"video kuyrukta sirasini koruyor.")
        for k in PENDING_KEYS:
            entry.pop(k, None)
        changed = True
    return changed


# --------------------------------------------------------------------------
# Ana akis
# --------------------------------------------------------------------------

def run_dry(drive, weeks, state):
    """Instagram'a hicbir sey gondermeden tum zinciri dogrular."""
    log("DRY RUN - Instagram'a istek gonderilmeyecek, state yazilmayacak")
    log(f"Hafta sirasi: {', '.join(w['name'] for w in weeks) or '(yok)'}")

    for vid, entry in video_entries(state):
        if entry.get("pending_container"):
            log(f"Dogrulanmayi bekleyen paylasim: {entry.get('name', vid)} "
                f"(container {entry['pending_container']}) - canli calisma "
                f"IG'ye sorup karar verecek")

    scanned = scan_weeks(drive, weeks)
    ertelenen = deferred_names(scanned, state)
    if ertelenen:
        log(f"Ertelenmis videolar: {', '.join(ertelenen)}")

    job, _, remaining = pick_job(scanned, state)
    if not job:
        log("Kuyrukta yayinlanacak video yok")
        return 0

    caption, caption_file = resolve_caption(drive, job)
    size = int(job["video"].get("size") or 0)
    bekleyen = sum(1 for v in job["videos"] if eligible(v, state))

    log(f"Hafta           : {job['week']['name']}")
    log(f"Secilecek video : {job['video']['name']}")
    log(f"Boyut           : {size / 1024 / 1024:.1f} MB "
        f"({'SINIR ASILDI' if size > MAX_VIDEO_BYTES else 'uygun'})")
    log(f"Caption dosyasi : {caption_file['name'] if caption_file else 'yok (sablon)'}")
    log(f"Caption uzunlugu: {ig_len(caption)}/{CAPTION_MAX_CHARS}")
    log(f"Etiketler       : {', '.join(USER_TAGS) if USER_TAGS else 'yok'}")
    log(f"Bu haftada sira : {bekleyen} video bekliyor")
    log(f"Kuyrukta toplam : {remaining} video bekliyor")
    log("--- caption ---")
    print(caption, flush=True)
    log("--- caption sonu ---")

    tmp_path = temp_video_path(job["video"], prefix="dryrun_")
    try:
        log("Indirme dogrulaniyor...")
        actual = download_file(drive, job["video"], tmp_path)
        log(f"Indirme tamam: {actual} bayt - Drive erisimi ve butunluk OK")
    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    log("DRY RUN basarili - canli calisma icin DRY_RUN degiskenini kaldirin")
    return 0


def _safe_save(drive, state_file_id, state):
    """state kaydi da patlarsa asil hatayi golgede birakmasin. Dosya id'si dondurur."""
    try:
        return save_state(drive, state_file_id, state) or state_file_id
    except Exception as e:
        log(f"UYARI: state.json kaydedilemedi: {e}")
        return state_file_id


def handle_empty_queue(drive, state_file_id, state, scanned):
    """Kuyruk bos: gunde BIR KEZ basarisiz cikar ki bildirim gitsin.

    Eskiden exit 0'di - icerik bitince otomasyon sessizce dururdu. Her
    calismada basarisiz olmak ise pencere icinde saatte bir bildirim demek.
    """
    meta = state.setdefault(META_KEY, {})
    ertelenen = deferred_names(scanned, state)
    if ertelenen:
        log(f"Kuyrukta su an paylasilabilecek video yok. Ertelenmis "
            f"({DEFER_HOURS} saat sonra tekrar denenecek): {', '.join(ertelenen)}")
    else:
        log("Kuyrukta yayinlanacak video yok")

    today = local_now().date().isoformat()
    if meta.get("empty_notified") == today:
        log("(Bugun zaten bildirildi, bu calisma basarili sayiliyor.)")
        _safe_save(drive, state_file_id, state)
        return 0

    meta["empty_notified"] = today
    _safe_save(drive, state_file_id, state)
    log("!!! KUYRUK BOS - hafta klasorlerine yeni video eklenene kadar paylasim "
        "yapilmayacak. Bildirim gitsin diye bu calisma BASARISIZ sayiliyor "
        "(gunde bir kez).")
    return 1


def main():
    log(f"rotakesit reels - {'DRY RUN' if DRY_RUN else 'canli mod'}")

    # Pencere kontrolu EN BASTA: saat basi calisiyoruz, gunun buyuk kisminda
    # hicbir sey yapmadan cikmaliyiz - Drive/IG cagrisi bile yapmadan.
    pencere = None
    if ENFORCE_WINDOW and not DRY_RUN:
        simdi = local_now()
        pencereler = parse_windows(POST_WINDOWS)
        if not pencereler:
            log(f"HATA - POST_WINDOWS ({POST_WINDOWS!r}) gecerli bir pencere "
                f"icermiyor; hic paylasim yapilamaz. Ornek: 9-14,17-22")
            return 1
        pencere = current_window(simdi, pencereler)
        if not pencere:
            log(f"Saat {simdi:%H:%M} (TR) paylasim penceresi disinda "
                f"({POST_WINDOWS}). Bir sey yapilmadi.")
            return 0
        log(f"Paylasim penceresi: {pencere[0]:02d}-{pencere[1]:02d} TR, "
            f"su an {simdi:%H:%M}")
    check_token_expiry()

    drive = drive_client()
    root_entries, published_folder, failed_folder, weeks = discover_root(drive)
    state_file_id, state, state_ok = load_state(drive, root_entries)

    if not state_ok:
        log("!!! state.json var ama okunamadi. Uzerine yazip gecmisi silmemek "
            "icin cikiliyor. Dosyayi Drive'da kontrol edin (bozuk JSON olabilir).")
        return 1

    if DRY_RUN:
        return run_dry(drive, weeks, state)

    # Onceki calisma publish sirasinda yarida kaldiysa ONCE onu coz -
    # yoksa ayni video ikinci kez paylasilabilir
    try:
        if resolve_pending(state):
            state_file_id = _safe_save(drive, state_file_id, state)
    except TransientError as e:
        log(f"GECICI HATA: bekleyen paylasim dogrulanamadi ({e}). Cift paylasim "
            f"riskine girmemek icin bu calismada paylasim yapilmadi.")
        return 1

    if pencere and posted_in_window(state, local_now(), pencere):
        log(f"Bu pencerede ({pencere[0]:02d}-{pencere[1]:02d} TR) bugun zaten "
            f"paylasim yapilmis. Bir sey yapilmadi.")
        return 0

    if MIN_INTERVAL_HOURS > 0:
        last = last_published_at(state)
        if last:
            elapsed = datetime.now(timezone.utc) - last
            if elapsed < timedelta(hours=MIN_INTERVAL_HOURS):
                kalan = timedelta(hours=MIN_INTERVAL_HOURS) - elapsed
                log(f"Son paylasim {elapsed.total_seconds() / 3600:.1f} saat once "
                    f"yapilmis. {MIN_INTERVAL_HOURS} saatlik aralik dolmadan yeni "
                    f"paylasim yapilmaz ({kalan.total_seconds() / 3600:.1f} saat kaldi).")
                log("Bu, ust uste tetikleyicilerin gunluk paylasim sayisini "
                    "ikiye katlamasini onler. MIN_INTERVAL_HOURS ile ayarlanir.")
                return 0

    scanned = scan_weeks(drive, weeks)
    sweep(drive, scanned, state, published_folder, failed_folder)

    job, live_ids, remaining = pick_job(scanned, state)
    prune_state(state, live_ids)
    meta = state.setdefault(META_KEY, {})
    meta["queue_remaining"] = remaining
    meta["queue_checked_at"] = datetime.now(timezone.utc).isoformat()

    if not job:
        return handle_empty_queue(drive, state_file_id, state, scanned)

    video = job["video"]
    log(f"Secilen: {job['week']['name']}/{video['name']} "
        f"(kuyrukta {remaining} video)")
    entry = state.setdefault(video["id"], {"retries": 0})
    entry["name"] = video["name"]
    entry["week"] = job["week"]["name"]
    caption, _caption_file = resolve_caption(drive, job)

    # Indirmeden once boyut kontrolu - bosuna 20 dakika harcamayalim
    declared = int(video.get("size") or 0)
    if declared > MAX_VIDEO_BYTES:
        entry["retries"] = MAX_RETRIES
        entry["last_error"] = f"Video {declared / 1024 / 1024:.0f} MB, IG siniri 1024 MB"
        entry["last_error_kind"] = "file"
        entry["last_attempt"] = datetime.now(timezone.utc).isoformat()
        state[video["id"]] = entry
        _safe_save(drive, state_file_id, state)
        log(f"HATA: {entry['last_error']} - sonraki calismada failed/ klasorune")
        return 1

    tmp_path = temp_video_path(video)
    container_id = None
    publish_started = None
    published_ok = False
    media_id = None

    try:
        log("Drive'dan indiriliyor...")
        actual = download_file(drive, video, tmp_path)
        log(f"Indirildi: {actual / 1024 / 1024:.1f} MB")

        last_err = None
        for deneme in range(1, CONTAINER_ATTEMPTS + 1):
            if deneme > 1 and time_left() < 10 * 60:
                log(f"Calisma butcesinde {time_left() / 60:.0f} dk kaldi, yeni "
                    f"container denenmiyor (workflow timeout'una takilmasin)")
                break
            try:
                container_id = create_container(caption)
                log(f"Container: {container_id}"
                    + (f" (deneme {deneme}/{CONTAINER_ATTEMPTS})" if deneme > 1 else ""))
                upload_video(container_id, tmp_path)
                wait_until_finished(container_id)
                break
            except TransientError as err:
                # Patlayan container kullanilamaz, sifirdan yenisi acilir
                last_err, container_id = err, None
                if deneme < CONTAINER_ATTEMPTS:
                    log(f"Gecici hata, yeni container ile tekrar: {err}")
                    time.sleep(10)
        if container_id is None:
            raise last_err

        # Publish'ten ONCE isaretle: calisma bundan sonra olurse (timeout,
        # iptal, state kaydinin patlamasi) sonraki calisma container'a sorar
        entry["pending_container"] = container_id
        entry["pending_at"] = datetime.now(timezone.utc).isoformat()
        entry["pending_caption"] = normalize_caption_head(caption)
        state[video["id"]] = entry
        try:
            state_file_id = save_state(drive, state_file_id, state) or state_file_id
        except Exception as e:
            log(f"UYARI: bekleyen paylasim isareti kaydedilemedi ({e}); "
                f"yine de yayinlaniyor")

        publish_started = datetime.now(timezone.utc)
        media_id = publish(container_id)
        published_ok = True
        log(f"YAYINLANDI - media_id: {media_id}")

    except Exception as e:
        # Yayin gercekten olmus ama yanit kaybolmus olabilir
        if publish_started is not None:
            durum, found = recover_publish(container_id, caption, publish_started)
            if durum == "published":
                published_ok, media_id = True, found
                log(f"Hata alindi ama reel yayinlanmis (container PUBLISHED, "
                    f"media_id: {found or 'bulunamadi'}) - cift paylasim "
                    f"engellendi. Bastirilan hata: {e}")
            elif durum == "unknown":
                log(f"!!! Publish sonucu belirsiz ({e}). IG'ye ulasilamadi; "
                    f"bekleyen isaret state'te kaliyor, sonraki calisma "
                    f"container'a sorup karar verecek. Sayaclar degismedi.")
                _safe_save(drive, state_file_id, state)
                return 1
            else:
                for k in PENDING_KEYS:
                    entry.pop(k, None)

        if not published_ok:
            entry["last_error"] = redact(e)[:500]
            entry["last_attempt"] = datetime.now(timezone.utc).isoformat()

            # FileError disindaki her sey gecici sayilir: token, kota, ag, izin.
            # Bunlarda sayaci artirmak saglam videolari failed/ klasorune surer.
            if isinstance(e, FileError):
                entry["retries"] = entry.get("retries", 0) + 1
                entry["last_error_kind"] = "file"
                state[video["id"]] = entry
                _safe_save(drive, state_file_id, state)
                log(f"DOSYA HATASI (deneme {entry['retries']}/{MAX_RETRIES}): {e}")
                if entry["retries"] >= MAX_RETRIES:
                    log("Deneme hakki bitti - sonraki calismada failed/ klasorune")
            else:
                entry["last_error_kind"] = "transient"
                sabit = (f"retry sayaci {entry.get('retries', 0)}/{MAX_RETRIES} "
                         f"sabit kaldi")
                if isinstance(e, FileTransientError):
                    n = entry.get("transient_retries", 0) + 1
                    if n >= MAX_TRANSIENT_RETRIES:
                        entry["transient_retries"] = 0
                        entry["deferrals"] = entry.get("deferrals", 0) + 1
                        entry["deferred_until"] = (
                            datetime.now(timezone.utc)
                            + timedelta(hours=DEFER_HOURS)).isoformat()
                        log(f"GECICI HATA ({n}. kez ust uste, {sabit}): {e}")
                        log(f"Video {DEFER_HOURS} saat ERTELENDI, sonraki calisma "
                            f"siradaki videoya gecer. failed/'a tasinmadi.")
                    else:
                        entry["transient_retries"] = n
                        log(f"GECICI HATA (videoya bagli {n}/{MAX_TRANSIENT_RETRIES}, "
                            f"{sabit}): {e}")
                        log("Video kuyrukta sirasini koruyor; ust uste "
                            f"{MAX_TRANSIENT_RETRIES} kez olursa ertelenecek.")
                else:
                    log(f"GECICI HATA ({sabit}): {e}")
                    log("Sebep dosya degil (token/kota/ag/izin). Sorunu giderin; "
                        "video kuyrukta sirasini koruyor.")
                state[video["id"]] = entry
                _safe_save(drive, state_file_id, state)
            return 1

    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # --- Buradan sonrasi: yayin KESIN basarili ---
    entry.update({
        "published": True,
        "media_id": media_id,
        "container_id": container_id,
        "week": job["week"]["name"],
        "name": video["name"],
        "published_at": datetime.now(timezone.utc).isoformat(),
    })
    for k in PENDING_KEYS + ("transient_retries", "deferred_until"):
        entry.pop(k, None)
    state[video["id"]] = entry
    meta["queue_remaining"] = max(remaining - 1, 0)

    # Once state: tasima patlasa bile ikinci kez paylasilmasin.
    # drive_exec zaten DRIVE_RETRIES kez tekrar dener.
    saved = True
    try:
        state_file_id = save_state(drive, state_file_id, state) or state_file_id
    except Exception as e:
        saved = False
        log(f"!!! Reel yayinlandi ama state.json yazilamadi: {e}")

    # Tasima state yazilamasa da denenir: video kuyruktan cikinca bir daha
    # secilemez. Caption dosyasi yerinde birakilir (Capitons/ bir kutuphane).
    try:
        move_file(drive, video, published_folder)
        log("published/ klasorune tasindi")
    except Exception as e:
        log(f"UYARI: reel yayinlandi ama dosya tasinamadi ({e}). "
            f"Sonraki calisma tekrar deneyecek.")

    log(f"Kuyrukta {meta['queue_remaining']} video kaldi")
    if not saved:
        log("Sonraki calisma bekleyen isaretle container'i IG'ye sorup yayini "
            "dogrulayacak; tekrar paylasilmaz. Bildirim icin calisma "
            "BASARISIZ sayiliyor.")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("Iptal edildi")
        sys.exit(130)
