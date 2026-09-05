#!/usr/bin/env python3
"""
IG long-lived access token yenileyici.

Sorun: long-lived token 60 gunde oluyor. Yenilenmezse otomasyon sessizce durur
ve (hata siniflandirmasi olmasaydi) saglam videolari FAILED'a surerdi.

Bu script:
  1. Mevcut token'in omrunu sorar
  2. REFRESH_BEFORE_DAYS'ten az kaldiysa fb_exchange_token ile yenisini alir
  3. GH_TOKEN varsa yeni token'i GitHub secret'ina yazar (libsodium ile sifreli)
     yoksa sadece "elle guncelleyin" der - token'i ASLA log'a basmaz

Calistirma:
  python refresh_token.py            # gerekiyorsa yeniler
  python refresh_token.py --force    # omru ne olursa olsun yeniler
  python refresh_token.py --check    # sadece rapor verir, degistirmez

Gerekli:
  IG_APP_ID, IG_APP_SECRET, IG_ACCESS_TOKEN
Opsiyonel (secret'i otomatik guncellemek icin):
  GH_TOKEN      - repoda "Secrets: read and write" yetkili PAT
  GH_REPOSITORY - "kullanici/repo" (Actions icinde otomatik gelir)
"""

import base64
import io
import json
import os
import sys
from datetime import datetime, timezone

import requests

GRAPH_VERSION = os.environ.get("GRAPH_VERSION", "").strip() or "v23.0"
GRAPH_HOST = f"https://graph.facebook.com/{GRAPH_VERSION}"

APP_ID = os.environ.get("IG_APP_ID", "").strip()
APP_SECRET = os.environ.get("IG_APP_SECRET", "").strip()
TOKEN = os.environ.get("IG_ACCESS_TOKEN", "").strip()

GH_TOKEN = os.environ.get("GH_TOKEN", "").strip()
GH_REPOSITORY = os.environ.get("GH_REPOSITORY", "").strip()
SECRET_NAME = os.environ.get("TOKEN_SECRET_NAME", "IG_ACCESS_TOKEN").strip()

# Bos deger de varsayilana dusmeli - GitHub tanimsiz variable'i bos string yapar
REFRESH_BEFORE_DAYS = int(os.environ.get("REFRESH_BEFORE_DAYS", "").strip() or 15)


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def require(*names):
    missing = [n for n in names if not os.environ.get(n, "").strip()]
    if missing:
        sys.exit(f"HATA - eksik ortam degiskeni: {', '.join(missing)}")


def token_days_left():
    """Kalan gun sayisi. None = bilinmiyor, -1 = gecersiz."""
    r = requests.get(
        f"{GRAPH_HOST}/debug_token",
        params={"input_token": TOKEN, "access_token": f"{APP_ID}|{APP_SECRET}"},
        timeout=30,
    )
    body = r.json()
    if "error" in body:
        sys.exit(f"HATA - debug_token: {body['error'].get('message')}")

    data = body.get("data", {})
    if data.get("is_valid") is False:
        return -1
    expires_at = data.get("expires_at")
    if not expires_at:
        return None
    delta = datetime.fromtimestamp(expires_at, timezone.utc) - datetime.now(timezone.utc)
    return delta.days


def exchange():
    """Yeni long-lived token alir."""
    r = requests.get(
        f"{GRAPH_HOST}/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": APP_ID,
            "client_secret": APP_SECRET,
            "fb_exchange_token": TOKEN,
        },
        timeout=30,
    )
    body = r.json()
    if "access_token" not in body:
        sys.exit(f"HATA - token degistirilemedi: {json.dumps(body)[:300]}")
    return body["access_token"]


def update_github_secret(value):
    """GitHub Actions secret'ini gunceller. Token log'a basilmaz."""
    try:
        from nacl import encoding, public
    except ImportError:
        log("UYARI: PyNaCl kurulu degil, secret otomatik guncellenemiyor "
            "(pip install pynacl)")
        return False

    if not (GH_TOKEN and GH_REPOSITORY):
        return False

    headers = {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    base = f"https://api.github.com/repos/{GH_REPOSITORY}/actions/secrets"

    r = requests.get(f"{base}/public-key", headers=headers, timeout=30)
    if r.status_code != 200:
        log(f"UYARI: GitHub public key alinamadi (HTTP {r.status_code}). "
            f"PAT'in 'Secrets: read and write' yetkisi var mi?")
        return False
    key_info = r.json()

    sealed = public.SealedBox(
        public.PublicKey(key_info["key"].encode("utf-8"), encoding.Base64Encoder)
    ).encrypt(value.encode("utf-8"))

    r = requests.put(
        f"{base}/{SECRET_NAME}",
        headers=headers,
        json={
            "encrypted_value": base64.b64encode(sealed).decode("utf-8"),
            "key_id": key_info["key_id"],
        },
        timeout=30,
    )
    if r.status_code not in (201, 204):
        log(f"UYARI: secret guncellenemedi (HTTP {r.status_code}) {r.text[:200]}")
        return False

    log(f"GitHub secret '{SECRET_NAME}' guncellendi")
    return True


def sync_local_env(value):
    """Yerelde .env varsa onu da gunceller.

    Aksi halde GitHub secret yenilenirken yerel .env eski token'da kalir
    ve yerel testler CI'dan farkli davranir.
    """
    if not os.path.exists(".env"):
        return
    with io.open(".env", encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("IG_ACCESS_TOKEN="):
            lines[i] = "IG_ACCESS_TOKEN=" + value
            with io.open(".env", "w", encoding="utf-8",
                         newline="\n") as fh:
                fh.write("\n".join(lines) + "\n")
            log("Yerel .env de guncellendi")
            return


def main():
    require("IG_APP_ID", "IG_APP_SECRET", "IG_ACCESS_TOKEN")

    force = "--force" in sys.argv
    check_only = "--check" in sys.argv

    days = token_days_left()
    if days == -1:
        log("Token GECERSIZ - yenileme ise yaramaz, Graph API Explorer'dan "
            "yeni bir token uretmeniz gerekiyor (bkz README).")
        return 2
    if days is None:
        log("Token suresiz gorunuyor (expires_at yok)")
    else:
        log(f"Token omru: {days} gun")

    if check_only:
        return 0

    if not force and days is not None and days > REFRESH_BEFORE_DAYS:
        log(f"Yenileme gerekmiyor (esik {REFRESH_BEFORE_DAYS} gun)")
        return 0
    if not force and days is None:
        log("Suresiz token, yenileme atlandi (--force ile zorlayabilirsiniz)")
        return 0

    log("Yeni long-lived token aliniyor...")
    new_token = exchange()
    log(f"Yeni token alindi (uzunluk {len(new_token)}, icerigi loglanmaz)")

    sync_local_env(new_token)

    if update_github_secret(new_token):
        return 0

    log("!!! Yeni token GitHub'a YAZILAMADI. Su adimlari elle yapin:")
    log("    1. Graph API Explorer > uygulamaniz > token'i kopyalayin")
    log("    2. Repo > Settings > Secrets and variables > Actions")
    log(f"    3. {SECRET_NAME} secret'ini guncelleyin")
    log("    (Otomatiklestirmek icin GH_TOKEN secret'i ekleyin - bkz README)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
