# Gizlilik Politikası — rotakesit-reels

**Son güncelleme:** 4 Eylül 2026

## Bu nedir?

`rotakesit-reels`, tek bir kişinin kendi Instagram hesabı için kullandığı kişisel
bir otomasyon aracıdır. Google Drive'daki kendi video dosyalarını alıp kendi
Instagram hesabında Reels olarak paylaşır. Başka kullanıcılara sunulan bir hizmet
değildir; kayıt, üyelik veya kullanıcı hesabı yoktur.

## Hangi verilere erişiyor?

Uygulama yalnızca **uygulamayı çalıştıran kişinin kendi hesaplarına** erişir:

- **Google Drive** (`https://www.googleapis.com/auth/drive`) — yalnızca kullanıcının
  belirttiği tek bir kök klasör ve altındaki dosyalar: video dosyalarını okur ve
  indirir, caption metinlerini okur, yayınlanan videoyu `published/` klasörüne
  taşır, işlem geçmişini `state.json` dosyasında tutar.
- **Instagram Graph API** — kullanıcının kendi Instagram Business hesabına Reels
  yükler ve yayınlar.

## Verilerle ne yapılıyor?

- Video dosyaları, yayınlama işlemi süresince geçici olarak indirilir ve işlem
  biter bitmez **silinir**.
- Hiçbir veri üçüncü taraflara aktarılmaz, satılmaz veya paylaşılmaz.
- Hiçbir veri, kullanıcının kendi Google Drive'ı ve kendi Instagram hesabı dışında
  bir yerde saklanmaz.
- Reklam, profilleme veya analitik amacıyla hiçbir kullanım yoktur.

## Kimlik bilgileri nerede tutuluyor?

Google OAuth refresh token'ı ve Instagram erişim anahtarı, kullanıcının kendi
GitHub deposundaki **şifrelenmiş GitHub Actions secret'larında** tutulur. Kaynak
kodda veya depoda düz metin olarak hiçbir kimlik bilgisi bulunmaz.

## Erişimi nasıl kaldırırım?

[myaccount.google.com/permissions](https://myaccount.google.com/permissions)
adresinden uygulamanın erişimini istediğiniz zaman kaldırabilirsiniz. Erişim
kaldırıldığında otomasyon durur; Drive'daki dosyalarınıza hiçbir şey olmaz.

## Veri saklama ve silme

Uygulama kalıcı bir veri deposu tutmaz. Tek kalıcı kayıt, kullanıcının kendi
Drive'ındaki `state.json` dosyasıdır ve hangi videonun ne zaman yayınlandığını
tutar. Kullanıcı bu dosyayı istediği zaman silebilir.

## İletişim

kral7161@gmail.com
