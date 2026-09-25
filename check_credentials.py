#!/usr/bin/env python3
"""
Kimlik bilgisi omur denetimi.

Bu otomasyonun tum sessiz olum sebepleri burada tek yerde toplaniyor.
Haftalik calisir (keepalive.yml); bir sey esige yaklasinca is BASARISIZ
olur, boylece GitHub maili ve NOTIFY_WEBHOOK bildirimi tetiklenir.

Kontrol edilenler:
  IG_ACCESS_TOKEN
    - expires_at            : long-lived token 60 gun (refresh_token.py yeniler)
    - data_access_expires_at: 90 gun, YENILEME ILE UZAMAZ.
      Sadece Facebook giris ekranindan yeniden yetki verilince sifirlanir.
      Olculdu: fb_exchange_token bu sayaci ayni birakiyor.
  GOOGLE_OAUTH_REFRESH_TOKEN
    - Gercekten yenileme denenir. Consent screen "Testing" modundaysa
      token verilisinden 7 gun sonra oluyor; "In production" ise suresiz.
  GH_TOKEN
    - Fine-grained PAT'in son kullanma tarihi yanit basliginda gelir.
  PAYLASIM SAGLIGI (Drive'daki state.json)
    - Son paylasim STALE_POST_HOURS'tan (36) eskiyse: tetikleyiciler durmus
      (cron-job.org kapanmis, PAT'i dolmus) ya da her calisma hata veriyor.
    - Kuyrukta QUEUE_CRITICAL (4 = 2 gun) veya daha az video kaldiysa.

Calistirma:
  python check_credentials.py            # esik altindaysa exit 1
  python check_credentials.py --report   # sadece rapor, her zaman exit 0
"""

import json
import os
import sys
from datetime import datetime, timezone

import requests

GRAPH_VERSION = os.environ.get("GRAPH_VERSION", "").strip() or "v23.0"
GRAPH_HOST = f"https://graph.facebook.com/{GRAPH_VERSION}"

def _int_env(name, default):
    """Bos veya bozuk deger varsayilana duser - denetim cokmesin."""
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


# Bu esiklerin altina inince is basarisiz olur (bildirim tetiklenir)
CRITICAL_DAYS = _int_env("CRED_CRITICAL_DAYS", 10)
WARN_DAYS = _int_env("CRED_WARN_DAYS", 21)
# Gunde 2 paylasim: 36 saat = en az 2 paylasim kacirilmis
STALE_POST_HOURS = _int_env("STALE_POST_HOURS", 36)
QUEUE_CRITICAL = _int_env("QUEUE_CRITICAL", 4)
QUEUE_WARN = _int_env("QUEUE_WARN", 10)
STATE_FILENAME = "state.json"
META_KEY = "_meta"

NOW = datetime.now(timezone.utc)
sorunlar = []
uyarilar = []


def log(msg=""):
    print(msg, flush=True)


def gun_kaldi(ts):
    return (datetime.fromtimestamp(ts, timezone.utc) - NOW).days


