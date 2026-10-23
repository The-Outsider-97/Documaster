"""The local server is the security boundary: token, Host, CSRF marker, Origin, CSP, path handling, size limits."""
import json
import unittest
import urllib.request

from support import Harness, TOKEN, make_pdf, needs_slai


@needs_slai
class SecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = Harness()

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def test_api_needs_the_token(self):
        for token in (False,):
            status, _, body = self.h.raw("GET", "/api/capabilities", token=token)
            self.assertEqual(status, 401)
        status, _, _ = self.h.raw("GET", "/api/capabilities", headers={"X-Documaster-Token": "wrong"})
        self.assertEqual(status, 401)
        status, _, _ = self.h.raw("GET", "/api/capabilities", token=False, headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(status, 200)
        status, _, _ = self.h.raw("GET", "/api/capabilities", token=False, headers={"Cookie": f"dm_session={TOKEN}"})
        self.assertEqual(status, 200)

    def test_token_in_query_is_not_accepted_for_the_api(self):
        status, _, _ = self.h.raw("GET", f"/api/capabilities?token={TOKEN}", token=False)
        self.assertEqual(status, 401)

    def test_landing_link_sets_a_strict_cookie(self):
        req = urllib.request.Request(self.h.base + f"/?token={TOKEN}", headers={"Host": f"127.0.0.1:{self.h.port}"})
        opener = urllib.request.build_opener(type("NoRedirect", (urllib.request.HTTPRedirectHandler,), {"redirect_request": lambda *a, **k: None}))
        try:
            opener.open(req)
            self.fail("expected a redirect")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 302)
            cookie = e.headers["Set-Cookie"]
            self.assertIn("HttpOnly", cookie)
            self.assertIn("SameSite=Strict", cookie)

    def test_wrong_token_in_landing_link_gives_no_cookie(self):
        status, headers, _ = self.h.raw("GET", "/?token=nope", token=False)
        self.assertNotIn("Set-Cookie", headers)

    def test_host_header_must_be_loopback_with_our_port(self):
        for host in ("evil.example.com", f"evil.example.com:{self.h.port}", "127.0.0.1:1", "127.0.0.1"):
            status, _, _ = self.h.raw("GET", "/api/capabilities", headers={"Host": host})
            self.assertEqual(status, 421, host)
        status, _, _ = self.h.raw("GET", "/", token=False, headers={"Host": "rebind.example.com"})
        self.assertEqual(status, 421)

    def test_changes_need_the_request_marker(self):
        status, _, body = self.h.raw("POST", "/api/documents/none/redo", b"{}", marker=False)
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"]["code"], "csrf")

    def test_foreign_origin_is_refused(self):
        status, _, body = self.h.raw("POST", "/api/documents/none/redo", b"{}", headers={"Origin": "http://evil.example.com"})
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"]["code"], "bad_origin")
        status, _, _ = self.h.raw("POST", "/api/documents/none/redo", b"{}", headers={"Origin": self.h.base})
        self.assertNotEqual(status, 403)

    def test_cross_origin_preflight_is_refused(self):
        status, headers, _ = self.h.raw("OPTIONS", "/api/convert", token=False)
        self.assertEqual(status, 405)
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_security_headers_on_every_response(self):
        for path in ("/", "/api/capabilities"):
            _, headers, _ = self.h.raw("GET", path)
            csp = headers["Content-Security-Policy"]
            self.assertIn("script-src 'self'", csp)
            self.assertNotIn("unsafe-eval", csp)
            self.assertIn("frame-ancestors 'none'", csp)
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(headers["Referrer-Policy"], "no-referrer")

    def test_static_files_cannot_escape_the_client_folder(self):
        for path in ("/../slai/config.py", "/..%2fslai%2fconfig.py", "/%2e%2e/slai/config.py", "/js/../../slai/config.py", "//etc/passwd", "/js/%00.js"):
            status, _, body = self.h.raw("GET", path, token=False)
            self.assertNotIn(b"class Settings", body, path)
            self.assertNotIn(b"root:x:", body, path)

    def test_unknown_endpoint_and_method(self):
        self.assertEqual(self.h.raw("GET", "/api/nothing")[0], 404)
        self.assertEqual(self.h.raw("DELETE", "/api/capabilities")[0], 405)

    def test_malformed_json_is_a_clean_400(self):
        status, _, body = self.h.raw("POST", "/api/merge", b"{not json", {"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        self.assertFalse(json.loads(body)["ok"])

    def test_json_body_must_be_an_object(self):
        status, _, _ = self.h.raw("POST", "/api/merge", b"[1,2,3]", {"Content-Type": "application/json"})
        self.assertEqual(status, 400)

    def test_upload_rejects_a_file_that_is_not_what_its_name_says(self):
        status, _, body = self.h.raw("POST", "/api/documents?name=evil.pdf", b"MZ\x90\x00 this is not a pdf", {"Content-Type": "application/octet-stream"})
        self.assertGreaterEqual(status, 400)
        self.assertFalse(json.loads(body)["ok"])

    def test_upload_name_cannot_carry_a_path(self):
        d = self.h.upload("../../../evil.pdf", make_pdf([["x"]]))
        self.assertNotIn("/", d["name"])
        self.assertNotIn("..", d["name"])

    def test_unknown_output_cannot_be_downloaded(self):
        for oid in ("../../etc/passwd", "0000000000", "x"):
            status, _, _ = self.h.raw("GET", f"/api/outputs/{oid}/download")
            self.assertIn(status, (400, 404))

    def test_non_loopback_host_is_refused_at_startup(self):
        from slai.__main__ import main
        self.assertEqual(main(["--host", "0.0.0.0", "--no-browser"]), 2)


if __name__ == "__main__":
    unittest.main()
