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

Calistirma:
  python check_credentials.py            # esik altindaysa exit 1
  python check_credentials.py --report   # sadece rapor, her zaman exit 0
"""

import os
import sys
from datetime import datetime, timezone

import requests

GRAPH_VERSION = os.environ.get("GRAPH_VERSION", "").strip() or "v23.0"
GRAPH_HOST = f"https://graph.facebook.com/{GRAPH_VERSION}"

# Bu esiklerin altina inince is basarisiz olur (bildirim tetiklenir)
CRITICAL_DAYS = int(os.environ.get("CRED_CRITICAL_DAYS", "").strip() or 10)
WARN_DAYS = int(os.environ.get("CRED_WARN_DAYS", "").strip() or 21)

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
        return

    # Refresh token'in olup olmadigini anlamanin tek kesin yolu: kullanmak
    try:
        r = requests.post("https://oauth2.googleapis.com/token", timeout=30, data={
            "client_id": cid, "client_secret": csec,
            "refresh_token": rt, "grant_type": "refresh_token"})
    except Exception as e:
        sorunlar.append(f"Google token yenilenemedi: {type(e).__name__}")
        log(f"  yenileme denemesi    : ag hatasi {e}")
        return

    if r.status_code == 200:
        log("  refresh token        : OK (yenileme calisiyor)")
        log("  NOT: consent screen 'Testing' modundaysa 7 gunde oluyor.")
        log("       console.cloud.google.com/auth/audience -> 'In production' olmali")
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


def check_github():
    log("\nGITHUB PAT")
    tok = env("GH_TOKEN")
    if not tok:
        log("  GH_TOKEN yok - harici tetikleyici ve otomatik token yenileme devre disi")
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
    check_google()
    check_github()

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
