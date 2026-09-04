#!/usr/bin/env python3
"""
rotakesit - Instagram Reels otomasyonu

Akis:
  1. Drive QUEUE klasorunden siradaki videoyu sec (dogal siralama)
  2. Ayni isimli .txt varsa caption'i oradan al, yoksa varsayilan sablon
  3. Videoyu indir + boyut dogrula -> IG resumable upload -> poll -> publish
  4. Basarili: dosyayi (ve caption'ini) PUBLISHED klasorune tasi
  5. Hatali: SADECE dosyaya ozgu hatalarda retry sayacini artir;
     token/kota/ag hatalari sayaci yakmaz. MAX_RETRIES'te FAILED'a tasi.

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
from google.oauth2 import service_account
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
    Retry sayaci ARTMAZ - yoksa saglam videolar FAILED'a surulur."""


class FileError(Exception):
    """Dosyaya ozgu hata (bozuk video, IG spec reddi, boyut).
    Retry sayaci ARTAR."""


# --------------------------------------------------------------------------
# Env dogrulama
#
# NOT: GitHub'da tanimsiz bir secret KeyError vermez, BOS STRING olur.
# Bu yuzden os.environ[...] korumasi yetmez; acik kontrol sart.
# --------------------------------------------------------------------------

REQUIRED_ENV = (
    "IG_USER_ID",
    "IG_ACCESS_TOKEN",
    "GOOGLE_SERVICE_ACCOUNT_JSON",
    "DRIVE_QUEUE_FOLDER_ID",
    "DRIVE_PUBLISHED_FOLDER_ID",
    "DRIVE_FAILED_FOLDER_ID",
)


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

    folders = {
        "DRIVE_QUEUE_FOLDER_ID": os.environ["DRIVE_QUEUE_FOLDER_ID"].strip(),
        "DRIVE_PUBLISHED_FOLDER_ID": os.environ["DRIVE_PUBLISHED_FOLDER_ID"].strip(),
        "DRIVE_FAILED_FOLDER_ID": os.environ["DRIVE_FAILED_FOLDER_ID"].strip(),
    }
    if len(set(folders.values())) != 3:
        sys.exit(
            "HATA - uc Drive klasor ID'si birbirinden farkli olmali. "
            "Ayni deger girilmis, dosyalar kendi klasorune tasinmaya calisir."
        )

    try:
        json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    except json.JSONDecodeError as e:
        sys.exit(f"HATA - GOOGLE_SERVICE_ACCOUNT_JSON gecerli JSON degil: {e}")


_validate_env()


# --------------------------------------------------------------------------
# Konfig
# --------------------------------------------------------------------------

DRY_RUN = _bool_env("DRY_RUN")

GRAPH_VERSION = os.environ.get("GRAPH_VERSION", "").strip() or "v23.0"
GRAPH_HOST = f"https://graph.facebook.com/{GRAPH_VERSION}"
RUPLOAD_HOST = f"https://rupload.facebook.com/ig-api-upload/{GRAPH_VERSION}"

IG_USER_ID = os.environ["IG_USER_ID"].strip()
IG_ACCESS_TOKEN = os.environ["IG_ACCESS_TOKEN"].strip()

# Opsiyonel - varsa token omru dogru sekilde sorgulanir ve yenileme yapilabilir
IG_APP_ID = os.environ.get("IG_APP_ID", "").strip()
IG_APP_SECRET = os.environ.get("IG_APP_SECRET", "").strip()

QUEUE_FOLDER_ID = os.environ["DRIVE_QUEUE_FOLDER_ID"].strip()
PUBLISHED_FOLDER_ID = os.environ["DRIVE_PUBLISHED_FOLDER_ID"].strip()
FAILED_FOLDER_ID = os.environ["DRIVE_FAILED_FOLDER_ID"].strip()

# Etiketler - Reels'te user_tags sadece username alir, x/y koordinati yok
USER_TAGS = [
    u.strip().lstrip("@")
    for u in os.environ.get("USER_TAGS", "rota,rotaile,ramedyaresmi").split(",")
    if u.strip()
]

