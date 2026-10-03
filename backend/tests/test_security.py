import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["EASYSCRIPT_TOKEN"] = "test-token-123"
os.environ.pop("EASYSCRIPT_AUTH", None)

from fastapi.testclient import TestClient  # noqa: E402

import security  # noqa: E402
import server  # noqa: E402

TOKEN = {"X-EasyScript-Token": "test-token-123"}


class SecurityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(server.app, base_url="http://127.0.0.1:9876")
        fd, cls.media = tempfile.mkstemp(suffix=".wav")
        os.write(fd, b"RIFF0000WAVE")
        os.close(fd)
        fd, cls.secret = tempfile.mkstemp(suffix=".txt")
        os.write(fd, b"private")
        os.close(fd)

    def test_health_is_open(self):
        self.assertEqual(self.client.get("/health").status_code, 200)

    def test_requests_need_token(self):
        r = self.client.get("/settings")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.client.get("/settings", headers={"X-EasyScript-Token": "wrong"}).status_code, 401)
        self.assertEqual(self.client.get("/settings", headers=TOKEN).status_code, 200)

    def test_audio_token_in_query_and_media_only(self):
        p = self.media
        self.assertEqual(self.client.get("/audio", params={"path": p}).status_code, 401)
        self.assertEqual(self.client.get("/audio", params={"path": p, "token": "test-token-123"}).status_code, 200)
        # Non-media files are never served, even with the token.
        r = self.client.get("/audio", params={"path": self.secret}, headers=TOKEN)
        self.assertEqual(r.status_code, 404)

    def test_dns_rebinding_host_rejected(self):
        r = self.client.get("/health", headers={"Host": "evil.example:9876"})
        self.assertEqual(r.status_code, 403)
        r = self.client.get("/settings", headers={**TOKEN, "Host": "localhost:9876"})
        self.assertEqual(r.status_code, 200)

    def test_upload_cannot_escape_upload_dir(self):
        target = os.path.join(tempfile.gettempdir(), "easyscript_escape_test.txt")
        if os.path.exists(target):
            os.remove(target)
        for name in ("../easyscript_escape_test.txt", target, "..\\..\\easyscript_escape_test.txt"):
            r = self.client.post("/upload", headers=TOKEN,
                                 files={"file": (name, b"hello", "text/plain")})
            self.assertEqual(r.status_code, 200, r.text)
            saved = r.json()["path"]
            self.assertEqual(os.path.dirname(os.path.abspath(saved)), os.path.abspath(server.UPLOAD_DIR))
            os.remove(saved)
        self.assertFalse(os.path.exists(target))
        # And without the token the drive-by form post is refused outright.
        r = self.client.post("/upload", files={"file": ("x.wav", b"hello", "audio/wav")})
        self.assertEqual(r.status_code, 401)

    def test_legacy_code_execution_endpoints_gone(self):
        for path in ("/execute-jsx", "/apply-cuts", "/diag-jsx", "/test-razor", "/split-at-points"):
            r = self.client.post(path, headers=TOKEN, json={"code": "1"})
            self.assertIn(r.status_code, (404, 405), path)

    def test_cors_preflight_for_panel_origin(self):
        r = self.client.options("/settings", headers={
            "Origin": "null",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-easyscript-token",
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("access-control-allow-origin"), "null")
        r = self.client.options("/settings", headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
        })
        self.assertNotEqual(r.headers.get("access-control-allow-origin"), "https://evil.example")

    def test_masked_secret_is_not_saved_back(self):
        orig_path = server.SETTINGS_PATH
        fd, tmp = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        server.SETTINGS_PATH = tmp
        try:
            server.save_settings({"hf_token": "hf_abcdefghijklmnop"})
            masked = self.client.get("/settings", headers=TOKEN).json()["hf_token"]
            self.assertEqual(masked, "hf_a...mnop")
            self.client.post("/settings", headers=TOKEN, json={"hf_token": masked, "x": 1})
            self.assertEqual(server.load_settings()["hf_token"], "hf_abcdefghijklmnop")
            self.client.post("/settings", headers=TOKEN, json={"hf_token": "hf_new"})
            self.assertEqual(server.load_settings()["hf_token"], "hf_new")
        finally:
            server.SETTINGS_PATH = orig_path
            os.remove(tmp)

    def test_websocket_requires_token(self):
        from starlette.websockets import WebSocketDisconnect
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect("/ws/live") as ws:
                ws.receive_text()

    def test_host_parser(self):
        self.assertTrue(security.host_allowed("localhost:9876"))
        self.assertTrue(security.host_allowed("127.0.0.1"))
        self.assertTrue(security.host_allowed("[::1]:9876"))
        self.assertFalse(security.host_allowed("127.0.0.1.evil.example"))
        self.assertFalse(security.host_allowed("evil.example:9876"))


if __name__ == "__main__":
    unittest.main()
