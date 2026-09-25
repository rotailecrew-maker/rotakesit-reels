"""post_reel.py testleri - gercek Drive/IG'ye hic dokunmaz.

Calistirma:  python -m unittest discover -s tests -v
"""

import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

# post_reel import aninda ortam degiskenlerini dogruluyor
os.environ.update({
    "IG_USER_ID": "123",
    "IG_ACCESS_TOKEN": "test-token-123456789",
    "DRIVE_ROOT_FOLDER_ID": "ROOT",
    "GOOGLE_OAUTH_CLIENT_ID": "cid",
    "GOOGLE_OAUTH_CLIENT_SECRET": "csec-123456789",
    "GOOGLE_OAUTH_REFRESH_TOKEN": "rt-123456789",
})
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import post_reel as p  # noqa: E402

FOLDER = p.FOLDER_MIME
VIDEO = "video/mp4"


# --------------------------------------------------------------------------
# Sahte Drive
# --------------------------------------------------------------------------

class _Req:
    def __init__(self, fn):
        self.fn = fn

    def execute(self, num_retries=0):
        return self.fn()


class FakeFiles:
    def __init__(self, drive):
        self.d = drive

    def list(self, q, **_):
        folder_id = q.split("'")[1]
        return _Req(lambda: {"files": [dict(f) for f in self.d.children(folder_id)]})

    def get_media(self, fileId, **_):
        return _Req(lambda: self.d.content[fileId])

    def update(self, fileId, addParents=None, removeParents=None,
               media_body=None, **_):
        def run():
            if media_body is not None:
                self.d.count_state_write()
                self.d.content[fileId] = media_body.getbytes(0, media_body.size())
                self.d.meta[fileId]["modifiedTime"] = self.d.tick()
            if addParents:
                if self.d.fail_moves:
                    raise RuntimeError("Drive HTTP 403 (sahte)")
                self.d.meta[fileId]["parents"] = [addParents]
            return {"id": fileId}
        return _Req(run)

    def create(self, body, media_body=None, **_):
        def run():
            self.d.count_state_write()
            fid = self.d.add(body["name"], "application/json", body["parents"][0])
            self.d.content[fid] = media_body.getbytes(0, media_body.size())
            return {"id": fid}
        return _Req(run)


class FakeDrive:
    def __init__(self):
        self.meta, self.content, self._n, self._t = {}, {}, 0, 0
        self.state_write_attempts = 0
        self.fail_state_writes_after = None
        self.fail_moves = False

    def count_state_write(self):
        """fail_state_writes_after=N: ilk N yazma basarili, sonrakiler patlar."""
        self.state_write_attempts += 1
        if self.fail_state_writes_after is not None and \
                self.state_write_attempts > self.fail_state_writes_after:
            raise RuntimeError("Drive HTTP 500 (sahte)")

    def tick(self):
        self._t += 1
        return f"2026-01-01T00:00:{self._t:02d}Z"

    def add(self, name, mime, parent, content=b"", fid=None):
        self._n += 1
        fid = fid or f"id{self._n}"
        self.meta[fid] = {"id": fid, "name": name, "mimeType": mime,
                           "parents": [parent], "size": str(len(content)),
                           "modifiedTime": self.tick()}
        self.content[fid] = content
        return fid

    def children(self, folder_id):
        return [f for f in self.meta.values() if folder_id in f["parents"]]

    def files(self):
        """googleapiclient arayuzu: drive.files().list(...) vb."""
        return FakeFiles(self)

    def state(self):
        ids = [f["id"] for f in self.children("ROOT") if f["name"] == "state.json"]
        if not ids:
            return {}
        return json.loads(self.content[ids[0]].decode("utf-8"))


def make_drive():
    d = FakeDrive()
    d.add("published", FOLDER, "ROOT", fid="PUB")
    d.add("failed", FOLDER, "ROOT", fid="FAIL")
    d.add("1. Hafta", FOLDER, "ROOT", fid="W1")
    d.add("Reels", FOLDER, "W1", fid="R1")
    d.add("Capitons", FOLDER, "W1", fid="C1")
    d.add("a.mp4", VIDEO, "R1", b"AAAA", fid="vA")
    d.add("b.mp4", VIDEO, "R1", b"BBBB", fid="vB")
    d.add("a.txt", "text/plain", "C1", "A caption #rotakesit".encode(), fid="tA")
    d.add("2. Hafta", FOLDER, "ROOT", fid="W2")
    d.add("Reels", FOLDER, "W2", fid="R2")
    d.add("c.mp4", VIDEO, "R2", b"CCCC", fid="vC")
    # Hafta olmayan klasor - videosu dogrudan icinde; paylasilMAMALI
    d.add("Arşiv", FOLDER, "ROOT", fid="ARS")
    d.add("x.mp4", VIDEO, "ARS", b"XXXX", fid="vX")
    return d