def tarih(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def degerlendir(ad, gun, ne_yapmali):
    """Kalan gune gore satiri basar ve gerekiyorsa sorun kaydeder."""
    if gun <= 0:
        durum = "!!! SURESI DOLMUS"
        sorunlar.append(f"{ad}: suresi dolmus - {ne_yapmali}")
    elif gun <= CRITICAL_DAYS:
        durum = f"!!! {gun} GUN KALDI"
        sorunlar.append(f"{ad}: {gun} gun kaldi - {ne_yapmali}")
    elif gun <= WARN_DAYS:
        durum = f"!  {gun} gun"
        uyarilar.append(f"{ad}: {gun} gun kaldi - {ne_yapmali}")
    else:
        durum = f"OK ({gun} gun)"
    return durum


def env(name):
    return os.environ.get(name, "").strip()


def check_instagram():
    log("INSTAGRAM")
    token, app, sec = env("IG_ACCESS_TOKEN"), env("IG_APP_ID"), env("IG_APP_SECRET")
    if not token:
        sorunlar.append("IG_ACCESS_TOKEN tanimli degil")
        log("  IG_ACCESS_TOKEN tanimli degil")
        return
    if not (app and sec):
        log("  IG_APP_ID/SECRET yok - omur sorgulanamiyor (atlandi)")
        return

    try:
        d = requests.get(f"{GRAPH_HOST}/debug_token", timeout=30,
                         params={"input_token": token,
                                 "access_token": f"{app}|{sec}"}).json().get("data", {})
    except Exception as e:
        sorunlar.append(f"IG token sorgulanamadi: {type(e).__name__}")
        log(f"  sorgulanamadi: {e}")
        return

    if d.get("is_valid") is False:
        sorunlar.append("IG_ACCESS_TOKEN GECERSIZ - Graph API Explorer'dan yenisini uretin")
        log("  gecerli              : HAYIR")
        return
    log("  gecerli              : evet")

    exp = d.get("expires_at")
    if exp:
        log(f"  expires_at           : {degerlendir('IG token', gun_kaldi(exp), 'refresh_token.py calistirin')} ({tarih(exp)})")
    else:
        log("  expires_at           : suresiz")

    # Asil tehlike bu: yenileme ile UZAMAZ, elle yeniden yetki gerektirir
    dae = d.get("data_access_expires_at")
    if dae:
        durum = degerlendir(
            "IG veri erisimi", gun_kaldi(dae),
            "Facebook giris ekranindan YENIDEN YETKI verin (token yenileme bunu uzatmaz)")
        log(f"  data_access_expires  : {durum} ({tarih(dae)})")
        log("     ^ token yenileme bu sayaci UZATMAZ - elle yeniden yetki sart")


def check_google():
    """Refresh token'i dener. Calisiyorsa Drive access token'ini dondurur."""
    log("\nGOOGLE DRIVE")
    cid, csec, rt = (env("GOOGLE_OAUTH_CLIENT_ID"),
                     env("GOOGLE_OAUTH_CLIENT_SECRET"),
                     env("GOOGLE_OAUTH_REFRESH_TOKEN"))
    if not (cid and csec and rt):
        if env("GOOGLE_SERVICE_ACCOUNT_JSON"):
            log("  service account kullaniliyor (OAuth yok) - suresiz")
        else:
            sorunlar.append("Google kimlik bilgisi yok")
            log("  kimlik bilgisi yok")
        return None

    # Refresh token'in olup olmadigini anlamanin tek kesin yolu: kullanmak
    try:
        r = requests.post("https://oauth2.googleapis.com/token", timeout=30, data={
            "client_id": cid, "client_secret": csec,
            "refresh_token": rt, "grant_type": "refresh_token"})
    except Exception as e:
        sorunlar.append(f"Google token yenilenemedi: {type(e).__name__}")
        log(f"  yenileme denemesi    : ag hatasi {e}")
        return None

    if r.status_code == 200:
        log("  refresh token        : OK (yenileme calisiyor)")
        log("  NOT: consent screen 'Testing' modundaysa 7 gunde oluyor.")
        log("       console.cloud.google.com/auth/audience -> 'In production' olmali")
        try:
            return r.json().get("access_token")
        except ValueError:
            return None
    else:
        hata = ""
        try:
            hata = r.json().get("error", "")
        except ValueError:
            pass
        sorunlar.append(
            f"GOOGLE_OAUTH_REFRESH_TOKEN CALISMIYOR ({hata}) - "
            "setup_oauth.py ile yeniden yetkilendirin. "
            "Consent screen 'Testing' modunda kaldiysa token 7 gunde olur.")
        log(f"  refresh token        : !!! BOZUK - HTTP {r.status_code} {hata}")
        return None


def _parse_utc(raw):
    try:
        when = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def read_state(access_token, root):
    """Drive kokundeki state.json'u okur. Yoksa None."""
    headers = {"Authorization": f"Bearer {access_token}"}
    r = requests.get(
        "https://www.googleapis.com/drive/v3/files", headers=headers, timeout=30,
        params={
            "q": f"'{root}' in parents and name = '{STATE_FILENAME}' "
                 f"and trashed = false",
            "fields": "files(id, modifiedTime)",
            "orderBy": "modifiedTime desc",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        })
    r.raise_for_status()
    files = r.json().get("files", [])
    if not files:
        return None
    r = requests.get(
        f"https://www.googleapis.com/drive/v3/files/{files[0]['id']}",
        headers=headers, timeout=30,
        params={"alt": "media", "supportsAllDrives": "true"})
    r.raise_for_status()
    state = json.loads(r.content.decode("utf-8-sig") or "{}")
    return state if isinstance(state, dict) else {}


def check_posting(access_token):
    """Paylasim gercekten oluyor mu? Kimlik bilgileri saglam olsa da
    tetikleyiciler durabilir (cron-job.org isi kapatir, PAT'i dolar) ya da
    kuyruk biter - ikisi de eskiden sessizdi."""
    log("\nPAYLASIM SAGLIGI")
    root = env("DRIVE_ROOT_FOLDER_ID")
    if not root:
        log("  DRIVE_ROOT_FOLDER_ID yok - atlandi")
        return
    if not access_token:
        log("  Drive erisimi yok (yukaridaki Google sonucuna bakin) - atlandi")
        return

    try:
        state = read_state(access_token, root)
    except Exception as e:
        sorunlar.append(f"state.json okunamadi: {type(e).__name__}")
        log(f"  state.json okunamadi: {e}")
        return
    if state is None:
        uyarilar.append("state.json yok - henuz hic paylasim yapilmamis olabilir")
        log("  state.json           : yok")
        return

    son = None
    for key, entry in state.items():
        if key == META_KEY or not isinstance(entry, dict):
            continue
        when = _parse_utc(entry.get("published_at"))
        if when and (son is None or when > son):
            son = when
    if son is None:
        uyarilar.append("state.json'da hic paylasim kaydi yok")
        log("  son paylasim         : kayit yok")
    else:
        saat = (NOW - son).total_seconds() / 3600
        if saat > STALE_POST_HOURS:
            durum = f"!!! {saat:.0f} SAAT ONCE"
            sorunlar.append(
                f"Son paylasim {saat:.0f} saat once (esik {STALE_POST_HOURS}) - "
                "Actions sekmesinde son calismalara ve cron-job.org'a bakin")
        else:
            durum = f"OK ({saat:.0f} saat once)"
        log(f"  son paylasim         : {durum} ({son:%Y-%m-%d %H:%M} UTC)")

    meta = state.get(META_KEY) or {}
    kalan = meta.get("queue_remaining")
    if not isinstance(kalan, int) or isinstance(kalan, bool):
        log("  kuyruk               : henuz sayilmadi (post_reel.py bir kez "
            "pencere icinde calisinca dolar)")
        return
    ne_yapmali = "hafta klasorlerinin Reels/ klasorune video ekleyin"
    if kalan <= QUEUE_CRITICAL:
        durum = f"!!! {kalan} VIDEO"
        sorunlar.append(f"Kuyrukta {kalan} video kaldi (~{kalan / 2:g} gun) - {ne_yapmali}")
    elif kalan <= QUEUE_WARN:
        durum = f"!  {kalan} video"
        uyarilar.append(f"Kuyrukta {kalan} video kaldi (~{kalan / 2:g} gun) - {ne_yapmali}")
    else:
        durum = f"OK ({kalan} video, ~{kalan / 2:g} gun)"
    sayim = str(meta.get("queue_checked_at", "?"))[:16].replace("T", " ")
    log(f"  kuyruk               : {durum} (sayim: {sayim} UTC)")


def check_github():
    log("\nGITHUB PAT")
    tok = env("GH_TOKEN")
    if not tok:
        # Harici tetikleyicinin (cron-job.org) PAT'i ayri; bu sadece refresh_token.py
        log("  GH_TOKEN yok - IG token'i otomatik yenilenip secret'a yazilamaz")
        uyarilar.append("GH_TOKEN tanimli degil (opsiyonel ama onerilir)")
        return
    try:
        r = requests.get("https://api.github.com/user", timeout=30, headers={
            "Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"})
    except Exception as e:
        sorunlar.append(f"GH_TOKEN sorgulanamadi: {type(e).__name__}")
        log(f"  sorgulanamadi: {e}")
        return

    if r.status_code != 200:
        sorunlar.append(f"GH_TOKEN gecersiz (HTTP {r.status_code}) - yeni PAT uretin")
        log(f"  gecerli              : HAYIR (HTTP {r.status_code})")
        return
    log("  gecerli              : evet")

    exp = r.headers.get("github-authentication-token-expiration")
    if not exp:
        log("  son kullanma         : suresiz")
        return
    try:
        d = datetime.strptime(exp.split(" UTC")[0], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc)
    except ValueError:
        log(f"  son kullanma         : {exp} (cozumlenemedi)")
        return
    gun = (d - NOW).days
    durum = degerlendir("GH_TOKEN", gun,
                        "github.com/settings/personal-access-tokens adresinden yenileyin")
    log(f"  son kullanma         : {durum} ({d:%Y-%m-%d})")


def main():
    rapor_modu = "--report" in sys.argv
    log("=" * 60)
    log(f"Kimlik bilgisi denetimi - {NOW:%Y-%m-%d %H:%M} UTC")
    log(f"Esikler: kritik <= {CRITICAL_DAYS} gun, uyari <= {WARN_DAYS} gun")
    log("=" * 60)

    check_instagram()
    drive_token = check_google()
    check_github()
    check_posting(drive_token)

    log("\n" + "=" * 60)
    if sorunlar:
        log("KRITIK:")
        for s in sorunlar:
            log(f"  - {s}")
    if uyarilar:
        log("UYARI:")
        for u in uyarilar:
            log(f"  - {u}")
    if not sorunlar and not uyarilar:
        log("Her sey yolunda.")
    log("=" * 60)

    if sorunlar and not rapor_modu:
        # Basarisiz cikis -> GitHub maili + NOTIFY_WEBHOOK bildirimi
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
