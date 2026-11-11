"""Live-view session control on the 0x0804 socket (no talk)."""
import os, threading, time, unittest

from helpers import FakeCamera
import common, liveview


class LiveViewSessionTest(unittest.TestCase):
    def run_session(self, stop_after, secs=30, camera=None):
        cam = (camera or FakeCamera()).start()
        if stop_after is not None:
            def stopper():
                time.sleep(stop_after)
                open(common.MEDIA_STOP, "w").close()
            threading.Thread(target=stopper, daemon=True).start()
        t0 = time.time()
        with open(os.devnull, "wb") as fh:
            liveview.liveview_session(cam.server_sock, fh, idle=1.5, secs=secs)
        cam.stop()
        return cam, time.time() - t0

    def test_go_reports_and_stop(self):
        cam, _ = self.run_session(stop_after=2.6)
        types = [t for _, t, _, _ in cam.records]
        self.assertEqual(cam.raw[:9], liveview.LIVE_GO, "go-ahead first, right after the handshake")
        self.assertGreaterEqual(types.count(0x12), 2, "a receiver report every second")
        self.assertEqual(types[-2:], [0x07, 0x08], "ends with the two stop records")
        self.assertFalse(os.path.exists(common.MEDIA_STOP), "the stop request is consumed")
        self.assertIsNone(common.get_state("streaming").get("streaming"))

    def test_duration_cap(self):
        _, took = self.run_session(stop_after=None, secs=1.5)
        self.assertLess(took, 4)

    def test_gives_up_when_camera_goes_quiet(self):
        class SilentCamera(FakeCamera):
            def _run(self):          # handshake, then nothing
                self.cam.sendall(b"\x00" * self.handshake)
                self._stop.wait()
        _, took = self.run_session(stop_after=None, camera=SilentCamera())
        self.assertLess(took, 4, "no data for `idle` seconds ends the session")


if __name__ == "__main__":
    unittest.main()