def fake_download(drive, meta, dest):
    data = drive.content[meta["id"]]
    with open(dest, "wb") as fh:
        fh.write(data)
    return len(data)


# --------------------------------------------------------------------------
# Ana akis testleri
# --------------------------------------------------------------------------

class MainFlowBase(unittest.TestCase):
    def setUp(self):
        self.drive = make_drive()
        self.logs = []
        self.captions = []   # create_container'a giden caption'lar
        self.published = []  # publish edilen container'lar
        self._n = 0

        def create_container(caption):
            self._n += 1
            self.captions.append(caption)
            return f"CONT{self._n}"

        def publish(cid):
            self.published.append(cid)
            return f"MEDIA-{cid}"

        patches = [
            mock.patch.object(p, "log", lambda m: self.logs.append(str(m))),
            mock.patch.object(p, "drive_client", lambda: self.drive),
            mock.patch.object(p, "download_file", fake_download),
            mock.patch.object(p, "check_token_expiry", lambda: None),
            mock.patch.object(p, "create_container", create_container),
            mock.patch.object(p, "upload_video", lambda cid, path: None),
            mock.patch.object(p, "wait_until_finished", lambda cid: None),
            mock.patch.object(p, "publish", publish),
            mock.patch.object(p, "container_status",
                              lambda cid: ("FINISHED", None)),
            mock.patch.object(p, "find_recent_media", lambda *a, **k: None),
            mock.patch.object(p.time, "sleep", lambda s: None),
            mock.patch.object(p, "ENFORCE_WINDOW", False),
            mock.patch.object(p, "DRY_RUN", False),
            mock.patch.object(p, "MIN_INTERVAL_HOURS", 0),
        ]
        for pt in patches:
            pt.start()
            self.addCleanup(pt.stop)

    def state(self):
        return self.drive.state()

    def parent(self, fid):
        return self.drive.meta[fid]["parents"][0]


