"""Dashboard: pages build cleanly, page scripts parse, and the HTTP API behaves."""
import json, os, re, socket, subprocess, tempfile, unittest, urllib.error, urllib.request

from helpers import HAVE_NODE, LOGDIR
import common, liveview, talk
import local_server as ls
import webui


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class WebUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.srv = webui.start(liveview.LIVE_HUB, LOGDIR, common.MEDIA_REQ, common.MEDIA_STOP, "10.0.0.1",
                              cls.port, get_settings=ls.get_settings,
                              settings_file=ls.SETTINGS_FILE, talk=talk.TALK)
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        talk.TALK.set_live(False)

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()

    def post(self, path, body=b"{}", ctype="application/json", headers=None):
        req = urllib.request.Request(self.base + path, data=body,
                                     headers={"Content-Type": ctype, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    # --- pages -------------------------------------------------------------
    def test_pages_have_no_leftover_placeholders(self):
        for path in ("/", "/gallery"):
            status, ctype, body = self.get(path)
            self.assertEqual(status, 200)
            self.assertIn("text/html", ctype)
            self.assertEqual(re.findall(r"__[A-Z][A-Za-z0-9_]*__", body.decode()), [], path)

    @unittest.skipUnless(HAVE_NODE, "needs node")
    def test_page_scripts_parse(self):
        for path in ("/", "/gallery"):
            html = self.get(path)[2].decode()
            scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
            self.assertTrue(scripts, path)
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
                f.write("\n;\n".join(scripts))
            try:
                r = subprocess.run(["node", "--check", f.name], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, f"{path}: {r.stderr}")
            finally:
                os.unlink(f.name)

    def test_static_and_favicon(self):
        self.assertEqual(self.get("/favicon.svg")[0], 200)
        self.assertEqual(self.get("/static/mpegts.js")[0], 200)

    # --- API ---------------------------------------------------------------
    def test_state(self):
        st = json.loads(self.get("/api/state")[2])
        self.assertIn("now", st)
        self.assertIn("armed", st)

    def test_csrf_guard(self):
        self.assertEqual(self.post("/api/snapshot", ctype="text/plain")[0], 403)
        self.assertEqual(self.post("/api/snapshot", headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.post("/api/snapshot", headers={"Origin": "http://evil.example"})[0], 403)

    def test_snapshot_writes_media_request(self):
        self.assertEqual(self.post("/api/snapshot")[0], 200)
        with open(common.MEDIA_REQ) as f:
            self.assertEqual(json.load(f)["mode"], "snapshot")
        os.remove(common.MEDIA_REQ)

    def test_talk_endpoint(self):
        oct = "application/octet-stream"
        talk.TALK.set_live(False)
        self.assertEqual(self.post("/api/talk?sid=ab1&rate=48000", b"\0" * 960, oct)[0], 409)
        self.assertEqual(self.post("/api/talk?sid=../x&rate=48000", b"\0" * 960, oct)[0], 400)
        self.assertEqual(self.post("/api/talk?sid=ab1&rate=5", b"\0" * 960, oct)[0], 400)
        self.assertEqual(self.post("/api/talk?sid=ab1&format=adts", b"", oct)[0], 409)
        status, body = self.post("/api/talk?sid=ab1&end=1", b"", oct)
        self.assertEqual(status, 200)
        self.assertIn("rx_secs", json.loads(body))

    def test_clip_names_are_confined(self):
        for bad in ("/clips/..%2Fsettings.json", "/clips/.hidden.jpg", "/clips/x.txt"):
            with self.assertRaises(urllib.error.HTTPError):
                self.get(bad)


if __name__ == "__main__":
    unittest.main()