# .txt bulunamazsa kullanilacak sablon. {name} = uzantisiz dosya adi
DEFAULT_CAPTION = os.environ.get(
    "DEFAULT_CAPTION",
    "{name}\n\n@rota @rotaile @ramedyaresmi\n\n#rotakesit #kesit #video",
)

MAX_RETRIES = _int_env("MAX_RETRIES", 3)
STATE_FILENAME = "state.json"

POLL_INTERVAL = 5                                      # saniye
POLL_TIMEOUT = _int_env("POLL_TIMEOUT", 480)           # 8 dk
UPLOAD_TIMEOUT = _int_env("UPLOAD_TIMEOUT", 600)       # 10 dk / deneme
UPLOAD_ATTEMPTS = _int_env("UPLOAD_ATTEMPTS", 3)
# En kotu senaryo ~ indirme + 10 dk + 8 dk. Workflow timeout'u 45 dk (bkz reels.yml)

VIDEO_EXTS = (".mp4", ".mov")
MAX_VIDEO_BYTES = 1024 * 1024 * 1024                   # IG Reels siniri: 1 GB

CAPTION_MAX_CHARS = 2200
CAPTION_MAX_HASHTAGS = 30

TOKEN_WARN_DAYS = 7
STATE_RETENTION_DAYS = _int_env("STATE_RETENTION_DAYS", 90)
# 0 = kapali. Acilirsa PUBLISHED'daki eski dosyalar Drive COP KUTUSUNA tasinir
# (kalici silme yok, 30 gun icinde geri alinabilir).
PUBLISHED_RETENTION_DAYS = _int_env("PUBLISHED_RETENTION_DAYS", 0)

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
    """Token'in log'a veya state.json'a sizmasini engeller."""
    text = str(text)
    for secret in (IG_ACCESS_TOKEN, IG_APP_SECRET):
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
    # App token varsa debug_token'in dogru kullanimi bu
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
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def list_folder(drive, folder_id):
    """Klasordeki tum dosyalari dondurur (sayfalama dahil)."""
    files, page_token = [], None
    while True:
        resp = drive_exec(
            drive.files().list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields=("nextPageToken, files(id, name, mimeType, size, "
                        "createdTime, modifiedTime)"),
                pageSize=1000,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ),
            f"Klasor listelenemedi ({folder_id})",
        )
        files.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            return files


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
    """UTF-8 dener, olmazsa cp1254 (Windows Notepad), o da olmazsa kayipli decode."""
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


def move_file(drive, file_id, from_folder, to_folder):
    drive_exec(
        drive.files().update(
            fileId=file_id,
            addParents=to_folder,
            removeParents=from_folder,
            fields="id, parents",
            supportsAllDrives=True,
        ),
        f"Dosya tasinamadi ({file_id})",
    )


def trash_file(drive, file_id):
    """Kalici silmez - Drive cop kutusuna tasir, 30 gun geri alinabilir."""
    drive_exec(
        drive.files().update(fileId=file_id, body={"trashed": True},
                             supportsAllDrives=True),
        f"Dosya cope tasinamadi ({file_id})",
    )


# --------------------------------------------------------------------------
# Durum dosyasi (Drive'da QUEUE klasoru icinde state.json)
# --------------------------------------------------------------------------

def load_state(drive, files):
    """(state_file_id, state, ok) dondurur.

    ok=False ise state.json var ama okunamadi. O durumda UZERINE YAZMADAN
    cikmak gerekir - yoksa tum yayin gecmisi ve retry sayaclari silinir.
    """
    for f in files:
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
                        body={"name": STATE_FILENAME, "parents": [QUEUE_FOLDER_ID]},
                        media_body=media, fields="id", supportsAllDrives=True,
                    ),
                    "state.json olusturulamadi",
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