class TestMainFlow(MainFlowBase):
    def test_basarili_paylasim(self):
        self.assertEqual(p.main(), 0)
        st = self.state()
        self.assertTrue(st["vA"]["published"])
        self.assertEqual(st["vA"]["media_id"], "MEDIA-CONT1")
        self.assertEqual(self.parent("vA"), "PUB")
        for k in p.PENDING_KEYS:
            self.assertNotIn(k, st["vA"])
        # b.mp4 ve c.mp4 kaldi; Arsiv'deki x.mp4 sayilmaz
        self.assertEqual(st["_meta"]["queue_remaining"], 2)
        self.assertEqual(self.captions, ["A caption #rotakesit"])

    def test_arsiv_klasoru_kuyruga_girmez(self):
        for _ in range(3):
            self.assertEqual(p.main(), 0)
        self.assertNotIn("vX", self.state())
        self.assertEqual(self.parent("vX"), "ARS")
        # Kuyruk bitti: ilk bos calisma bildirim icin basarisiz
        self.assertEqual(p.main(), 1)

    def test_publish_yaniti_kayboldu_ama_yayinlandi(self):
        def publish(cid):
            raise p.TransientError("Ag hatasi (ReadTimeout)")
        with mock.patch.object(p, "publish", publish), \
                mock.patch.object(p, "container_status",
                                  lambda cid: ("PUBLISHED", None)):
            self.assertEqual(p.main(), 0)
        st = self.state()
        self.assertTrue(st["vA"]["published"])
        self.assertEqual(self.parent("vA"), "PUB")

    def test_publish_reddedildi_yayinlanmadi(self):
        def publish(cid):
            raise p.FileError("Yayinlanamadi: HTTP 400 [code=352]")
        with mock.patch.object(p, "publish", publish):
            self.assertEqual(p.main(), 1)
        st = self.state()
        self.assertFalse(st["vA"].get("published"))
        self.assertEqual(st["vA"]["retries"], 1)
        for k in p.PENDING_KEYS:
            self.assertNotIn(k, st["vA"])

    def test_belirsiz_publish_sonraki_calismada_cozulur(self):
        def publish(cid):
            raise p.TransientError("Ag hatasi (ReadTimeout)")

        def status_down(cid):
            raise p.TransientError("Ag hatasi (ConnectionError)")

        with mock.patch.object(p, "publish", publish), \
                mock.patch.object(p, "container_status", status_down):
            self.assertEqual(p.main(), 1)
        st = self.state()
        self.assertEqual(st["vA"]["pending_container"], "CONT1")
        self.assertEqual(st["vA"].get("retries", 0), 0)
        pending_at = st["vA"]["pending_at"]

        # Sonraki calisma: IG'ye ulasildi, container yayinlanmis
        with mock.patch.object(p, "container_status",
                               lambda cid: ("PUBLISHED", None)):
            self.assertEqual(p.main(), 0)
        st = self.state()
        self.assertTrue(st["vA"]["published"])
        self.assertEqual(st["vA"]["published_at"], pending_at)
        self.assertEqual(self.parent("vA"), "PUB")
        # a.mp4 IKINCI KEZ paylasilmadi, sira b.mp4'e gecti
        self.assertEqual(self.captions.count("A caption #rotakesit"), 1)
        self.assertTrue(st["vB"]["published"])

    def test_bekleyen_dogrulanamazsa_paylasim_yapilmaz(self):
        self.drive.fail_state_writes_after = 1  # sadece "bekleyen" kaydi yazilir
        self.assertEqual(p.main(), 1)
        self.drive.fail_state_writes_after = None

        def status_down(cid):
            raise p.TransientError("[code=190] token")
        with mock.patch.object(p, "container_status", status_down):
            self.assertEqual(p.main(), 1)
        self.assertEqual(len(self.captions), 1)  # ikinci container acilmadi

    def test_state_yazilamasa_da_tasinir_ve_tekrar_paylasilmaz(self):
        # 1. yazma (bekleyen isaret) basarili, paylasim sonrasi yazma patlar
        self.drive.fail_state_writes_after = 1
        self.assertEqual(p.main(), 1)
        self.assertEqual(self.published, ["CONT1"])
        self.assertEqual(self.parent("vA"), "PUB")      # yine de tasindi
        self.assertEqual(self.state()["vA"]["pending_container"], "CONT1")

        self.drive.fail_state_writes_after = None
        with mock.patch.object(p, "container_status",
                               lambda cid: ("PUBLISHED", None)):
            self.assertEqual(p.main(), 0)
        st = self.state()
        self.assertTrue(st["vA"]["published"])
        self.assertEqual(self.captions.count("A caption #rotakesit"), 1)

    def test_tasima_patlarsa_sonraki_calisma_tasir(self):
        self.drive.fail_moves = True
        self.assertEqual(p.main(), 0)
        self.assertEqual(self.parent("vA"), "R1")
        self.drive.fail_moves = False
        self.assertEqual(p.main(), 0)
        self.assertEqual(self.parent("vA"), "PUB")
        self.assertEqual(self.captions.count("A caption #rotakesit"), 1)

    def test_videoya_bagli_gecici_hata_ertelenir(self):
        def upload(cid, path):
            raise p.FileTransientError("Yukleme 3 denemede tamamlanamadi")
        with mock.patch.object(p, "upload_video", upload):
            for i in range(1, p.MAX_TRANSIENT_RETRIES):
                self.assertEqual(p.main(), 1)
                self.assertEqual(self.state()["vA"]["transient_retries"], i)
            self.assertEqual(p.main(), 1)
        st = self.state()
        self.assertIn("deferred_until", st["vA"])
        self.assertEqual(st["vA"].get("retries", 0), 0)   # hak yanmadi
        self.assertEqual(self.parent("vA"), "R1")          # failed/'a gitmedi

        # Sonraki calisma siradaki videoya gecer
        self.assertEqual(p.main(), 0)
        self.assertTrue(self.state()["vB"]["published"])

    def test_erteleme_bitince_sirasina_doner(self):
        self.test_videoya_bagli_gecici_hata_ertelenir()
        later = datetime.now(timezone.utc) + timedelta(hours=p.DEFER_HOURS + 1)

        class FakeDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return later if tz else later.replace(tzinfo=None)
        with mock.patch.object(p, "datetime", FakeDT):
            self.assertEqual(p.main(), 0)
        self.assertTrue(self.state()["vA"]["published"])

    def test_genel_gecici_hata_sayac_yakmaz(self):
        def create(caption):
            raise p.TransientError("Container olusturulamadi: [code=190]")
        with mock.patch.object(p, "create_container", create):
            for _ in range(p.MAX_TRANSIENT_RETRIES + 2):
                self.assertEqual(p.main(), 1)
        st = self.state()
        self.assertEqual(st["vA"].get("transient_retries", 0), 0)
        self.assertNotIn("deferred_until", st["vA"])

    def test_bos_kuyruk_gunde_bir_kez_basarisiz(self):
        for fid in ("vA", "vB", "vC"):
            self.drive.meta[fid]["parents"] = ["PUB"]
        self.assertEqual(p.main(), 1)
        self.assertEqual(p.main(), 0)
        self.assertEqual(self.state()["_meta"]["queue_remaining"], 0)

    def test_min_interval_ikinci_paylasimi_engeller(self):
        with mock.patch.object(p, "MIN_INTERVAL_HOURS", 2):
            self.assertEqual(p.main(), 0)
            self.assertEqual(p.main(), 0)
        self.assertEqual(len(self.published), 1)

    def test_birden_fazla_state_json_en_yenisi(self):
        eski = self.drive.add("state.json", "application/json", "ROOT",
                              json.dumps({"vA": {"published": True}}).encode())
        yeni = self.drive.add("state.json", "application/json", "ROOT",
                              json.dumps({}).encode())
        self.drive.meta[eski]["modifiedTime"] = "2026-01-01T00:00:00Z"
        self.drive.meta[yeni]["modifiedTime"] = "2026-02-01T00:00:00Z"
        entries = p.list_folder(self.drive, "ROOT")
        fid, state, ok = p.load_state(self.drive, entries)
        self.assertTrue(ok)
        self.assertEqual(fid, yeni)

    def test_dry_run_hicbir_sey_yazmaz(self):
        with mock.patch.object(p, "DRY_RUN", True):
            self.assertEqual(p.main(), 0)
        self.assertEqual(self.captions, [])
        self.assertEqual(self.state(), {})
        self.assertEqual(self.parent("vA"), "R1")

    def test_gecersiz_pencere_basarisiz(self):
        with mock.patch.object(p, "ENFORCE_WINDOW", True), \
                mock.patch.object(p, "POST_WINDOWS", "22-2"):
            self.assertEqual(p.main(), 1)
        self.assertEqual(self.captions, [])


