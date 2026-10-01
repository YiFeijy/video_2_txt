import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from bili_transcript import bvid_from_input, clip, combine, media_fingerprint, media_urls, merge_screen, official_subtitle, parse_time


class TranscriptToolTests(unittest.TestCase):
    def test_time_and_bvid_input(self):
        self.assertEqual(parse_time("32:20.47"), 1940.47)
        self.assertEqual(bvid_from_input("https://www.bilibili.com/video/BV1HUvmBZEv7/?p=2"), "BV1HUvmBZEv7")
        with self.assertRaises(ValueError):
            bvid_from_input("https://unrelated.example/video/BV1HUvmBZEv7")

    def test_ocr_keeps_distinct_equal_length_captions(self):
        rows = [
            {"time": 0.0, "text": "你妈吓坏了", "confidence": 1.0},
            {"time": 1.0, "text": "你爸笑坏了", "confidence": 1.0},
        ]
        self.assertEqual(merge_screen(rows, 1.0, 2.0), [(0.0, 1.0, "你妈吓坏了"), (1.0, 2.0, "你爸笑坏了")])

    def test_cutoff_and_cross_boundary_speech_appears_once(self):
        screen = [(2.0, 5.0, "袁院士")]
        speech = clip([(0.0, 5.2, "袁院士，我叫他隆平大哥")], 5.0)
        rows, voice_only = combine(screen, speech)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][:2], (0.0, 5.0))
        self.assertEqual(rows[0][2], "袁院士")
        self.assertEqual(rows[0][3], "袁院士，我叫他隆平大哥")
        self.assertEqual(voice_only, [])

    def test_dash_media_selection(self):
        data = {"dash": {"audio": [{"baseUrl": "a64", "bandwidth": 64000}, {"baseUrl": "a96", "bandwidth": 96000}],
                         "video": [{"baseUrl": "v360", "height": 360, "bandwidth": 200000},
                                   {"baseUrl": "v720", "height": 720, "bandwidth": 500000}]}}
        self.assertEqual(media_urls(data, "audio")[0][0], "a96")
        self.assertEqual(media_urls(data, "video")[0][0], "v360")

    def test_platform_subtitle_track_is_separate(self):
        payload = b'{"body":[{"from":1,"to":2,"content":"hello"}]}'
        with TemporaryDirectory() as folder, patch("bili_transcript.request_bytes", return_value=payload):
            cues = official_subtitle([{"lan": "zh-CN", "subtitle_url": "//example.com/sub.json"}], Path(folder), "https://www.bilibili.com/")
            self.assertEqual(cues, [(1.0, 2.0, "hello")])
            self.assertTrue((Path(folder) / "official_track.json").exists())

    def test_media_fingerprint_changes_when_local_media_changes(self):
        with TemporaryDirectory() as folder:
            media = Path(folder) / "video.mp4"
            media.write_bytes(b"first")
            first = media_fingerprint(media)
            media.write_bytes(b"second")
            self.assertNotEqual(first, media_fingerprint(media))


if __name__ == "__main__":
    unittest.main()