def prune_state(state, files):
    """Kuyrukta artik olmayan ve suresi gecmis kayitlari atar.

    Kuyrukta HALA duran hicbir kayda dokunmaz - yoksa 'published' isareti
    kaybolur ve video ikinci kez paylasilir.
    """
    if STATE_RETENTION_DAYS <= 0:
        return 0
    in_queue = {f["id"] for f in files}
    cutoff = datetime.now(timezone.utc) - timedelta(days=STATE_RETENTION_DAYS)
    dropped = []
    for file_id, entry in state.items():
        if file_id in in_queue:
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
        text = text[:CAPTION_MAX_CHARS - 1].rstrip() + "…"
        log(f"UYARI: caption {CAPTION_MAX_CHARS} karaktere kisaltildi")
    return text


def caption_file_for(files, video_name):
    """Ayni isimli .txt'yi HARF DUYARSIZ arar."""
    stem = os.path.splitext(video_name)[0].lower()
    for f in files:
        if f["name"].lower() == f"{stem}.txt":
            return f
    return None


def resolve_caption(drive, files, video_name):
    stem = os.path.splitext(video_name)[0]
    txt = caption_file_for(files, video_name)
    if txt:
        try:
            text = read_text_file(drive, txt["id"])
        except Exception as e:
            log(f"UYARI: {txt['name']} okunamadi ({e}), varsayilan sablona dusuluyor")
            text = ""
        if text:
            log(f"Caption kaynagi: {txt['name']}")
            return normalize_caption(text), txt["id"]
        log(f"UYARI: {txt['name']} bos, varsayilan sablona dusuluyor")
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
    """Video byte'larini rupload'a yukler; kopan yuklemeyi kaldigi yerden surdurur.

    Onceki surum upload_type=resumable ile session aciyor ama tek seferde
    offset=0'dan yukluyordu - 180. MB'ta kopan bir yukleme bastan basliyordu.
    """
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
# Ana akis
# --------------------------------------------------------------------------

def natural_key(name):
    """'10.mp4' > '2.mp4' olsun diye - duz sort bunun tersini yapar."""
    return [int(p) if p.isdigit() else p.lower()
            for p in re.split(r"(\d+)", name)]


def pick_next(files, state):
    """Dogal siralamada, MAX_RETRIES'i asmamis ilk video."""
    videos = [
        f for f in files
        if f["name"].lower().endswith(VIDEO_EXTS)
        and not state.get(f["id"], {}).get("published")
        and state.get(f["id"], {}).get("retries", 0) < MAX_RETRIES
    ]
    videos.sort(key=lambda f: natural_key(f["name"]))
    return videos[0] if videos else None


def sweep_exhausted(drive, files, state):
    """MAX_RETRIES'i asmis dosyalari (ve caption'larini) FAILED'a tasi."""
    for f in files:
        entry = state.get(f["id"], {})
        if entry.get("retries", 0) < MAX_RETRIES or entry.get("moved_to_failed"):
            continue
        try:
            move_file(drive, f["id"], QUEUE_FOLDER_ID, FAILED_FOLDER_ID)
            entry["moved_to_failed"] = True
            state[f["id"]] = entry
            log(f"FAILED'a tasindi: {f['name']}")
        except Exception as e:
            log(f"FAILED tasima hatasi ({f['name']}): {e}")
            continue

        # Caption dosyasi kuyrukta oksuz kalmasin
        txt = caption_file_for(files, f["name"])
        if txt:
            try:
                move_file(drive, txt["id"], QUEUE_FOLDER_ID, FAILED_FOLDER_ID)
                log(f"FAILED'a tasindi: {txt['name']}")
            except Exception as e:
                log(f"Caption tasima hatasi ({txt['name']}): {e}")


def cleanup_published(drive):
    """PUBLISHED klasorunu sinirsiz buyumekten korur. Varsayilan: kapali."""
    if PUBLISHED_RETENTION_DAYS <= 0:
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=PUBLISHED_RETENTION_DAYS)
    moved = 0
    try:
        for f in list_folder(drive, PUBLISHED_FOLDER_ID):
            stamp = f.get("createdTime") or f.get("modifiedTime") or ""
            when = None
            for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
                try:
                    when = datetime.strptime(stamp, fmt).replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    continue
            if when and when < cutoff:
                trash_file(drive, f["id"])
                moved += 1
    except Exception as e:
        log(f"PUBLISHED temizligi atlandi: {e}")
        return
    if moved:
        log(f"PUBLISHED temizligi: {moved} dosya cop kutusuna tasindi "
            f"({PUBLISHED_RETENTION_DAYS} gunden eski)")


