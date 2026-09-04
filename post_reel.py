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
  5. Basarili: videoyu kokteki published/ klasorune tasi
  6. Hatali: SADECE dosyaya ozgu hatalarda retry sayacini artir;
     token/kota/ag hatalari sayaci yakmaz. MAX_RETRIES'te failed/ klasorune.

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

# Klasor adi eslesmeleri - hepsi harf duyarsiz
REELS_NAMES = ("reels", "reel", "videolar")
# "Capitons" mevcut yazim; "Captions" ileride duzeltilirse de calissin
CAPTION_NAMES = ("capitons", "captions", "caption", "captionlar")
# Kok altinda hafta sayilmayacak klasorler
SKIP_ROOT_NAMES = {"published", "failed", "retry", "archive", "arsiv"}

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
STATE_FILENAME = "state.json"

POLL_INTERVAL = 5                                      # saniye
POLL_TIMEOUT = _int_env("POLL_TIMEOUT", 480)           # 8 dk
UPLOAD_TIMEOUT = _int_env("UPLOAD_TIMEOUT", 600)       # 10 dk / deneme
UPLOAD_ATTEMPTS = _int_env("UPLOAD_ATTEMPTS", 3)
# En kotu senaryo ~ indirme + 10 dk + 8 dk. Workflow timeout'u 45 dk.

VIDEO_EXTS = (".mp4", ".mov")
MAX_VIDEO_BYTES = 1024 * 1024 * 1024                   # IG Reels siniri: 1 GB

CAPTION_MAX_CHARS = 2200
CAPTION_MAX_HASHTAGS = 30

TOKEN_WARN_DAYS = 7

# Ust uste tetikleyicilere karsi koruma. Harici tetikleyici 18:00'de,
# cron 18:17'de calisirsa ikisi FARKLI videolar paylasir - state.json bunu
# engellemez, o sadece AYNI videonun tekrarini engeller. Bu esik, son
# paylasimdan bu yana yeterli sure gecmediyse calismayi sessizce bitirir.
# 0 = kapali. Gunde 2 paylasim icin 6 saat guvenli (aralar 9 ve 15 saat).
MIN_INTERVAL_HOURS = _int_env("MIN_INTERVAL_HOURS", 6)
STATE_RETENTION_DAYS = _int_env("STATE_RETENTION_DAYS", 90)

