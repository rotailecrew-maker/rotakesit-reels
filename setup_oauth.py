#!/usr/bin/env python3
"""
Bir kerelik OAuth kurulumu - Drive'a KENDI hesabiniz olarak erisim.

Neden gerekli:
  Service account kisisel My Drive'da islevsiz kaliyor. Olculdu:
    - dosya olusturma  -> storageQuotaExceeded (kotasi 0)
    - dosya tasima     -> cannotAddParent (canMoveItemOutOfDrive=False)
  Kendi hesabinizin refresh token'i ile bu sinirlarin hicbiri yok.

Kullanim:
  1. GCP Console > APIs & Services > OAuth consent screen
       - User type: External
       - App name: rotakesit-reels, destek e-postasi: kendi adresiniz
       - Scopes: eklemenize gerek yok, asagidaki scope zaten isteniyor
       - Test users: KENDI adresinizi ekleyin
       - ONEMLI: "Publishing status" -> PUBLISH APP (In production).
         "Testing" durumunda kalirsa refresh token 7 GUNDE oluyor.
         "Dogrulanmamis uygulama" uyarisi normaldir, kendi uygulamaniz.

  2. GCP Console > APIs & Services > Credentials
       - Create credentials > OAuth client ID
       - Application type: Desktop app
       - Olusunca JSON'u indirin (client_secret_....json)

  3. python setup_oauth.py "C:\\yol\\client_secret_....json"
       Tarayici acilir, hesabinizi secip izin verirsiniz.
       Refresh token .env dosyasina yazilir, ekrana BASILMAZ.

  4. GitHub secret'lari icin: python setup_oauth.py --github
"""

import io
import json
import os
import re
import sys

SCOPES = ["https://www.googleapis.com/auth/drive"]
ENV_PATH = ".env"

KEYS = ("GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        "GOOGLE_OAUTH_REFRESH_TOKEN")


def die(msg):
    sys.exit(f"HATA - {msg}")


def load_env():
    if not os.path.exists(ENV_PATH):
        return {}
    out = {}
    for line in io.open(ENV_PATH, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip("'\"")
    return out


def write_env(updates):
    """Var olan anahtari gunceller, yoksa sona ekler.

    re.sub kullanilmiyor: degerlerdeki ters bolu kacislari yorumlanir.
    """
    lines = io.open(ENV_PATH, encoding="utf-8").read().split("\n") \
        if os.path.exists(ENV_PATH) else []
    for key, value in updates.items():
        new = f"{key}={value}"
        for i, line in enumerate(lines):
            if line.strip().startswith(key + "="):
                lines[i] = new
                break
        else:
            lines.append(new)
    io.open(ENV_PATH, "w", encoding="utf-8", newline="\n").write(
        "\n".join(lines).rstrip("\n") + "\n")


def show_github_instructions():
    env = load_env()
    missing = [k for k in KEYS if not env.get(k)]
    if missing:
        die("once kurulumu tamamlayin, eksik: " + ", ".join(missing))
    print("GitHub > Settings > Secrets and variables > Actions altina")
    print("su uc secret'i ekleyin (degerleri .env dosyanizda):\n")
    for k in KEYS:
        v = env[k]
        print(f"  {k}")
        print(f"    {v[:6]}...{v[-4:]}  ({len(v)} karakter)")
    print("\nGOOGLE_SERVICE_ACCOUNT_JSON secret'ina artik gerek yok.")
    return 0


def main():
    if "--github" in sys.argv:
        return show_github_instructions()

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        die("google-auth-oauthlib kurulu degil:\n"
            "    pip install -r requirements.txt")

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        die("OAuth client JSON dosyasinin yolunu verin:\n"
            '    python setup_oauth.py "C:\\yol\\client_secret_....json"\n'
            "  (dosyayi GCP Console > Credentials > OAuth client ID > Desktop app"
            " ile olusturup indirin)")

    path = args[0]
    if not os.path.exists(path):
        die(f"dosya bulunamadi: {path}")

    data = json.load(io.open(path, encoding="utf-8"))
    section = data.get("installed") or data.get("web")
    if not section:
        die("bu bir OAuth client dosyasi degil. 'Desktop app' tipinde bir "
            "OAuth client ID olusturup indirdiginizden emin olun "
            "(service account JSON'u DEGIL).")
    if "web" in data:
        print("UYARI: 'Web application' tipi secilmis. 'Desktop app' onerilir; "
              "yonlendirme hatasi alirsaniz sebebi budur.\n")

    print("Tarayici aciliyor. Google hesabinizi secip izin verin.")
    print('"Google bu uygulamayi dogrulamadi" ekraninda:')
    print('  Gelismis > <uygulama adi> sayfasina git (guvensiz)\n')

    flow = InstalledAppFlow.from_client_config({"installed": section}, SCOPES)
    # access_type=offline + prompt=consent: refresh token'in DONMESINI garanti eder.
    # Bunlar olmadan ikinci yetkilendirmede refresh_token bos gelir.
    creds = flow.run_local_server(
        port=0, access_type="offline", prompt="consent",
        authorization_prompt_message="",
        success_message="Tamam. Bu sekmeyi kapatip terminale donebilirsiniz.",
    )

    if not creds.refresh_token:
        die("refresh token alinamadi. Google hesap ayarlarindan uygulamanin "
            "erisimini kaldirip tekrar deneyin.")

    write_env({
        "GOOGLE_OAUTH_CLIENT_ID": creds.client_id,
        "GOOGLE_OAUTH_CLIENT_SECRET": creds.client_secret,
        "GOOGLE_OAUTH_REFRESH_TOKEN": creds.refresh_token,
    })

    print("\nBASARILI - kimlik bilgileri .env dosyasina yazildi "
          "(icerikleri ekrana basilmadi).")

    # Gercekten calistigini dogrula
    try:
        from googleapiclient.discovery import build
        drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        me = drive.about().get(fields="user,storageQuota").execute()
        quota = me.get("storageQuota", {})
        limit = int(quota.get("limit", 0) or 0)
        print(f"Dogrulama : {me['user'].get('emailAddress')}")
        if limit:
            used = int(quota.get("usage", 0) or 0)
            print(f"Depolama  : {used / 2**30:.1f} / {limit / 2**30:.0f} GB")
    except Exception as e:
        print(f"UYARI: dogrulama cagrisi basarisiz ({type(e).__name__}: {e})")

    print("\nSirada: python setup_oauth.py --github")
    return 0


if __name__ == "__main__":
    sys.exit(main())
