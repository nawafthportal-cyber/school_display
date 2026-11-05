"""Media controls must seek correctly through the production static middleware."""
from unittest.mock import patch

from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase

from core.middleware import DirectStaticAssetMiddleware


class StaticVideoRangeTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.middleware = DirectStaticAssetMiddleware(lambda request: HttpResponse(status=404))
        self.asset = patch("core.static_assets.read_static_asset", return_value=("videos/tutorial.mp4", b"0123456789"))
        self.asset.start()
        self.addCleanup(self.asset.stop)

    def get(self, value=None, **headers):
        if value is not None:
            headers["HTTP_RANGE"] = value
        return self.middleware(self.factory.get("/static/videos/tutorial.mp4?v=1", **headers))

    def test_full_video_advertises_seeking_without_streaming(self):
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Accept-Ranges"], "bytes")
        self.assertEqual(response["Content-Length"], "10")
        self.assertEqual(response["Content-Encoding"], "identity")
        self.assertFalse(response.streaming)

    def test_browser_probe_and_open_ended_seek(self):
        for value, expected, content_range in (
            ("bytes=0-1", b"01", "bytes 0-1/10"),
            ("bytes=6-", b"6789", "bytes 6-9/10"),
            ("bytes=7-99", b"789", "bytes 7-9/10"),
            ("bytes=-3", b"789", "bytes 7-9/10"),
            ("bytes=-99", b"0123456789", "bytes 0-9/10"),
        ):
            with self.subTest(range=value):
                response = self.get(value)
                self.assertEqual(response.status_code, 206)
                self.assertEqual(response.content, expected)
                self.assertEqual(response["Content-Range"], content_range)
                self.assertEqual(int(response["Content-Length"]), len(expected))

    def test_unsatisfiable_ranges(self):
        for value in ("bytes=10-", "bytes=8-4", "bytes=-0"):
            with self.subTest(range=value):
                response = self.get(value)
                self.assertEqual(response.status_code, 416)
                self.assertEqual(response["Content-Range"], "bytes */10")
                self.assertEqual(response.content, b"")

    def test_multipart_invalid_and_if_range_fall_back_to_full_video(self):
        for value in ("bytes=0-1,4-5", "invalid", "bytes=-", "bytes=" + "9" * 5000 + "-"):
            with self.subTest(range=value):
                self.assertEqual(self.get(value).status_code, 200)
        self.assertEqual(self.get("bytes=0-1", HTTP_IF_RANGE='"old-version"').status_code, 200)

    def test_head_reports_full_length_without_a_body(self):
        response = self.middleware(self.factory.head("/static/videos/tutorial.mp4", HTTP_RANGE="bytes=0-1"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Length"], "10")
        self.assertEqual(response.content, b"")

    def test_other_static_assets_keep_their_existing_response(self):
        with patch("core.static_assets.read_static_asset", return_value=("css/example.css", b"body{}")):
            response = self.get("bytes=0-1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"body{}")
        self.assertNotIn("Accept-Ranges", response)