# --------------------------------------------------------------------------
# Birim testleri
# --------------------------------------------------------------------------

class TestCaption(unittest.TestCase):
    def setUp(self):
        pt = mock.patch.object(p, "log", lambda m: None)
        pt.start()
        self.addCleanup(pt.stop)

    def test_kirpma_2200_asmaz(self):
        self.assertLessEqual(p.ig_len(p.normalize_caption("a" * 3000)), 2200)

    def test_emoji_iki_birim_sayilir(self):
        out = p.normalize_caption("😀" * 1500)
        self.assertLessEqual(p.ig_len(out), 2200)
        self.assertTrue(out.endswith("…"))

    def test_kisa_caption_degismez(self):
        self.assertEqual(p.normalize_caption("Merhaba #rota"), "Merhaba #rota")

    def test_fazla_hashtag_baska_etiketi_bozmaz(self):
        tags = " ".join(f"#t{i}" for i in range(29))
        out = p.normalize_caption("#rotakesit video " + tags + " #rota")
        self.assertIn("#rotakesit", out)
        self.assertFalse(out.endswith("#rota"))
        self.assertEqual(len(p.re.findall(r"#\w+", out)), 30)

    def test_crlf_normalize(self):
        self.assertEqual(p.normalize_caption("a\r\nb\rc"), "a\nb\nc")

    def test_caption_head(self):
        self.assertEqual(p.normalize_caption_head(" a\r\nb "), "a\nb")


