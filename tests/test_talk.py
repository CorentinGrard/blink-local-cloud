"""Two-way talk: browser PCM -> ffmpeg -> paced 0x05 records on the live-view socket.

These run the real encoder (the system's ffmpeg), so in CI they also run inside
the built image: an ffmpeg upgrade there once added ~4.4 s of input probing.
"""
import os, threading, time, unittest

from helpers import HAVE_FFMPEG, FakeCamera, adts_frames, encode_adts, speech_like_pcm
import common, liveview, talk

FRAME = 1024 / 16000            # one AAC frame at 16 kHz = 64 ms


def run_session(secs, script):
    """Run a live-view session against a FakeCamera while `script()` drives talk
    in another thread; returns the camera after the session ends."""
    cam = FakeCamera().start()
    t = threading.Thread(target=script, daemon=True)
    t.start()
    def stopper():
        time.sleep(secs)
        open(common.MEDIA_STOP, "w").close()
    threading.Thread(target=stopper, daemon=True).start()
    with open(os.devnull, "wb") as fh:
        liveview.liveview_session(cam.server_sock, fh, idle=5, secs=secs + 5)
    cam.stop()
    return cam


def stream_pcm(sid, pcm, rate=48000, chunk_secs=0.1, start_delay=0.5):
    """Feed PCM like the browser does: one ~100 ms chunk every 100 ms."""
    time.sleep(start_delay)
    step = int(rate * chunk_secs) * 2
    t0 = time.time()
    for k, i in enumerate(range(0, len(pcm), step)):
        time.sleep(max(0, t0 + k * chunk_secs - time.time()))
        assert talk.TALK.feed(sid, rate, pcm[i:i + step])
    talk.TALK.end(sid)


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
class LiveTalkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pcm = speech_like_pcm(3.0)
        cls.cam = run_session(6.0, lambda: stream_pcm("press1", cls.pcm))

    def test_records_are_aac_lc_16k_mono_adts(self):
        audio = self.cam.audio()
        self.assertGreater(len(audio), 30)
        for _, _, _, fr in audio:
            # FF F1 = ADTS, MPEG-4, no CRC; 60 40 = AAC-LC, 16 kHz, mono
            self.assertEqual(fr[:2], b"\xff\xf1")
            self.assertEqual(fr[2] & 0xfc, 0x60 & 0xfc)
            n = ((fr[3] & 3) << 11) | (fr[4] << 3) | (fr[5] >> 5)
            self.assertEqual(n, len(fr), "record length must equal the ADTS frame length")

    def test_no_encoder_metadata_frame(self):
        self.assertFalse(any(b"Lavc" in fr for _, _, _, fr in self.cam.audio()))

    def test_seq_counts_from_zero(self):
        seqs = [s for _, _, s, _ in self.cam.audio()]
        self.assertEqual(seqs, list(range(len(seqs))))

    def test_all_audio_arrives(self):
        # 3 s of input = ~47 frames; allow for encoder priming/flush
        self.assertGreaterEqual(len(self.cam.audio()), int(3.0 / FRAME) - 3)

    def test_paced_in_real_time(self):
        times = [t for t, *_ in self.cam.audio()]
        span = times[-1] - times[0]
        expected = (len(times) - 1) * FRAME
        self.assertAlmostEqual(span, expected, delta=0.5)


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
class EncoderLatencyTest(unittest.TestCase):
    def test_first_frame_is_quick(self):
        """Regression: without -probesize/-analyzeduration, ffmpeg 7.1 held the
        first ~4.4 s of every press (and then dropped it)."""
        pcm = speech_like_pcm(2.0)
        talk.TALK.set_live(True)
        try:
            got = []
            t0 = time.time()
            for k, i in enumerate(range(0, len(pcm), 9600)):
                time.sleep(max(0, t0 + k * 0.1 - time.time()))
                talk.TALK.feed("lat", 48000, pcm[i:i + 9600])
                if talk.TALK.pending() and not got:
                    got.append(time.time() - t0)
            talk.TALK.end("lat")
            deadline = time.time() + 2
            while not got and time.time() < deadline:
                if talk.TALK.pending():
                    got.append(time.time() - t0)
                time.sleep(0.01)
        finally:
            talk.TALK.set_live(False)
        self.assertTrue(got, "no frame came out of the encoder")
        self.assertLess(got[0], 1.0, f"first frame after {got[0]:.2f}s")


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
class PressesTest(unittest.TestCase):
    def test_each_press_restarts_seq(self):
        """Regression: the camera plays only a press whose records start at seq 0."""
        clip = encode_adts(speech_like_pcm(1.0))
        n = len(adts_frames(clip))

        def two_presses():
            time.sleep(0.3)
            self.assertTrue(talk.TALK.feed_adts(clip))
            time.sleep(n * FRAME + 0.8)
            self.assertTrue(talk.TALK.feed_adts(clip))

        cam = run_session(2 * n * FRAME + 2.5, two_presses)
        seqs = [s for _, _, s, _ in cam.audio()]
        self.assertEqual(seqs, list(range(n)) * 2)


class TalkHubTest(unittest.TestCase):
    def test_refused_without_live_view(self):
        talk.TALK.set_live(False)
        self.assertFalse(talk.TALK.feed("x", 48000, b"\0" * 9600))
        self.assertFalse(talk.TALK.feed_adts(b""))

    def test_feed_adts_splits_frames(self):
        f1 = b"\xff\xf1\x60\x40" + bytes([0x01, 0x7f, 0xfc]) + b"\x00" * 4   # 11-byte frame
        talk.TALK.set_live(True)
        try:
            self.assertTrue(talk.TALK.feed_adts(f1 * 3))
            self.assertEqual(talk.TALK.qlen(), 3)
        finally:
            talk.TALK.set_live(False)
        self.assertEqual(talk.TALK.qlen(), 0, "frames are dropped when the live view ends")


if __name__ == "__main__":
    unittest.main()