# Graph API hata kodlari - bunlar dosyanin sucu degil, retry sayacini yakmasinlar
TRANSIENT_GRAPH_CODES = {
    1,    # Unknown / gecici
    2,    # Service temporarily unavailable
    4,    # Application request limit reached
    10,   # Permission denied
    17,   # User request limit reached
    32,   # Page request limit reached
    100,  # Invalid parameter - pratikte konfig hatasi
    102,  # Session expired
    190,  # Access token gecersiz / suresi dolmus
    200,  # Permissions error
    341,  # Application limit reached
    368,  # Temporarily blocked
    613,  # Rate limit
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
    return FileError(detail)


def drive_exec(request, context):
    """Drive cagrilarini calistirir, hatalari siniflandirir.

    403 (izin/kota), 404 (paylasilmamis), 429 (rate limit), 5xx (sunucu) -
    hicbiri videonun sucu degil, hepsi TransientError.
    """
    try:
        return request.execute()
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


def download_file(drive, meta, dest_path):
    """Indirir ve Drive'in bildirdigi boyutla karsilastirir."""
    request = drive.files().get_media(fileId=meta["id"], supportsAllDrives=True)
    try:
        with io.FileIO(dest_path, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request, chunksize=8 * 1024 * 1024)
            done = False
            while not done:
                _, done = downloader.next_chunk()
    except HttpError as e:
        raise TransientError(
            f"Indirme hatasi: Drive HTTP {getattr(e.resp, 'status', 0)}") from e
    except Exception as e:
        raise TransientError(f"Indirme hatasi: {type(e).__name__} {e}") from e

    expected = int(meta.get("size") or 0)
    actual = os.path.getsize(dest_path)
    if expected and actual != expected:
        raise FileError(
            f"Indirme eksik: {actual} bayt indi, {expected} bayt bekleniyordu")
    return actual


def read_text_file(drive, file_id):
    """UTF-8 dener, olmazsa cp1254 (Windows Notepad), o da olmazsa kayipli."""
    data = drive_exec(
        drive.files().get_media(fileId=file_id, supportsAllDrives=True),
        f"Metin dosyasi okunamadi ({file_id})",
    )
    for enc in ("utf-8-sig", "utf-8", "cp1254"):
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
    """
    for f in root_entries:
        if f["name"] == STATE_FILENAME:
            try:
                raw = read_text_file(drive, f["id"])
                return f["id"], json.loads(raw or "{}"), True
            except Exception as e:
                log(f"state.json okunamadi: {type(e).__name__} {e}")
                return f["id"], {}, False
    return None, {}, True


def save_state(drive, state_file_id, state):
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
            else:
                drive_exec(
                    drive.files().create(
                        body={"name": STATE_FILENAME, "parents": [ROOT_FOLDER_ID]},
                        media_body=media, fields="id", supportsAllDrives=True,
                    ),
                    "state.json olusturulamadi (service account kullaniyorsaniz "
                    "kotasi 0'dir; OAuth'a gecin veya dosyayi elle olusturun)",
                )
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


def last_published_at(state):
    """state.json'daki en son basarili paylasim zamani. Yoksa None."""
    stamps = []
    for entry in state.values():
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
    for file_id, entry in state.items():
        if file_id in live_ids:
            continue
        stamp = entry.get("published_at") or entry.get("last_attempt")
        if not stamp:
            continue
        try:
            when = datetime.fromisoformat(stamp)
        except ValueError:
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


def normalize_caption(text):
    """IG limitlerine uydurur: 30 hashtag, 2200 karakter."""
    tags = re.findall(r"#\w+", text, flags=re.UNICODE)
    if len(tags) > CAPTION_MAX_HASHTAGS:
        fazla = tags[CAPTION_MAX_HASHTAGS:]
        for tag in fazla:
            text = text.replace(tag, "", 1)
        text = re.sub(r"[ \t]{2,}", " ", text).strip()
        log(f"UYARI: {len(tags)} hashtag vardi, son {len(fazla)} tanesi cikarildi "
            f"(IG siniri {CAPTION_MAX_HASHTAGS})")
    if len(text) > CAPTION_MAX_CHARS:
        text = text[:CAPTION_MAX_CHARS - 1].rstrip() + "..."
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

        if r.status_code >= 500 or r.status_code == 429:
            last_error = f"HTTP {r.status_code}: {r.text[:200]}"
            log(f"Yukleme reddedildi ({last_error}), tekrar denenecek")
            offset = upload_offset(container_id)
            continue

        raise graph_failure(r, "Yukleme basarisiz")

    raise TransientError(
        f"Yukleme {UPLOAD_ATTEMPTS} denemede tamamlanamadi. Son hata: {last_error}")


def wait_until_finished(container_id):
    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
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

        if r.status_code >= 500:
            log(f"Durum sorgusu HTTP {r.status_code}, tekrar denenecek")
            time.sleep(POLL_INTERVAL)
            continue

        code = body.get("status_code")
        if code == "FINISHED":
            return
        if code == "ERROR":
            # Islemede hata = videonun kendisiyle ilgili (codec, sure, en-boy)
            raise FileError(f"Container ERROR: {body.get('status')}")
        if code == "EXPIRED":
            raise TransientError(f"Container EXPIRED: {body.get('status')}")
        log(f"Isleniyor... ({code})")
        time.sleep(POLL_INTERVAL)
    raise TransientError(f"Isleme {POLL_TIMEOUT}s icinde bitmedi")


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


def find_recent_media(caption, minutes=60):
    """Publish yaniti kaybolduysa gercekten yayinlanip yayinlanmadigini dogrular.

    Bu olmadan: IG paylasimi yapar ama yanit timeout'a duser -> state'e
    yazilmaz -> video kuyrukta kalir -> ayni reel ikinci kez paylasilir.
    """
    head = (caption or "").strip()[:80]
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

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    for item in data:
        try:
            when = datetime.strptime(item.get("timestamp", ""),
                                     "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            continue
        if when < cutoff:
            continue
        if (item.get("caption") or "").strip().startswith(head):
            return item.get("id")
    return None


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

    weeks = [f for f in folders
             if f["name"].strip().lower() not in SKIP_ROOT_NAMES
             and f["id"] not in (published, failed)]
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


def eligible(video, state):
    entry = state.get(video["id"], {})
    return (video["name"].lower().endswith(VIDEO_EXTS)
            and not entry.get("published")
            and entry.get("retries", 0) < MAX_RETRIES)


def pick_job(drive, weeks, state):
    """Hafta sirasiyla ilk uygun videoyu bulur; kuyruktaki tum id'leri de toplar."""
    job, seen_ids = None, set()
    for week in weeks:
        reels, videos, captions = week_contents(drive, week)
        seen_ids.update(v["id"] for v in videos)
        if reels is None:
            log(f"UYARI: {week['name']} icinde Reels klasoru veya video yok, atlandi")
            continue
        if job is None:
            adaylar = sorted((v for v in videos if eligible(v, state)),
                             key=lambda f: natural_key(f["name"]))
            if adaylar:
                job = {"week": week, "reels": reels, "video": adaylar[0],
                       "videos": videos, "captions": captions}
    return job, seen_ids


def sweep_exhausted(drive, weeks, state, failed_folder):
    """MAX_RETRIES'i asmis videolari failed/ klasorune tasir - kuyruk tikanmasin.

    Caption'lar YERINDE KALIR: Capitons/ bir kutuphane, sadece video hareket eder.
    """
    for week in weeks:
        reels, videos, _ = week_contents(drive, week)
        if reels is None:
            continue
        for v in videos:
            entry = state.get(v["id"], {})
            if entry.get("retries", 0) < MAX_RETRIES or entry.get("moved_to_failed"):
                continue
            try:
                move_file(drive, v, failed_folder)
                entry["moved_to_failed"] = True
                state[v["id"]] = entry
                log(f"failed/ klasorune tasindi: {week['name']}/{v['name']}")
            except Exception as e:
                log(f"failed/ tasima hatasi ({v['name']}): {e}")


# --------------------------------------------------------------------------
# Ana akis
# --------------------------------------------------------------------------

def run_dry(drive, weeks, state):
    """Instagram'a hicbir sey gondermeden tum zinciri dogrular."""
    log("DRY RUN - Instagram'a istek gonderilmeyecek, state yazilmayacak")
    log(f"Hafta sirasi: {', '.join(w['name'] for w in weeks) or '(yok)'}")

    job, _ = pick_job(drive, weeks, state)
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
    log(f"Etiketler       : {', '.join(USER_TAGS) if USER_TAGS else 'yok'}")
    log(f"Bu haftada sira : {bekleyen} video bekliyor")
    log("--- caption ---")
    print(caption, flush=True)
    log("--- caption sonu ---")

    tmp_path = os.path.join(tempfile.gettempdir(), f"dryrun_{job['video']['name']}")
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
    """state kaydi da patlarsa asil hatayi golgede birakmasin."""
    try:
        save_state(drive, state_file_id, state)
    except Exception as e:
        log(f"UYARI: state.json kaydedilemedi: {e}")


def main():
    log(f"rotakesit reels - {'DRY RUN' if DRY_RUN else 'canli mod'}")
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

    sweep_exhausted(drive, weeks, state, failed_folder)

    job, live_ids = pick_job(drive, weeks, state)
    prune_state(state, live_ids)

    if not job:
        log("Kuyrukta yayinlanacak video yok")
        _safe_save(drive, state_file_id, state)
        return 0

    video = job["video"]
    log(f"Secilen: {job['week']['name']}/{video['name']}")
    entry = state.setdefault(video["id"], {"retries": 0})
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

    tmp_path = os.path.join(tempfile.gettempdir(), video["name"])
    container_id = None
    media_id = None

    try:
        log("Drive'dan indiriliyor...")
        actual = download_file(drive, video, tmp_path)
        log(f"Indirildi: {actual / 1024 / 1024:.1f} MB")

        container_id = create_container(caption)
        log(f"Container: {container_id}")

        upload_video(container_id, tmp_path)
        wait_until_finished(container_id)

        media_id = publish(container_id)
        log(f"YAYINLANDI - media_id: {media_id}")

    except Exception as e:
        # Yayin gercekten olmus ama yanit kaybolmus olabilir
        if container_id is not None:
            recovered = find_recent_media(caption)
            if recovered:
                media_id = recovered
                log(f"Hata alindi ama reel yayinlanmis (media_id: {media_id}) - "
                    f"cift paylasim engellendi. Bastirilan hata: {e}")

        if not media_id:
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
                state[video["id"]] = entry
                _safe_save(drive, state_file_id, state)
                log(f"GECICI HATA (retry sayaci {entry.get('retries', 0)}"
                    f"/{MAX_RETRIES} sabit kaldi): {e}")
                log("Sebep dosya degil (token/kota/ag/izin). Sorunu giderin; "
                    "video kuyrukta sirasini koruyor.")
            return 1

    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # --- Buradan sonrasi: yayin KESIN basarili ---
    # Once state'e yaz: tasima patlasa bile ikinci kez paylasilmasin
    entry.update({
        "published": True,
        "media_id": media_id,
        "week": job["week"]["name"],
        "name": video["name"],
        "published_at": datetime.now(timezone.utc).isoformat(),
    })
    state[video["id"]] = entry
    save_state(drive, state_file_id, state)

    # Tasima hatasi yayini gecersiz kilmaz - job'u FAIL ETME, sadece uyar.
    # Caption dosyasi yerinde birakilir (Capitons/ bir kutuphane).
    try:
        move_file(drive, video, published_folder)
        log("published/ klasorune tasindi")
    except Exception as e:
        log(f"UYARI: reel yayinlandi ama dosya tasinamadi ({e}). "
            f"Drive'da elle tasiyin. Tekrar paylasilmaz (state'te isaretli).")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("Iptal edildi")
        sys.exit(130)