class TestWindows(unittest.TestCase):
    def setUp(self):
        pt = mock.patch.object(p, "log", lambda m: None)
        pt.start()
        self.addCleanup(pt.stop)

    def test_parse(self):
        self.assertEqual(p.parse_windows("9-14,17-22"), [(9, 14), (17, 22)])
        self.assertEqual(p.parse_windows("8"), [(8, 8)])

    def test_gecersizler_atlanir(self):
        self.assertEqual(p.parse_windows("22-2,25-3,x,9-10"), [(9, 10)])

    def test_current_window_sinirlar(self):
        w = [(9, 14), (17, 22)]
        at = lambda h, m=0: datetime(2026, 1, 1, h, m)  # noqa: E731
        self.assertIsNone(p.current_window(at(8, 59), w))
        self.assertEqual(p.current_window(at(9), w), (9, 14))
        self.assertEqual(p.current_window(at(14, 59), w), (9, 14))
        self.assertIsNone(p.current_window(at(15), w))
        self.assertEqual(p.current_window(at(22, 30), w), (17, 22))


class TestHelpers(unittest.TestCase):
    def test_temp_path_guvenli(self):
        for name in ("Bolum 1/2.mp4", "/etc/passwd.mov", "../../x.MP4", "ad.mkv"):
            path = p.temp_video_path({"name": name})
            try:
                self.assertEqual(os.path.dirname(path), tempfile.gettempdir())
                self.assertTrue(os.path.exists(path))
                self.assertIn(os.path.splitext(path)[1], p.VIDEO_EXTS)
            finally:
                os.unlink(path)

    def test_week_re(self):
        for ad in ("1. Hafta", "10.Hafta", "Hafta 3", "3"):
            self.assertTrue(p.WEEK_RE.search(ad), ad)
        for ad in ("Arşiv", "Taslak", "Ham Çekim 2", "Arsiv 2024"):
            self.assertFalse(p.WEEK_RE.search(ad), ad)

    def test_natural_sort(self):
        adlar = ["10. Hafta", "2. Hafta", "1. Hafta"]
        self.assertEqual(sorted(adlar, key=p.natural_key),
                         ["1. Hafta", "2. Hafta", "10. Hafta"])

    def test_utf16_caption(self):
        d = make_drive()
        d.content["tA"] = "Türkçe açıklama".encode("utf-16")
        self.assertEqual(p.read_text_file(d, "tA"), "Türkçe açıklama")

    def test_cp1254_caption(self):
        d = make_drive()
        d.content["tA"] = "Türkçe ğüşiöç".encode("cp1254")
        self.assertEqual(p.read_text_file(d, "tA"), "Türkçe ğüşiöç")

    def test_kisayol_uygun_degil(self):
        v = {"id": "s", "name": "a.mp4",
             "mimeType": "application/vnd.google-apps.shortcut"}
        self.assertFalse(p.eligible(v, {}))


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        if isinstance(self._body, str):
            raise ValueError
        return self._body


class TestClassification(unittest.TestCase):
    def cls(self, status, body):
        return type(p.graph_failure(FakeResp(status, body), "t"))

    def test_token_genel_gecici(self):
        self.assertIs(self.cls(400, {"error": {"code": 190}}), p.TransientError)

    def test_paylasim_limiti_genel_gecici(self):
        self.assertIs(self.cls(400, {"error": {"code": 9, "error_subcode": 2207042}}),
                      p.TransientError)

    def test_sunucu_hatasi_genel_gecici(self):
        self.assertIs(self.cls(503, "<html>"), p.TransientError)

    def test_code_100_videoya_bagli(self):
        self.assertIs(self.cls(400, {"error": {"code": 100}}), p.FileTransientError)

    def test_json_olmayan_4xx_hak_yakmaz(self):
        self.assertIs(self.cls(400, "<html>proxy</html>"), p.FileTransientError)

    def test_spec_reddi_dosya_hatasi(self):
        self.assertIs(self.cls(400, {"error": {"code": 352}}), p.FileError)