def run_dry(drive, files, state):
    """Instagram'a hicbir sey gondermeden tum zinciri dogrular."""
    log("DRY RUN - Instagram'a istek gonderilmeyecek, state yazilmayacak")
    video = pick_next(files, state)
    if not video:
        log("Kuyrukta yayinlanacak video yok")
        return 0

    caption, caption_file_id = resolve_caption(drive, files, video["name"])
    size = int(video.get("size") or 0)
    log(f"Secilecek video : {video['name']}")
    log(f"Boyut           : {size / 1024 / 1024:.1f} MB "
        f"({'SINIR ASILDI' if size > MAX_VIDEO_BYTES else 'uygun'})")
    log(f"Caption dosyasi : {'var' if caption_file_id else 'yok (sablon)'}")
    log(f"Etiketler       : {', '.join(USER_TAGS) if USER_TAGS else 'yok'}")
    log("--- caption ---")
    print(caption, flush=True)
    log("--- caption sonu ---")

    tmp_path = os.path.join(tempfile.gettempdir(), f"dryrun_{video['name']}")
    try:
        log("Indirme dogrulaniyor...")
        actual = download_file(drive, video, tmp_path)
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
    files = list_folder(drive, QUEUE_FOLDER_ID)
    state_file_id, state, state_ok = load_state(drive, files)

    if not state_ok:
        log("!!! state.json var ama okunamadi. Uzerine yazip gecmisi silmemek "
            "icin cikiliyor. Dosyayi Drive'da kontrol edin (bozuk JSON olabilir).")
        return 1

    if DRY_RUN:
        return run_dry(drive, files, state)

    sweep_exhausted(drive, files, state)
    prune_state(state, files)
    cleanup_published(drive)

    video = pick_next(files, state)
    if not video:
        log("Kuyrukta yayinlanacak video yok")
        save_state(drive, state_file_id, state)
        return 0

    log(f"Secilen: {video['name']}")
    entry = state.setdefault(video["id"], {"retries": 0})
    caption, caption_file_id = resolve_caption(drive, files, video["name"])

    # Indirmeden once boyut kontrolu - bosuna 20 dakika harcamayalim
    declared = int(video.get("size") or 0)
    if declared > MAX_VIDEO_BYTES:
        entry["retries"] = MAX_RETRIES
        entry["last_error"] = f"Video {declared / 1024 / 1024:.0f} MB, IG siniri 1024 MB"
        entry["last_error_kind"] = "file"
        entry["last_attempt"] = datetime.now(timezone.utc).isoformat()
        state[video["id"]] = entry
        _safe_save(drive, state_file_id, state)
        log(f"HATA: {entry['last_error']} - sonraki calismada FAILED'a tasinacak")
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
            # Bunlarda sayaci artirmak saglam videolari FAILED'a surer.
            if isinstance(e, FileError):
                entry["retries"] = entry.get("retries", 0) + 1
                entry["last_error_kind"] = "file"
                state[video["id"]] = entry
                _safe_save(drive, state_file_id, state)
                log(f"DOSYA HATASI (deneme {entry['retries']}/{MAX_RETRIES}): {e}")
                if entry["retries"] >= MAX_RETRIES:
                    log("Deneme hakki bitti - sonraki calismada FAILED'a tasinacak")
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
        "published_at": datetime.now(timezone.utc).isoformat(),
    })
    state[video["id"]] = entry
    save_state(drive, state_file_id, state)

    # Tasima hatasi yayini gecersiz kilmaz - job'u FAIL ETME, sadece uyar
    try:
        move_file(drive, video["id"], QUEUE_FOLDER_ID, PUBLISHED_FOLDER_ID)
        if caption_file_id:
            move_file(drive, caption_file_id, QUEUE_FOLDER_ID, PUBLISHED_FOLDER_ID)
        log("PUBLISHED klasorune tasindi")
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
