"""Protocol helpers: settings bytes, Kt commands, timestamps, upload de-chunking."""
import os, struct, unittest

from helpers import TOOLS
import blink_proto as bp
import media_watch
import local_server as ls

TPL = os.path.join(TOOLS, "templates")


def tpl(name):
    with open(os.path.join(TPL, name), "rb") as f:
        return f.read()


class SettingsTest(unittest.TestCase):
    def test_round_trip_heartbeat_and_config(self):
        for name in ("heartbeat_0807.bin", "config_0807.bin"):
            reply = tpl(name)
            cur = bp.parse_settings(reply)
            self.assertIsNotNone(cur, name)
            want = {"nv": 1, "ir": 4, "quality": 2, "flip": 1, "audio_stream": 0,
                    "clip_len": 12, "sensitivity": 50, "retrigger": 20, "armed": 1, "motion": 1}
            patched = bp.patch_settings(reply, **want)
            self.assertEqual(len(patched), len(reply))
            got = bp.parse_settings(patched)
            for k, v in want.items():
                self.assertEqual(got[k], v, f"{name}: {k}")

    def test_patch_changes_only_setting_bytes(self):
        reply = tpl("heartbeat_0807.bin")
        patched = bp.patch_settings(reply, nv=0)
        diff = [i for i, (a, b) in enumerate(zip(reply, patched)) if a != b]
        self.assertTrue(set(diff) <= {bp.SETTINGS_OFFSETS[210]["nv"]})

    def test_unknown_length_is_left_alone(self):
        self.assertEqual(bp.patch_settings(b"\x00" * 50, nv=1), b"\x00" * 50)
        self.assertIsNone(bp.parse_settings(b"\x00" * 50))


class CommandTest(unittest.TestCase):
    def test_liveview_command_seq_and_media_ip(self):
        cmd = bp.set_media_ip(bp.set_kt_seq(tpl("start_liveview_0806.bin"), 0x1234), "10.10.10.1")
        blocks = list(bp._kt_blocks(cmd))
        self.assertTrue(blocks)
        for _, seq, field in blocks:
            self.assertEqual(seq, 0x1234)
            self.assertEqual(field, b"\x04\x0a\x0a\x0a\x01")

    def test_kt_echo_carries_the_poll_seq(self):
        self.assertEqual(bp._kt_seq(ls.kt_echo(0x0abc)), 0x0abc)

    def test_ts_patch_writes_utc_now(self):
        hb = ls.ts_patch(tpl("heartbeat_0807.bin"))
        year, = struct.unpack(">H", hb[28:30])
        self.assertGreaterEqual(year, 2026)
        self.assertTrue(1 <= hb[30] <= 12 and 1 <= hb[31] <= 31)


class UploadTest(unittest.TestCase):
    def test_split_streams_demuxes_jpeg_and_ts(self):
        jpeg = b"\xff\xd8\xff\xe0" + b"J" * 100 + b"\xff\xd9"
        ts = b"\x47" + b"\x00" * 187
        def chunk(t, ctr, payload):
            return bytes([t]) + struct.pack(">II", ctr, len(payload)) + payload
        data = b"HDR" + chunk(0x04, 1, jpeg[:50]) + chunk(0x00, 2, ts) + chunk(0x04, 3, jpeg[50:])
        streams = media_watch.split_streams(data)
        self.assertEqual(streams[0x04], jpeg)
        self.assertEqual(streams[0x00], ts)
        self.assertEqual(media_watch._carve_jpeg(streams[0x04]), jpeg)


class RestTest(unittest.TestCase):
    def test_header_order_mirrors_the_cloud(self):
        resp = ls.http_response(body="{}", account_id="1", x_frame_options=True,
                                x_xss_protection=True).decode()
        names = [l.split(":")[0] for l in resp.split("\r\n\r\n")[0].split("\r\n")[1:]]
        self.assertEqual(names, ["Date", "Content-Type", "Content-Length", "Connection",
                                 "Account-Id", "Client-Verified", "Server", "Status",
                                 "X-Blink-Trace-Id", "X-Content-Type-Options",
                                 "X-Frame-Options", "X-Powered-By", "X-Xss-Protection"])
        self.assertTrue(resp.endswith("\r\n\r\n{}"))


if __name__ == "__main__":
    unittest.main()