class TestInstagramPolling(unittest.TestCase):
    """wait_until_finished / recover_publish - http() sahte yanitlarla."""

    def setUp(self):
        for pt in (mock.patch.object(p, "log", lambda m: None),
                   mock.patch.object(p.time, "sleep", lambda s: None)):
            pt.start()
            self.addCleanup(pt.stop)

    def with_responses(self, *items):
        seq = list(items)

        def fake_http(method, url, **kw):
            item = seq.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return mock.patch.object(p, "http", fake_http)

    def test_ag_kopmasi_ve_5xx_sonrasi_finished(self):
        with self.with_responses(p.TransientError("Ag hatasi"),
                                 FakeResp(502, "<html>"),
                                 FakeResp(200, {"status_code": "IN_PROGRESS"}),
                                 FakeResp(200, {"status_code": "FINISHED"})):
            p.wait_until_finished("C")

    def test_token_hatasi_hemen_duser(self):
        with self.with_responses(FakeResp(400, {"error": {"code": 190}})):
            with self.assertRaises(p.TransientError) as cm:
                p.wait_until_finished("C")
        self.assertNotIsInstance(cm.exception, p.FileTransientError)

    def test_error_dosya_hatasi(self):
        with self.with_responses(FakeResp(200, {"status_code": "ERROR"})):
            with self.assertRaises(p.FileError):
                p.wait_until_finished("C")

    def test_expired_videoya_bagli_gecici(self):
        with self.with_responses(FakeResp(200, {"status_code": "EXPIRED"})):
            with self.assertRaises(p.FileTransientError):
                p.wait_until_finished("C")

    def test_recover_published(self):
        with self.with_responses(FakeResp(200, {"status_code": "PUBLISHED"})), \
                mock.patch.object(p, "find_recent_media", lambda *a: "M1"):
            self.assertEqual(p.recover_publish("C", "cap", datetime.now()),
                             ("published", "M1"))

    def test_recover_unpublished(self):
        with self.with_responses(FakeResp(200, {"status_code": "FINISHED"})):
            self.assertEqual(p.recover_publish("C", "cap", datetime.now())[0],
                             "unpublished")

    def test_recover_unknown(self):
        err = p.TransientError("Ag hatasi")
        with self.with_responses(err, err, err), \
                mock.patch.object(p, "find_recent_media", lambda *a: None):
            self.assertEqual(p.recover_publish("C", "cap", datetime.now())[0],
                             "unknown")


# --------------------------------------------------------------------------
# check_credentials / refresh_token
# --------------------------------------------------------------------------

class TestCheckPosting(unittest.TestCase):
    def setUp(self):
        import check_credentials as cc
        self.cc = cc
        cc.sorunlar.clear()
        cc.uyarilar.clear()
        for pt in (mock.patch.object(cc, "log", lambda m="": None),
                   mock.patch.dict(os.environ, {"DRIVE_ROOT_FOLDER_ID": "ROOT"})):
            pt.start()
            self.addCleanup(pt.stop)

    def run_with(self, state):
        with mock.patch.object(self.cc, "read_state", lambda tok, root: state):
            self.cc.check_posting("tok")

    def test_eski_paylasim_ve_az_kuyruk_alarm(self):
        eski = (self.cc.NOW - timedelta(hours=50)).isoformat()
        self.run_with({"v": {"published_at": eski}, "_meta": {"queue_remaining": 3}})
        self.assertEqual(len(self.cc.sorunlar), 2)

    def test_saglikli(self):
        yeni = (self.cc.NOW - timedelta(hours=5)).isoformat()
        self.run_with({"v": {"published_at": yeni}, "_meta": {"queue_remaining": 30}})
        self.assertEqual(self.cc.sorunlar, [])
        self.assertEqual(self.cc.uyarilar, [])


class TestRefreshToken(unittest.TestCase):
    def test_env_yoksa_olusturur_varsa_gunceller(self):
        os.environ.setdefault("IG_APP_ID", "x")
        import refresh_token as rt
        old = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"GITHUB_ACTIONS": ""}), \
                mock.patch.object(rt, "log", lambda m: None):
            os.chdir(tmp)
            try:
                self.assertTrue(rt.sync_local_env("YENI1"))
                self.assertTrue(rt.sync_local_env("YENI2"))
                with io.open(".env", encoding="utf-8") as fh:
                    self.assertEqual(fh.read(), "IG_ACCESS_TOKEN=YENI2\n")
            finally:
                os.chdir(old)

    def test_ci_icinde_yazmaz(self):
        import refresh_token as rt
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            self.assertFalse(rt.sync_local_env("X"))


if __name__ == "__main__":
    unittest.main()
